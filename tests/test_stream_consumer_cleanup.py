"""Owner layers must deterministically close the streams they forward.

The service layers close their own inner generators. The owners of the outermost
stream are the routes, the unattended pipeline, and the regular-outline research
branch; when a client disconnects, a task is cancelled, or a loop returns early,
they are the ones that must close it, otherwise the abandoned generator unwinds
the conversation scope from the event loop's finalizer, which runs in a copied
Context:

    ValueError: <Token ...> was created in a different Context

These tests drive the real endpoints, the real unattended stages and the real
outline streaming service with stubbed service/provider boundaries, and assert
observable terminal state (scope restored, inner stream closed) rather than
source text.
"""
import asyncio
from contextlib import aclosing
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from landppt.services.runtime.ai_execution import (
    ai_conversation_context,
    get_current_ai_conversation_id,
)


class _Recorder:
    """Stub streams that record their lifecycle and the scope they ran in."""

    def __init__(self):
        self.events = []

    async def provider(self, tag):
        self.events.append((tag + ":provider:start", get_current_ai_conversation_id()))
        try:
            yield "payload-1"
            yield "payload-2"
        finally:
            # Provider shutdown can itself await network/resource cleanup.
            await asyncio.sleep(0)
            self.events.append((tag + ":provider:closed", get_current_ai_conversation_id()))

    def closed(self, name):
        return any(event == name for event, _session in self.events)

    def names(self):
        return [event for event, _session in self.events]


class _FakeProjectManager:
    def __init__(self, project):
        self._project = project
        self.projects = {}
        self.stage_updates = []

    async def get_project(self, project_id, user_id=None):
        return self._project

    async def update_project_status(self, *args, **kwargs):
        return True

    async def update_stage_status(self, *args, **kwargs):
        self.stage_updates.append(args)
        return True


def _project(requirements=None):
    return SimpleNamespace(
        id="project-1",
        project_id="project-1",
        topic="Quarterly Review",
        scenario="general",
        requirements="",
        outline=None,
        confirmed_requirements=requirements or {},
        project_metadata={},
        todo_board=None,
        updated_at=0.0,
    )


async def _disconnect_response(response, mode):
    """Exercise ASGI response handling without manually closing the body iterator."""
    sending = asyncio.Event()
    request_contexts = []

    async def receive():
        await sending.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] != "http.response.body" or not message.get("body"):
            return
        sending.set()
        if mode == "send_error":
            raise OSError("client disconnected")
        if mode == "cancel":
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        # Keep the iterator suspended at yield while the send is interrupted.
        await asyncio.Event().wait()

    async def request():
        spec_version = "2.3" if mode == "disconnect" else "2.4"
        try:
            await response(
                {"type": "http", "asgi": {"spec_version": spec_version}},
                receive,
                send,
            )
        finally:
            request_contexts.append(get_current_ai_conversation_id())

    task = asyncio.create_task(request())
    if mode == "send_error":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(task, timeout=5)
    elif mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    else:
        await asyncio.wait_for(task, timeout=5)
    assert sending.is_set()
    assert request_contexts == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send_error", "disconnect", "cancel"])
async def test_outline_route_closes_forwarded_stream_on_disconnect(monkeypatch, mode):
    """The outline SSE route must close the service stream it forwards."""
    import landppt.web.route_modules.outline_generation_routes as routes
    from landppt.services.enhanced_ppt_service import EnhancedPPTService
    from landppt.services.outline.project_outline_workflow_service import (
        ProjectOutlineWorkflowService,
    )

    recorder = _Recorder()

    async def outline_stream(self, project_id, *, force_regenerate=False):
        recorder.events.append(("outline:start", get_current_ai_conversation_id()))
        try:
            async with aclosing(recorder.provider("outline")) as provider:
                async for _payload in provider:
                    yield 'data: {"content": "x"}\n\n'
        finally:
            recorder.events.append(("outline:closed", get_current_ai_conversation_id()))

    service = object.__new__(EnhancedPPTService)
    service.project_manager = _FakeProjectManager(_project())
    service.project_outline_workflow = ProjectOutlineWorkflowService(service)

    async def role_provider(_role):
        return None, {"provider": "openai"}

    service.get_role_provider_async = role_provider

    streaming = service.project_outline_workflow._outline_generation._streaming_service
    streaming._generate_outline_streaming = outline_stream.__get__(streaming)

    monkeypatch.setattr(routes, "ppt_service", service)
    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda _user_id: service)

    async def allow_credits(*_args, **_kwargs):
        return True, 0, 100

    async def skip_reserve(*_args, **_kwargs):
        return True, "skipped", "skipped"

    async def no_charge(*_args, **_kwargs):
        return None

    monkeypatch.setattr(routes, "check_credits_for_operation", allow_credits)
    monkeypatch.setattr(routes, "reserve_billable_operation", skip_reserve)
    monkeypatch.setattr(routes, "commit_billable_operation", no_charge)
    monkeypatch.setattr(routes, "release_billable_operation", no_charge)

    response = await routes.stream_outline_generation(
        "project-1", False, SimpleNamespace(id=1)
    )

    await _disconnect_response(response, mode)
    assert recorder.closed("outline:closed"), recorder.names()
    assert recorder.closed("outline:provider:closed"), recorder.names()
    assert get_current_ai_conversation_id() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send_error", "disconnect", "cancel"])
