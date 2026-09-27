import hashlib
import time
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _create_db():
    from landppt.database.models import Base, User, UserSession

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine, tables=[User.__table__, UserSession.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_argon2id_password_hash_and_legacy_verification():
    from landppt.auth.passwords import (
        hash_password,
        password_hash_needs_update,
        verify_password,
    )

    current = hash_password("correct horse battery staple")
    assert current.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", current) is True
    assert verify_password("incorrect", current) is False
    assert password_hash_needs_update(current) is False

    legacy = hashlib.sha256(b"legacy-password").hexdigest()
    assert verify_password("legacy-password", legacy) is True
    assert verify_password("incorrect", legacy) is False
    assert password_hash_needs_update(legacy) is True


def test_successful_login_transparently_migrates_legacy_password_hash():
    from landppt.auth.auth_service import AuthService
    from landppt.database.models import User

    db = _create_db()
    try:
        legacy = hashlib.sha256(b"legacy-password").hexdigest()
        user = User(
            username="legacy-user",
            email="legacy@example.com",
            password_hash=legacy,
            is_active=True,
            is_admin=False,
            credits_balance=0,
        )
        db.add(user)
        db.commit()

        authenticated = AuthService().authenticate_user(
            db, user.username, "legacy-password"
        )
        assert authenticated is not None
        assert authenticated.password_hash.startswith("$argon2id$")
        assert authenticated.check_password("legacy-password") is True
    finally:
        db.close()


def test_session_cache_never_contains_password_material():
    from landppt.auth.auth_service import AuthService
    from landppt.database.models import User

    user = User(
        username="cache-user",
        email="cache@example.com",
        is_active=True,
        is_admin=False,
        credits_balance=0,
    )
    user.id = 7
    user.set_password("a sufficiently long password")

    auth = AuthService()
    payload = auth.build_session_cache_payload(user, time.time() + 300)
    assert "password_hash" not in payload

    cached_user = auth.user_from_session_cache(payload)
    assert cached_user is not None
    assert cached_user.password_hash == ""


def test_password_change_revokes_all_existing_sessions():
    from landppt.auth.auth_service import AuthService
    from landppt.database.models import User, UserSession

    db = _create_db()
    try:
        user = User(
            username="password-user",
            email="password@example.com",
            is_active=True,
            is_admin=False,
            credits_balance=0,
        )
        user.set_password("old-password-value")
        db.add(user)
        db.commit()
        db.refresh(user)

        session = UserSession(
            session_id="existing-session",
            user_id=user.id,
            expires_at=time.time() + 300,
            is_active=True,
        )
        db.add(session)
        db.commit()

        assert (
            AuthService().update_user_password(db, user, "new-password-value") is True
        )
        db.expire_all()
        persisted = db.query(UserSession).filter_by(session_id="existing-session").one()
        assert persisted.is_active is False
    finally:
        db.close()


def test_oauth_redirects_are_restricted_to_local_paths():
    from landppt.auth.security import safe_local_redirect

    assert safe_local_redirect("/projects/one?tab=slides") == "/projects/one?tab=slides"
    assert safe_local_redirect("https://attacker.example/phish") == "/dashboard"
    assert safe_local_redirect("//attacker.example/phish") == "/dashboard"
    assert safe_local_redirect("/\\attacker.example") == "/dashboard"
    assert (
        safe_local_redirect("/dashboard\r\nLocation: https://attacker.example")
        == "/dashboard"
    )


def test_proxy_headers_are_used_only_for_trusted_direct_peers(monkeypatch):
    from landppt.auth.security import get_client_ip
    from landppt.core.config import app_config

    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"x-forwarded-for": "198.51.100.20"},
    )
    monkeypatch.setattr(app_config, "trusted_proxy_cidrs", "10.0.0.0/8")
    assert get_client_ip(request) == "203.0.113.10"

    request.client.host = "10.1.2.3"
    assert get_client_ip(request) == "198.51.100.20"
