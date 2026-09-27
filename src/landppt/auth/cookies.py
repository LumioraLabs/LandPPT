"""Central session-cookie policy."""

from __future__ import annotations

from starlette.responses import Response

from ..core.config import app_config


SESSION_COOKIE_NAME = "session_id"


def set_session_cookie(
    response: Response,
    session_id: str,
    *,
    max_age: int | None,
) -> None:
    """Set a browser session cookie using the configured security policy."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=app_config.session_cookie_secure,
        samesite=app_config.session_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the session cookie with attributes matching its creation."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=app_config.session_cookie_secure,
        samesite=app_config.session_cookie_samesite,
        path="/",
    )
