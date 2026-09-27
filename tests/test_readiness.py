"""Readiness reports dependency failures and liveness does not."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

from landppt.core.readiness import assess_readiness
from landppt.main import health_check, readiness_check


@pytest.mark.asyncio
async def test_liveness_does_not_consult_dependencies():
    payload = await health_check()
    assert payload["status"] == "healthy"
    assert "dependencies" not in payload


@pytest.mark.asyncio
async def test_readiness_fails_when_storage_cache_or_database_is_down(tmp_path, monkeypatch):
    missing = tmp_path / "missing-artifacts"
    config = SimpleNamespace(
        cache_backend="valkey",
        task_execution_mode="queue",
        valkey_url="redis://127.0.0.1:1/0",
        storage_backend="local",
        local_storage_root=str(missing),
    )

    import landppt.database.database as database

    class _BrokenEngine:
        def connect(self):
            raise ConnectionError("database unavailable")

    monkeypatch.setattr(database, "async_engine", _BrokenEngine())

    failures = await assess_readiness(config)
    assert failures == ["database", "cache", "storage"]

    monkeypatch.setattr(
        "landppt.main.assess_readiness",
        assess_readiness,
        raising=False,
    )

    async def _failed():
        return ["database", "cache", "storage"]

    import landppt.core.readiness as readiness

    monkeypatch.setattr(readiness, "assess_readiness", _failed)
    response = await readiness_check()
    assert response.status_code == 503
    assert response.body
    body = response.body.decode()
    assert "database" in body and "cache" in body and "storage" in body


@pytest.mark.asyncio
async def test_local_storage_readiness_accepts_a_writable_directory(tmp_path, monkeypatch):
    async def _database_ok() -> bool:
        return True

    monkeypatch.setattr("landppt.core.readiness._database_is_ready", _database_ok)
    config = SimpleNamespace(
        cache_backend="memory",
        task_execution_mode="inline",
        storage_backend="local",
        local_storage_root=str(tmp_path),
    )
    failures = await assess_readiness(config)
    assert failures == []


def _silence_startup(monkeypatch) -> None:
    import landppt.main as main_module

    async def fake_startup():
        return True

    monkeypatch.setattr(main_module, "run_startup_initialization", fake_startup)


def test_unauthenticated_ready_reports_down_dependencies_without_redirect(monkeypatch):
    import landppt.main as main_module

    _silence_startup(monkeypatch)

    async def down():
        return ["database"]

    monkeypatch.setattr("landppt.core.readiness.assess_readiness", down)
    client = TestClient(main_module.app, follow_redirects=False)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"] == ["database"]

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 302
    assert dashboard.headers["location"].endswith("/auth/login")


def test_ready_with_disallowed_host_is_not_successful(monkeypatch):
    import landppt.main as main_module

    _silence_startup(monkeypatch)

    async def down():
        return ["storage"]

    monkeypatch.setattr("landppt.core.readiness.assess_readiness", down)
    guarded = TrustedHostMiddleware(main_module.app, allowed_hosts=["slides.example.com"])
    client = TestClient(guarded, follow_redirects=False)

    rejected = client.get("/ready", headers={"host": "10.0.0.8"})
    assert rejected.status_code == 400

    allowed = client.get("/ready", headers={"host": "slides.example.com"})
    assert allowed.status_code == 503
    assert allowed.json()["dependencies"] == ["storage"]
