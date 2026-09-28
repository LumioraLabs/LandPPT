"""Remaining billable routes reserve before the model and skip an open hold."""

import asyncio
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from test_billable_route_gates import _balance, _open_ledger, _seed_reserved


class _Request:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _ppt(provider_name="landppt", ai_provider=None, project=None, **methods):
    return SimpleNamespace(
        get_role_provider_async=AsyncMock(
            return_value=(ai_provider, {"provider": provider_name})
        ),
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        **methods,
    )


@pytest.mark.asyncio
async def test_native_dialog_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class Provider:
        async def stream_chat_completion(self, **kwargs):
            calls["n"] += 1
            yield "hello"

    request = routes.AISlideNativeDialogRequest(
        slideIndex=1, slideTitle="Intro", slideContent="<div>x</div>", userRequest="Hello"
    )
    monkeypatch.setattr(
        routes, "get_ppt_service_for_user", lambda uid: _ppt(ai_provider=Provider())
    )
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("ai-native-dialog", user.id, 1, "Hello"),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.ai_slide_native_dialog_stream(request, user)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        assert "in_progress" in "".join(chunks)
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 19
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_dialog_commits_a_new_hold(tmp_path, monkeypatch):
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class Provider:
        async def stream_chat_completion(self, **kwargs):
            calls["n"] += 1
            yield "hello"

    request = routes.AISlideNativeDialogRequest(
        slideIndex=1, slideTitle="Intro", slideContent="<div>x</div>", userRequest="Hello"
    )
    monkeypatch.setattr(
        routes, "get_ppt_service_for_user", lambda uid: _ppt(ai_provider=Provider())
    )
    try:
        response = await routes.ai_slide_native_dialog_stream(request, user)
        async for _chunk in response.body_iterator:
            pass
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 19
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_outline_optimize_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = routes.OutlineAIOptimizeRequest(
        outline_content='{"slides":[]}',
        user_request="tighter",
        project_info={"project_id": "deck"},
    )

    async def chat(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="{}")

    monkeypatch.setattr(
        routes,
        "get_ppt_service_for_user",
        lambda uid: _ppt(_chat_completion_for_role=chat),
    )
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id(
            "outline-optimize", user.id, "full", None, request.outline_content, "tighter"
        ),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.ai_optimize_outline(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bullet_enhance_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = routes.AIBulletPointEnhanceRequest(
        slideIndex=1,
        slideTitle="Intro",
        slideContent="<div>x</div>",
        userRequest="richer",
        projectInfo={"project_id": "deck"},
        contextInfo={"originalBulletPoint": "Ship faster"},
    )

    async def complete(*args, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(content="Ship the paid plan faster")

    monkeypatch.setattr(
        routes,
        "get_ppt_service_for_user",
        lambda uid: _ppt(_text_completion_for_role=complete),
    )
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("ai-bullet", user.id, 1, "richer", "Ship faster"),
        "ai_edit",
        1,
        3,
    )
    try:
        response = await routes.ai_enhance_bullet_point(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_image_regenerate_skips_generation_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = routes.AIImageRegenerateRequest(
        slide_index=0,
        image_info={"id": "img-1", "alt": "old"},
        slide_content={"title": "Intro", "html_content": "<div>x</div>"},
        project_topic="Deck",
        project_scenario="business",
    )

    async def provider_config(user_id):
        return {"provider_name": "landppt"}

    async def ai_provider(user_id):
        calls["n"] += 1
        return object()

    monkeypatch.setattr(
        "landppt.services.image.image_service.get_image_service", lambda: object()
    )
    monkeypatch.setattr(
        "landppt.services.db_config_service.get_user_ai_provider_config", provider_config
    )
    monkeypatch.setattr("landppt.services.db_config_service.get_user_ai_provider", ai_provider)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: _ppt())
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("ai-image-regenerate", user.id, 0, "Deck", "img-1"),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.ai_regenerate_image(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_auto_images_skip_generation_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import ai_edit_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    request = routes.AIAutoImageGenerateRequest(
        slide_index=0,
        slide_content={"title": "Intro", "html_content": "<div>x</div>"},
        project_topic="Deck",
        project_scenario="business",
    )

    async def provider_config(user_id):
        return {"provider_name": "landppt"}

    async def ai_provider(user_id):
        calls["n"] += 1
        return object()

    monkeypatch.setattr(
        "landppt.services.image.image_service.get_image_service", lambda: object()
    )
    monkeypatch.setattr(
        "landppt.services.db_config_service.get_user_ai_provider_config", provider_config
    )
    monkeypatch.setattr("landppt.services.db_config_service.get_user_ai_provider", ai_provider)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("ai-auto-images", user.id, 0, "Deck", "Intro"),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.ai_auto_generate_slide_images(request, user)
        assert response["in_progress"] is True
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_speech_generation_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import speech_script_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeSpeech:
        def __init__(self, user_id=None):
            self.provider_settings = {"provider": "landppt"}

        async def initialize_async(self):
            return None

        async def generate_multi_slide_scripts_with_retry(self, *args, **kwargs):
            calls["n"] += 1

        async def generate_full_presentation_scripts(self, *args, **kwargs):
            calls["n"] += 1

    project = SimpleNamespace(slides_data=[{"title": "A"}])
    monkeypatch.setattr(
        routes, "ppt_service", SimpleNamespace(project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)))
    )
    monkeypatch.setattr(
        "landppt.services.speech_script_service.SpeechScriptService", FakeSpeech
    )
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("speech-script", "deck", "single", "0"),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.generate_speech_script(
            "deck",
            routes.SpeechScriptGenerationRequest(generation_type="single", slide_indices=[0]),
            user,
        )
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 19
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_speech_humanize_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import speech_script_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeSpeech:
        def __init__(self, user_id=None):
            self.provider_settings = {"provider": "landppt"}

        async def initialize_async(self):
            return None

        async def humanize_script(self, *args, **kwargs):
            calls["n"] += 1
            return "natural"

    monkeypatch.setattr(
        routes,
        "ppt_service",
        SimpleNamespace(project_manager=SimpleNamespace(get_project=AsyncMock(return_value=SimpleNamespace()))),
    )
    monkeypatch.setattr(
        "landppt.services.speech_script_service.SpeechScriptService", FakeSpeech
    )
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("speech-humanize", "deck", "zh", "0"),
        "ai_other",
        1,
        1,
    )
    try:
        response = await routes.humanize_speech_scripts(
            "deck",
            routes.SpeechScriptHumanizeRequest(
                scripts=[routes.SpeechScriptHumanizeItem(slide_index=0, script_content="Formal text")]
            ),
            user,
        )
        assert response["in_progress"] is True
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_free_template_generation_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import template_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = SimpleNamespace(project_metadata={"template_mode": "free"})

    async def stream_free_template_generation(*args, **kwargs):
        calls["n"] += 1
        yield {"type": "complete", "template": {"html_template": "<div>new</div>"}}

    service = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        global_template_service=SimpleNamespace(
            _get_template_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"}))
        ),
        stream_free_template_generation=stream_free_template_generation,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: service)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("free-template", "deck", "True"),
        "template_generation",
        1,
        10,
    )
    try:
        response = await routes.generate_project_free_template(
            "deck", _Request({"force": True, "stream": False}), user
        )
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_free_template_adjustment_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.services.credits_service import billable_operation_id
    from landppt.web.route_modules import template_routes as routes

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = SimpleNamespace(
        project_metadata={
            "template_mode": "free",
            "free_template_html": "<div>old</div>",
            "free_template_name": "Free",
        }
    )

    async def adjust(**kwargs):
        calls["n"] += 1
        yield {"type": "complete", "html_template": "<div>new</div>"}

    service = SimpleNamespace(
        project_manager=SimpleNamespace(
            get_project=AsyncMock(return_value=project),
            update_project_metadata=AsyncMock(),
        ),
        global_template_service=SimpleNamespace(
            _get_template_role_provider_async=AsyncMock(return_value=(None, {"provider": "landppt"})),
            adjust_template_with_ai_stream=adjust,
        ),
        clear_cached_style_genes=lambda project_id: None,
    )
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda uid: service)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("free-template-adjust", "deck", "brighter"),
        "template_generation",
        1,
        10,
    )
    try:
        response = await routes.adjust_project_free_template(
            "deck", _Request({"adjustment_request": "brighter"}), user
        )
        assert response["in_progress"] is True
        assert calls["n"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_global_template_adjustment_skips_the_model_while_a_hold_is_open(
    tmp_path, monkeypatch
):
    from landppt.api.global_master_template_api import adjust_template
    from landppt.services.credits_service import billable_operation_id
    import landppt.api.global_master_template_api as api

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}

    class FakeTemplates:
        def __init__(self, user_id=None, **kwargs):
            self.user_id = user_id

        async def _get_template_role_provider_async(self):
            return None, {"provider": "landppt"}

        async def adjust_template_with_ai_stream(self, **kwargs):
            calls["n"] += 1
            yield {"type": "complete", "html_template": "<div>new</div>"}

    monkeypatch.setattr(api, "GlobalMasterTemplateService", FakeTemplates)
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("template-adjust", user.id, "Ocean", "brighter"),
        "template_generation",
        1,
        10,
    )
    try:
        response = await adjust_template(
            {
                "html_template": "<div>old</div>",
                "adjustment_request": "brighter",
                "template_name": "Ocean",
                "stream": False,
            },
            user,
            None,
        )
        assert response["in_progress"] is True
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unattended_outline_skips_the_model_while_a_hold_is_open(tmp_path, monkeypatch):
    from landppt.services.credits_service import billable_operation_id
    from landppt.services.unattended_service import UnattendedPipelineRunner

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    project = SimpleNamespace(
        topic="Deck",
        outline=None,
        confirmed_requirements={
            "topic": "Deck",
            "content_source": "topic",
            "page_count_settings": {"mode": "fixed", "fixed_pages": 1},
        },
    )

    async def generate_outline_streaming(project_id, force_regenerate=False):
        calls["n"] += 1
        yield 'data: {"done": true, "llm_call_count": 1}\n\n'

    service = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=AsyncMock(return_value=project)),
        generate_outline_streaming=generate_outline_streaming,
    )
    runner = UnattendedPipelineRunner(project_id="deck", user_id=user.id, config={})
    runner._service = lambda: service
    runner._resolve_provider = AsyncMock(return_value="landppt")
    runner._set_stage = AsyncMock()
    await _seed_reserved(
        maker,
        user.id,
        billable_operation_id("unattended-outline", "deck", "False"),
        "outline_generation",
        1,
        3,
    )
    try:
        with pytest.raises(RuntimeError):
            await runner._run_outline()
        assert calls["n"] == 0
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unattended_outline_commits_a_new_hold(tmp_path, monkeypatch):
    from landppt.services.unattended_service import UnattendedPipelineRunner

    engine, maker, user = await _open_ledger(tmp_path, monkeypatch)
    calls = {"n": 0}
    empty = SimpleNamespace(
        topic="Deck",
        outline=None,
        confirmed_requirements={
            "topic": "Deck",
            "content_source": "topic",
            "page_count_settings": {"mode": "fixed", "fixed_pages": 1},
        },
    )
    filled = SimpleNamespace(
        topic="Deck",
        outline={"slides": [{"title": "A"}]},
        confirmed_requirements=empty.confirmed_requirements,
    )
    loads = {"n": 0}

    async def get_project(*args, **kwargs):
        loads["n"] += 1
        return empty if loads["n"] == 1 else filled

    async def generate_outline_streaming(project_id, force_regenerate=False):
        calls["n"] += 1
        yield 'data: {"done": true, "llm_call_count": 1}\n\n'

    service = SimpleNamespace(
        project_manager=SimpleNamespace(get_project=get_project),
        generate_outline_streaming=generate_outline_streaming,
    )
    runner = UnattendedPipelineRunner(project_id="deck", user_id=user.id, config={})
    runner._service = lambda: service
    runner._resolve_provider = AsyncMock(return_value="landppt")
    runner._set_stage = AsyncMock()
    try:
        await runner._run_outline()
        assert calls["n"] == 1
        assert await _balance(maker, user.id) == 17
    finally:
        await engine.dispose()
