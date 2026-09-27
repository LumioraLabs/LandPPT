"""
Shared helpers for extracted web route modules.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import Response
from fastapi.templating import Jinja2Templates

from ...core.config import app_config
from ...database.database import AsyncSessionLocal
from ...services.service_instances import get_ppt_service_for_user, ppt_service

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="src/landppt/web/templates")


def _apply_no_store_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _timestamp_to_datetime(timestamp: Any) -> str:
    try:
        if isinstance(timestamp, (int, float)):
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        return str(timestamp)
    except (ValueError, OSError):
        return "Invalid timestamp"


def _strftime_filter(timestamp: Any, format_string: str = "%Y-%m-%d %H:%M") -> str:
    try:
        if isinstance(timestamp, (int, float)):
            return datetime.fromtimestamp(timestamp).strftime(format_string)
        return str(timestamp)
    except (ValueError, OSError):
        return "Invalid timestamp"


templates.env.filters["timestamp_to_datetime"] = _timestamp_to_datetime
templates.env.filters["strftime"] = _strftime_filter
templates.env.globals["credits_enabled"] = app_config.enable_credits_system


def billable_operation_id(scope: str, *parts: object) -> str:
    from ...services.credits_service import billable_operation_id as _build

    return _build(scope, *parts)


def billable_work_may_start(state: str) -> bool:
    from ...services.credits_service import billable_work_may_start as _may_start

    return _may_start(state)


async def reserve_billable_operation(
    user_id: int,
    operation_id: str,
    operation_type: str,
    quantity: int = 1,
    *,
    reference_id: str | None = None,
    provider_name: str | None = None,
    ttl_seconds: int = 900,
) -> tuple[bool, str, str]:
    if not app_config.enable_credits_system:
        return True, "Credits system disabled", "skipped"
    if (provider_name or "").strip().lower() != "landppt":
        return True, "Non-billable provider", "skipped"
    try:
        from ...services.credits_service import CreditsService

        async with AsyncSessionLocal() as session:
            return await CreditsService(session).reserve_for_operation(
                user_id,
                operation_id,
                operation_type,
                quantity,
                reference_id=reference_id,
                ttl_seconds=ttl_seconds,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("Credit reservation error: %s", exc)
        return False, "Credit reservation failed", "rejected"


async def commit_billable_operation(
    operation_id: str, successful_units: int
) -> tuple[bool, str]:
    if not app_config.enable_credits_system:
        return True, "Credits system disabled"
    try:
        from ...services.credits_service import CreditsService

        async with AsyncSessionLocal() as session:
            ok, message, _charged = await CreditsService(session).commit_reservation(
                operation_id, successful_units
            )
            return ok, message
    except Exception as exc:  # noqa: BLE001
        logger.error("Credit commit error: %s", exc)
        return False, "Credit commit failed"


async def release_billable_operation(operation_id: str, reason: str = "released") -> None:
    if not app_config.enable_credits_system or not operation_id:
        return
    try:
        from ...services.credits_service import CreditsService

        async with AsyncSessionLocal() as session:
            await CreditsService(session).release_reservation(operation_id, reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("Credit release error: %s", exc)


async def acquire_billable_hold(
    user_id: int,
    operation_id: str,
    operation_type: str,
    quantity: int = 1,
    *,
    reference_id: str | None = None,
    provider_name: str | None = None,
) -> tuple[bool, str, str, str | None]:
    """Reserve before paid work.

    Returns (proceed, message, state, owned_operation_id).
    proceed is true only when this caller may run the model.
    owned_operation_id is set only when this caller created the hold.
    """
    ok, message, state = await reserve_billable_operation(
        user_id,
        operation_id,
        operation_type,
        quantity,
        reference_id=reference_id,
        provider_name=provider_name,
    )
    if not ok or not billable_work_may_start(state):
        return False, message, state, None
    return True, message, state, operation_id if state == "created" else None


async def consume_credits_for_operation(
    user_id: int,
    operation_type: str,
    quantity: int = 1,
    description: str | None = None,
    reference_id: str | None = None,
    provider_name: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, str]:
    if not app_config.enable_credits_system:
        return True, "Credits system disabled"

    if (provider_name or "").strip().lower() != "landppt":
        return True, "Non-billable provider"

    try:
        from ...services.credits_service import CreditsService

        async with AsyncSessionLocal() as session:
            credits_service = CreditsService(session)
            stable_id = operation_id or billable_operation_id(
                operation_type,
                user_id,
                reference_id or "",
                quantity,
                description or "",
            )
            ok, _message, state = await credits_service.reserve_for_operation(
                user_id,
                stable_id,
                operation_type,
                quantity,
                reference_id=reference_id,
            )
            if not ok:
                return False, _message
            if state == "committed":
                return True, _message
            committed, message, _charged = await credits_service.commit_reservation(
                stable_id, quantity
            )
            return committed, message
    except Exception as exc:  # noqa: BLE001
        logger.error("Credits consumption error: %s", exc)
        return False, f"Credits consumption failed: {exc}"


async def check_credits_for_operation(
    user_id: int,
    operation_type: str,
    quantity: int = 1,
    provider_name: str | None = None,
) -> tuple[bool, int, int]:
    if not app_config.enable_credits_system:
        return True, 0, 0

    if (provider_name or "").strip().lower() != "landppt":
        return True, 0, 0

    try:
        from ...services.credits_service import CreditsService

        async with AsyncSessionLocal() as session:
            credits_service = CreditsService(session)
            required = credits_service.get_operation_cost(operation_type, quantity)
            balance = await credits_service.get_balance(user_id)
            return balance >= required, required, balance
    except Exception as exc:  # noqa: BLE001
        logger.error("Credits check error: %s", exc)
        return False, 0, 0