async def test_slide_route_closes_forwarded_stream_on_disconnect(monkeypatch, mode):
    """The slide SSE route must close the service stream it forwards."""
    import landppt.web.route_modules.slide_routes as routes

    recorder = _Recorder()

    async def slides_stream(_project_id):
        recorder.events.append(("slides:start", get_current_ai_conversation_id()))
        try:
            with ai_conversation_context("slides-session"):
                async with aclosing(recorder.provider("slides")) as provider:
                    async for _payload in provider:
                        yield 'data: {"type": "progress", "current": 1, "total": 2}\n\n'
        finally:
            recorder.events.append(("slides:closed", get_current_ai_conversation_id()))

    project = _project()

    class FakeService:
        def __init__(self):
            self.project_manager = _FakeProjectManager(project)

        def generate_slides_streaming(self, project_id):
            return slides_stream(project_id)

    monkeypatch.setattr(routes, "get_ppt_service_for_user", lambda _user_id: FakeService())

    response = await routes.stream_slides_generation("project-1", SimpleNamespace(id=1))

    await _disconnect_response(response, mode)

    assert recorder.closed("slides:closed"), recorder.names()
    assert recorder.closed("slides:provider:closed"), recorder.names()
    assert get_current_ai_conversation_id() is None


@pytest.mark.asyncio
async def test_unattended_outline_closes_stream_on_early_stage_exit(monkeypatch):
    """An exception thrown out of the unattended stage after the first chunk
    must still close the stream it is iterating."""
    from landppt.services import unattended_service as module

    recorder = _Recorder()

    async def outline_stream(_project_id, **_kwargs):
        recorder.events.append(("unattended:start", get_current_ai_conversation_id()))
        try:
            yield 'data: {"ping": true}\n\n'
            yield 'data: {"status": {"progress": 0.5, "message": "构建中"}}\n\n'
        finally:
            recorder.events.append(("unattended:closed", get_current_ai_conversation_id()))

    project = _project({"content_source": "topic", "topic": "Quarterly Review"})

    class FakeService:
        def __init__(self):
            self.project_manager = _FakeProjectManager(project)

        async def get_role_provider_async(self, _role):
            return None, {"provider": "openai"}

        def generate_outline_streaming(self, project_id, **kwargs):
            return outline_stream(project_id, **kwargs)

    service = FakeService()

    runner = object.__new__(module.UnattendedPipelineRunner)
    runner.project_id = "project-1"
    runner.user_id = 1
    runner.topic = "Quarterly Review"
    runner.config = {}
    runner._service = lambda: service

    async def set_stage(stage_id, **kwargs):
        # Simulate the stage body failing after the first streamed chunk (a
        # progress write or billing call can raise here in production).
        if kwargs.get("progress") is not None:
            raise RuntimeError("stage body failed mid-stream")

    runner._set_stage = set_stage

    async def allow_credits(*_args, **_kwargs):
        return True, 0, 100

    async def no_charge(*_args, **_kwargs):
        return None

    import landppt.web.route_modules.support as support

    monkeypatch.setattr(support, "check_credits_for_operation", allow_credits)
    monkeypatch.setattr(support, "consume_credits_for_operation", no_charge)

    with pytest.raises(RuntimeError, match="stage body failed mid-stream"):
        await runner._run_outline()

    assert recorder.closed("unattended:closed"), recorder.names()
    assert get_current_ai_conversation_id() is None


