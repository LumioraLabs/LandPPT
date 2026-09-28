import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _HeaderMap(dict):
    def __init__(self, values: dict | None = None):
        super().__init__()
        for key, value in (values or {}).items():
            self[str(key).lower()] = str(value)

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class _FakeRequest:
    def __init__(self, path: str, headers: dict | None = None, cookies: dict | None = None):
        self.headers = _HeaderMap(headers)
        self.cookies = dict(cookies or {})
        self.state = SimpleNamespace()
        self.url = SimpleNamespace(path=path, scheme="http", netloc="testserver")
        self.base_url = "http://testserver/"


class _FakeCache:
    def __init__(self, payload: dict | None):
        self.payload = dict(payload or {})
        self.is_connected = True
        self.refreshed: list[tuple[str, int | None]] = []
        self.deleted: list[str] = []
        self.reads: list[str] = []

    async def get_session(self, session_id: str):
        self.reads.append(session_id)
        return dict(self.payload) if self.payload else None

    async def refresh_session(self, session_id: str, ttl: int | None = None):
        self.refreshed.append((session_id, ttl))
        return True

    async def delete_session(self, session_id: str):
        self.deleted.append(session_id)
        return True


def _create_db():
    from landppt.database.models import Base, User, UserSession

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, UserSession.__table__])
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    return SessionLocal()


def _create_user(db, username: str, email: str):
    from landppt.database.models import User

    user = User(username=username, email=email, is_admin=False, is_active=True, credits_balance=0)
    user.set_password("pw")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_auth_middleware_does_not_trust_cache_after_database_revocation(monkeypatch):
    pytest.importorskip("fastapi")
    import landppt.auth.middleware as auth_middleware_module
    import landppt.services.cache_service as cache_service_module
    from landppt.auth.auth_service import AuthService
    from landppt.auth.middleware import AuthMiddleware
    from landppt.database.models import UserSession

    db = _create_db()
    try:
        user = _create_user(db, "cache-user", "cache@example.com")
        auth = AuthService()
        payload = auth.build_session_cache_payload(user, expires_at=time.time() + 300)
        fake_cache = _FakeCache(payload)
        db.add(
            UserSession(
                user_id=user.id,
                session_id="cached-session",
                expires_at=time.time() + 300,
                is_active=False,
            )
        )
        db.commit()

        async def _fake_get_cache_service():
            return fake_cache

        def _get_db():
            yield db

        monkeypatch.setattr(cache_service_module, "get_cache_service", _fake_get_cache_service)
        monkeypatch.setattr(auth_middleware_module, "get_db", _get_db)

        middleware = AuthMiddleware()
        request = _FakeRequest(path="/dashboard", cookies={"session_id": "cached-session"})

        async def _call_next(req):
            raise AssertionError("a revoked session must not reach the protected route")

        response = asyncio.run(middleware(request, _call_next))

        assert response.status_code == 302
        assert response.headers["location"] == "/auth/login"
        assert getattr(request.state, "user", None) is None
        assert fake_cache.reads == []
    finally:
        db.close()


def test_update_user_password_supports_cached_user_objects():
    from landppt.auth.auth_service import AuthService
    from landppt.database.models import User

    db = _create_db()
    try:
        user = _create_user(db, "pw-user", "pw@example.com")
        auth = AuthService()
        payload = auth.build_session_cache_payload(user, expires_at=time.time() + 300)
        cached_user = auth.user_from_session_cache(payload)

        assert cached_user is not None
        assert auth.update_user_password(db, cached_user, "new-password") is True

        db.expire_all()
        persisted = db.query(User).filter(User.id == user.id).first()
        assert persisted is not None
        assert persisted.check_password("new-password")
    finally:
        db.close()
