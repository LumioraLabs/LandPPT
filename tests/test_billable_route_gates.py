"""Shipped routes must not call the model unless they created the credit hold."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock

from test_billing_service import _billing_database, _create_user


async def _balance(maker, user_id):
    from landppt.database.models import User

    async with maker() as session:
        return await session.scalar(
            select(User.credits_balance).where(User.id == user_id)
        )


async def _open_ledger(tmp_path, monkeypatch):
    from landppt.core.config import app_config
    from landppt.database.models import CreditReservation, UserMetrics

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: CreditReservation.__table__.create(sync_connection)
        )
        await connection.run_sync(
            lambda sync_connection: UserMetrics.__table__.create(sync_connection)
        )
    monkeypatch.setattr(app_config, "enable_credits_system", True)
    monkeypatch.setattr("landppt.database.database.AsyncSessionLocal", maker)
    monkeypatch.setattr("landppt.web.route_modules.support.AsyncSessionLocal", maker)
    monkeypatch.setattr(
        "landppt.api.global_master_template_api.AsyncSessionLocal", maker
    )
    user = await _create_user(maker)
    async with maker() as session:
        from landppt.database.models import User

        await session.execute(
            User.__table__.update().where(User.id == user.id).values(credits_balance=20)
        )
        await session.commit()
    return engine, maker, user


async def _seed_reserved(
    maker, user_id, operation_id, operation_type, quantity, amount
):
    from landppt.database.models import CreditReservation, User

    async with maker() as session:
        balance = await session.scalar(
            select(User.credits_balance).where(User.id == user_id)
        )
        await session.execute(
            User.__table__.update()
            .where(User.id == user_id)
            .values(credits_balance=balance - amount)
        )
        session.add(
            CreditReservation(
                user_id=user_id,
                operation_id=operation_id,
                operation_type=operation_type,
                quantity=quantity,
                reserved_amount=amount,
                committed_amount=0,
                status="reserved",
                reference_id="project",
                expires_at=10**10,
                created_at=1,
                updated_at=1,
            )
        )
        await session.commit()


def _slide_project():
    return SimpleNamespace(
        title="Deck",
        topic="Deck",
        scenario="general",
        requirements="Explain the product",
        outline={"title": "Deck", "slides": [{"title": "A", "content_points": ["fact"]}]},
        confirmed_requirements={
            "topic": "Deck",
            "page_count_settings": {"mode": "fixed", "fixed_pages": 1},
        },
        project_metadata={"render_mode": "svg", "language": "zh"},
        slides_data=[
            {"page_number": 1, "title": "Old", "html_content": "<div>old</div>"}
        ],
    )


@pytest.mark.asyncio
async def test_batch_regenerate_does_not_call_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import slide_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def generate(*args, **kwargs):
        calls["n"] += 1
        return "<div>new</div>"

    project = _slide_project()
    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_locked_slide_indices=AsyncMock(return_value=[]),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _ensure_global_master_template_selected=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    operation_id = billable_operation_id("slide-batch", "deck", "0")
    await _seed_reserved(maker, user.id, operation_id, "slide_generation", 1, 5)
    try:
        response = await routes._do_batch_regenerate(
            "deck",
            routes.SlideBatchRegenerateRequest(regenerate_all=True),
            user,
        )
        assert response["in_progress"] is True
        assert response["success"] is False
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 15
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_regenerate_calls_the_model_once_for_a_new_hold(
    tmp_path, monkeypatch
):
    import landppt.services.db_project_manager as db_module
    from landppt.web.route_modules import slide_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def generate(*args, **kwargs):
        calls["n"] += 1
        return "<div>new</div>"

    project = _slide_project()
    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_locked_slide_indices=AsyncMock(return_value=[]),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _ensure_global_master_template_selected=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )
    db = SimpleNamespace(
        save_single_slide=AsyncMock(return_value=True),
        update_project_data=AsyncMock(),
    )
    monkeypatch.setattr(db_module, "DatabaseProjectManager", lambda: db)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        response = await routes._do_batch_regenerate(
            "deck",
            routes.SlideBatchRegenerateRequest(regenerate_all=True),
            user,
        )
        assert response["success"] is True, response
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 15
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_batch_regenerate_releases_the_hold_when_cancelled(tmp_path, monkeypatch):
    from landppt.web.route_modules import slide_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)

    async def generate(*args, **kwargs):
        raise asyncio.CancelledError()

    project = _slide_project()
    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_locked_slide_indices=AsyncMock(return_value=[]),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _ensure_global_master_template_selected=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        with pytest.raises(asyncio.CancelledError):
            await routes._do_batch_regenerate(
                "deck",
                routes.SlideBatchRegenerateRequest(regenerate_all=True),
                user,
            )
        assert await _balance(maker, user.id) == 20
    finally:
        await engine.dispose()


def _edit_request(**overrides):
    from landppt.web.route_modules.ai_edit_routes import AISlideEditRequest

    data = {
        "slideIndex": 1,
        "slideTitle": "Intro",
        "slideContent": "<div>Current</div>",
        "userRequest": "Shorten the title",
        "projectInfo": {"project_id": "deck"},
    }
    data.update(overrides)
    return AISlideEditRequest(**data)


@pytest.mark.asyncio
async def test_ai_slide_edit_does_not_call_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = _edit_request()

    async def chat(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="done")

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _chat_completion_for_role=chat,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    operation_id = billable_operation_id(
        "ai-slide-edit", user.id, "deck", request.slideIndex, request.userRequest
    )
    await _seed_reserved(maker, user.id, operation_id, "ai_edit", 1, 3)
    try:
        response = await routes.ai_slide_edit(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ai_slide_edit_calls_the_model_once_for_a_new_hold(tmp_path, monkeypatch):
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def chat(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="updated")

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _chat_completion_for_role=chat,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        response = await routes.ai_slide_edit(_edit_request(), user)
        assert response["success"] is True, response
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ai_slide_edit_releases_the_hold_when_cancelled(tmp_path, monkeypatch):
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)

    async def chat(*args, **kwargs):
        raise asyncio.CancelledError()

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _chat_completion_for_role=chat,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        with pytest.raises(asyncio.CancelledError):
            await routes.ai_slide_edit(_edit_request(), user)
        assert await _balance(maker, user.id) == 20
    finally:
        await engine.dispose()


def _element_request():
    from landppt.web.route_modules.ai_edit_routes import AIElementEditRequest

    return AIElementEditRequest(
        slideIndex=1,
        slideTitle="Intro",
        slideContent="<div>Current</div>",
        elementHtml='<div id="box">Title</div>',
        elementId="box",
        userRequest="Shorter",
        projectInfo={"project_id": "deck"},
    )


@pytest.mark.asyncio
async def test_element_edit_does_not_call_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = _element_request()

    async def chat(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="<div>ok</div>")

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _chat_completion_for_role=chat,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    operation_id = billable_operation_id(
        "ai-element-edit",
        user.id,
        "deck",
        request.slideIndex,
        request.elementId,
        request.userRequest,
    )
    await _seed_reserved(maker, user.id, operation_id, "ai_edit", 1, 3)
    try:
        response = await routes.ai_element_edit(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_element_edit_commits_a_new_hold_after_the_model(tmp_path, monkeypatch):
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def chat(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="<div>ok</div>")

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _chat_completion_for_role=chat,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        response = await routes.ai_element_edit(_element_request(), user)
        assert response["success"] is True, response
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_ai_edit_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = _edit_request()

    class Provider:
        async def stream_chat_completion(self, **kwargs):
            calls["n"] += 1
            yield "hello"

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(
            return_value=(Provider(), {"provider": "landppt"})
        )
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    operation_id = billable_operation_id(
        "ai-slide-edit-stream",
        user.id,
        "deck",
        request.slideIndex,
        request.userRequest,
    )
    await _seed_reserved(maker, user.id, operation_id, "ai_edit", 1, 3)
    try:
        response = await routes.ai_slide_edit_stream(request, user)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        body = "".join(chunks)
        assert "in_progress" in body
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generate_outline_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import outline_generation_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()

    async def generate_outline(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(title="Deck", slides=[], metadata={})

    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        generate_outline=generate_outline,
        _update_outline_generation_stage=AsyncMock(),
    )
    monkeypatch.setattr(routes, "ppt_service", owner)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("outline", "deck"),
        "outline_generation",
        1,
        3,
    )
    try:
        response = await routes.generate_outline("deck", user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generate_outline_commits_one_new_hold(tmp_path, monkeypatch):
    from landppt.web.route_modules import outline_generation_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()

    async def generate_outline(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(title="Deck", slides=[{"title": "A"}], metadata={})

    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        generate_outline=generate_outline,
        _update_outline_generation_stage=AsyncMock(),
    )
    monkeypatch.setattr(routes, "ppt_service", owner)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    try:
        response = await routes.generate_outline("deck", user)
        assert response["status"] == "success", response
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outline_stream_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import outline_generation_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()
    project.outline = None

    async def generate_outline_streaming(project_id, force_regenerate=False):
        calls["n"] += 1
        yield 'data: {"done": true, "llm_call_count": 1}\n\n'

    owner = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        generate_outline_streaming=generate_outline_streaming,
        _update_outline_generation_stage=AsyncMock(),
    )
    monkeypatch.setattr(routes, "ppt_service", owner)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("outline-stream", "deck", "False"),
        "outline_generation",
        1,
        3,
    )
    try:
        response = await routes.stream_outline_generation("deck", False, user)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        assert "in_progress" in "".join(chunks)
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_template_generation_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.api.global_master_template_api import generate_template_with_ai
    from landppt.api.models import GlobalMasterTemplateGenerateRequest
    from landppt.services.credits_service import billable_operation_id
    import landppt.api.global_master_template_api as api

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeTemplates:
        def __init__(self, user_id=None, **kwargs):
            self.user_id = user_id

        async def _get_template_role_provider_async(self):
            return None, {"provider": "landppt"}

        async def generate_template_with_ai(self, **kwargs):
            calls["n"] += 1
            return {"html": "<div></div>"}

    monkeypatch.setattr(api, "GlobalMasterTemplateService", FakeTemplates)
    request = GlobalMasterTemplateGenerateRequest(prompt="blue", template_name="Ocean")
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("template-generation", user.id, "Ocean", "blue"),
        "template_generation",
        1,
        10,
    )
    try:
        response = await generate_template_with_ai(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_template_generation_calls_the_model_once_for_a_new_hold(
    tmp_path, monkeypatch
):
    from landppt.api.global_master_template_api import generate_template_with_ai
    from landppt.api.models import GlobalMasterTemplateGenerateRequest
    import landppt.api.global_master_template_api as api

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeTemplates:
        def __init__(self, user_id=None, **kwargs):
            self.user_id = user_id

        async def _get_template_role_provider_async(self):
            return None, {"provider": "landppt"}

        async def generate_template_with_ai(self, **kwargs):
            calls["n"] += 1
            return {"html": "<div></div>"}

    monkeypatch.setattr(api, "GlobalMasterTemplateService", FakeTemplates)
    try:
        response = await generate_template_with_ai(
            GlobalMasterTemplateGenerateRequest(prompt="blue", template_name="Ocean"),
            user,
        )
        assert response["success"] is True, response
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ppt_creation_skips_generation_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.services.slide.slide_generation_service import SlideGenerationService
    import landppt.services.db_project_manager as db_module

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()

    class Database:
        async def update_stage_status(self, *args, **kwargs):
            return None

        async def list_slides(self, project_id):
            return []

        async def get_single_slide(self, project_id, index):
            return None

        async def save_single_slide(self, *args, **kwargs):
            return True

        async def update_project_data(self, *args, **kwargs):
            return None

    async def generate(*args, **kwargs):
        calls["n"] += 1
        return "<div>page</div>"

    service = SimpleNamespace(
        user_id=user.id,
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _get_user_generation_config=AsyncMock(
            return_value={
                "enable_parallel_generation": False,
                "parallel_slides_count": 1,
            }
        ),
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        _prepare_project_creative_guidance=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )
    monkeypatch.setattr(db_module, "DatabaseProjectManager", Database)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("ppt-creation", "deck", "0"),
        "slide_generation",
        1,
        5,
    )
    try:
        chunks = []
        async for chunk in SlideGenerationService(service)._generate_slides_streaming_impl(
            "deck"
        ):
            chunks.append(chunk)
        body = "".join(chunks)
        assert "in_progress" in body
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 15
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ppt_creation_calls_the_model_once_for_a_new_hold(tmp_path, monkeypatch):
    from landppt.services.slide.slide_generation_service import SlideGenerationService
    import landppt.services.db_project_manager as db_module

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()

    class Database:
        async def update_stage_status(self, *args, **kwargs):
            return None

        async def list_slides(self, project_id):
            return []

        async def get_single_slide(self, project_id, index):
            return None

        async def save_single_slide(self, *args, **kwargs):
            return True

        async def update_project_data(self, *args, **kwargs):
            return None

    async def generate(*args, **kwargs):
        calls["n"] += 1
        return "<div>page</div>"

    async def no_cache():
        return None

    service = SimpleNamespace(
        user_id=user.id,
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _get_user_generation_config=AsyncMock(
            return_value={
                "enable_parallel_generation": False,
                "parallel_slides_count": 1,
            }
        ),
        _is_slides_generation_cancelled=AsyncMock(return_value=False),
        _prepare_project_creative_guidance=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )
    monkeypatch.setattr(db_module, "DatabaseProjectManager", Database)
    monkeypatch.setattr("landppt.services.cache_service.get_cache_service", no_cache)
    try:
        chunks = []
        async for chunk in SlideGenerationService(service)._generate_slides_streaming_impl(
            "deck"
        ):
            chunks.append(chunk)
        assert any('"type": "complete"' in chunk or '"type":"complete"' in chunk for chunk in chunks)
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 15
    finally:
        await engine.dispose()


def _ppt_generation_service(user, project, generate, cancelled):
    return SimpleNamespace(
        user_id=user.id,
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
        _load_prompts_md_system_prompt=lambda: "",
        _get_user_generation_config=AsyncMock(
            return_value={
                "enable_parallel_generation": False,
                "parallel_slides_count": 1,
            }
        ),
        _is_slides_generation_cancelled=cancelled,
        _prepare_project_creative_guidance=AsyncMock(),
        _generate_single_slide_html_with_prompts=generate,
        _combine_slides_to_full_html=lambda *args: "combined",
    )


class _RememberingSlides:
    def __init__(self):
        self.saved = {}

    async def update_stage_status(self, *args, **kwargs):
        return None

    async def list_slides(self, project_id):
        return [dict(slide) for slide in self.saved.values()]

    async def get_single_slide(self, project_id, index):
        slide = self.saved.get(index)
        return dict(slide) if slide else None

    async def save_single_slide(self, project_id, index, data, **kwargs):
        self.saved[index] = dict(data)
        return True

    async def update_project_data(self, *args, **kwargs):
        return None


async def _ppt_events(service, project_id="deck"):
    from landppt.services.slide.slide_generation_service import SlideGenerationService

    chunks = []
    async for chunk in SlideGenerationService(service)._generate_slides_streaming_impl(
        project_id
    ):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_ppt_stop_releases_the_hold_and_a_later_run_generates(tmp_path, monkeypatch):
    import json

    from landppt.database.models import CreditReservation
    from sqlalchemy import select
    import landppt.services.db_project_manager as db_module

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    cancelled = {"value": True}
    project = _slide_project()
    database = _RememberingSlides()

    async def generate(*args, **kwargs):
        calls["n"] += 1
        return "<div>ok</div>"

    async def is_cancelled(project_id, cache=None):
        return cancelled["value"]

    monkeypatch.setattr(db_module, "DatabaseProjectManager", lambda: database)
    monkeypatch.setattr(
        "landppt.services.cache_service.get_cache_service", AsyncMock(return_value=None)
    )
    service = _ppt_generation_service(user, project, generate, is_cancelled)
    try:
        stopped = await _ppt_events(service)
        stopped_events = [
            json.loads(line[6:])
            for chunk in stopped
            for line in chunk.splitlines()
            if line.startswith("data: ")
        ]
        assert any(
            event.get("message") == "已收到停止请求，已停止生成。"
            for event in stopped_events
        )
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 20
        async with maker() as session:
            status = await session.scalar(select(CreditReservation.status))
        assert status == "released"

        cancelled["value"] = False
        resumed = await _ppt_events(service)
        assert any('"type": "complete"' in chunk for chunk in resumed)
        assert calls["n"] == 1
        assert "<div>ok</div>" in "".join(resumed)
        assert await _balance(maker, user.id) == 15
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_ppt_page_is_reserved_again_when_retried(tmp_path, monkeypatch):
    import landppt.services.db_project_manager as db_module

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = _slide_project()
    database = _RememberingSlides()

    async def generate(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model down")
        return "<div>ok</div>"

    monkeypatch.setattr(db_module, "DatabaseProjectManager", lambda: database)
    monkeypatch.setattr(
        "landppt.services.cache_service.get_cache_service", AsyncMock(return_value=None)
    )
    service = _ppt_generation_service(
        user, project, generate, AsyncMock(return_value=False)
    )
    try:
        failed = await _ppt_events(service)
        assert any('"partial": true' in chunk or '"partial":true' in chunk for chunk in failed)
        assert calls["n"] == 1
        assert database.saved[0]["generation_failed"] is True
        assert await _balance(maker, user.id) == 20

        retried = await _ppt_events(service)
        assert calls["n"] == 2
        assert database.saved[0]["html_content"] == "<div>ok</div>"
        assert database.saved[0].get("generation_failed") is not True
        assert await _balance(maker, user.id) == 15
        assert any('"type": "complete"' in chunk for chunk in retried)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_slide_edit_agent_does_not_start_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.slide.slide_edit_agent_service import SlideEditAgentRequest
    from landppt.web.route_modules import slide_edit_agent_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeAgent:
        def __init__(self):
            calls["n"] += 1

        async def run_agent(self, *args, **kwargs):
            calls["n"] += 1

    owner = SimpleNamespace(
        get_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"}))
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: owner)
    monkeypatch.setattr(routes, "SlideEditAgentService", FakeAgent)
    await _seed_reserved(maker, user.id, "run-held", "ai_edit", 1, 3)
    try:
        response = await routes.stream_slide_edit_agent(
            SlideEditAgentRequest(
                projectId="deck",
                slideIndex=1,
                slideTitle="Intro",
                slideContent="<div>Current</div>",
                userRequest="Shorten the title",
                runId="run-held",
            ),
            user,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        assert "in_progress" in "".join(chunks)
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_and_worker_start_the_reservation_sweep(monkeypatch):
    entered = {"lifespan": asyncio.Event(), "worker": asyncio.Event()}

    async def sweep(interval_seconds=30):
        entered["lifespan"].set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(
        "landppt.services.credits_service.sweep_expired_credit_reservations", sweep
    )
    import landppt.main as main

    monkeypatch.setattr(main, "startup_application", AsyncMock())
    monkeypatch.setattr(main, "shutdown_application", AsyncMock())
    async with main.lifespan(None):
        await asyncio.wait_for(entered["lifespan"].wait(), timeout=1)

    async def worker_sweep(interval_seconds=30):
        entered["worker"].set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(
        "landppt.services.credits_service.sweep_expired_credit_reservations",
        worker_sweep,
    )
    import landppt.tasks.worker as worker_mod
    from landppt.tasks.worker import Worker

    async def init_db(**kwargs):
        return None

    async def consume(self):
        await asyncio.wait_for(entered["worker"].wait(), timeout=1)
        self.request_stop()

    monkeypatch.setattr(worker_mod, "init_db", init_db)
    monkeypatch.setattr(worker_mod, "close_db", AsyncMock())
    monkeypatch.setattr(worker_mod, "close_cache_service", AsyncMock())
    monkeypatch.setattr(Worker, "_consume_until_stopped", consume)
    await Worker().run_forever()
    assert entered["worker"].is_set()