@pytest.mark.asyncio
async def test_unattended_slide_stream_cancellation_closes_bookkeeping(monkeypatch):
    """The unattended ppt stage must release its stream when the run is cancelled.

    The stage owns `service.generate_slides_streaming(...)`; a cancelled
    unattended task must close it in the same Context rather than leaving it to a
    finalizer.
    """
    from landppt.services import unattended_service as module

    closed = []
    started = asyncio.Event()

    async def slides_stream(_project_id):
        try:
            started.set()
            for page in range(1, 4):
                yield (
                    'data: {"type": "progress", "current": %d, "total": 3}\n\n' % page
                )
                await asyncio.sleep(30)
        finally:
            closed.append(get_current_ai_conversation_id())

    project = _project()
    project.outline = {
        "title": "Deck",
        "slides": [
            {"page_number": index, "title": "s%d" % index} for index in (1, 2, 3)
        ],
    }

    class FakeService:
        def __init__(self):
            self.project_manager = _FakeProjectManager(project)

        async def clear_cancel_slides_generation(self, _project_id):
            return True

        def generate_slides_streaming(self, project_id):
            return slides_stream(project_id)

    service = FakeService()

    runner = object.__new__(module.UnattendedPipelineRunner)
    runner.project_id = "project-1"
    runner.user_id = 1
    runner.topic = "Deck"
    runner.config = {}
    runner._service = lambda: service

    stages = []

    async def set_stage(stage_id, **kwargs):
        stages.append((stage_id, kwargs))

    runner._set_stage = set_stage

    task = asyncio.create_task(runner._run_ppt())
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert closed == [None], closed
    assert get_current_ai_conversation_id() is None


@pytest.mark.asyncio
async def test_research_branch_early_close_owns_research_stream(tmp_path, monkeypatch):
    """The regular-outline research branch returns as soon as it has an outline.

    That return must close the research stream it is iterating; otherwise the
    stream is abandoned and its conversation scope is unwound by a finalizer in a
    copied Context.
    """
    from landppt.services.outline.project_outline_streaming_service import (
        ProjectOutlineStreamingService,
    )
    import landppt.services.db_project_manager as db_module

    # The research branch persists the outline; keep this test off the real
    # database (an aiosqlite worker thread would outlive the test process).
    class SilentDbManager:
        async def save_project_outline(self, *_args, **_kwargs):
            return True

        async def get_project(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(db_module, "DatabaseProjectManager", SilentDbManager)

    research_closed = []
    project = _project({"network_mode": True})
    project.project_metadata = {"language": "zh", "network_mode": True}

    class ProjectManager:
        projects = {}

        async def get_project(self, _project_id):
            return project

        async def update_project_status(self, *_args, **_kwargs):
            return True

    owner = SimpleNamespace(project_manager=ProjectManager())

    async def update_stage(*_args, **_kwargs):
        return None

    owner._update_outline_generation_stage = update_stage

    service = ProjectOutlineStreamingService(owner)

    async def research_stream(_project_id, _project, _requirements, _network_mode):
        try:
            yield {"status": {"step": "research", "message": "研究完成", "progress": 1.0}}
            yield {
                "outline": {
                    "title": "From research",
                    "slides": [
                        {
                            "page_number": 1,
                            "title": "From research",
                            "content_points": ["c"],
                            "slide_type": "title",
                        }
                    ],
                },
                "llm_call_count": 2,
            }
        finally:
            research_closed.append(get_current_ai_conversation_id())

    service._run_streaming_outline_research = research_stream

    observed = []
    with ai_conversation_context("outer-workflow"):
        stream = service.generate_outline_streaming("project-1", force_regenerate=True)

        async def _drain():
            async with aclosing(stream) as owned:
                async for event in owned:
                    observed.append(event)
                    if isinstance(event, str) and '"outline"' in event:
                        break

        await asyncio.wait_for(_drain(), timeout=10)

        # closed inside the owner, before control returned to this task
        assert research_closed == ["outer-workflow"], research_closed
        assert get_current_ai_conversation_id() == "outer-workflow"

    assert get_current_ai_conversation_id() is None
    assert any('"outline"' in event for event in observed if isinstance(event, str))
