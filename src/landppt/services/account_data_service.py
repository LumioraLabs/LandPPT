"""Owner-scoped account export and deletion.

Payment orders, credit ledger rows, and reservation records are retained after
deletion so the financial audit trail still balances. Other user-owned rows and
stored artifacts are removed, while the account row is anonymized so retained
financial records keep a valid foreign key.
"""

from __future__ import annotations

import secrets
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import (
    Artifact,
    AsyncTask,
    CreditReservation,
    CreditTransaction,
    DailyCheckIn,
    GlobalMasterTemplate,
    InviteCode,
    InviteCodeUsage,
    NarrationAudio,
    Notification,
    PaymentOrder,
    PPTTemplate,
    Project,
    ProjectVersion,
    RedemptionCode,
    SlideData,
    SpeechScript,
    TodoBoard,
    TodoStage,
    User,
    UserAPIKey,
    UserConfig,
    UserMetrics,
    UserSession,
    VerificationCode,
)
from ..database.repositories import ProjectRepository


def _row_payload(row: Any, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Serialize mapped columns without traversing lazy relationships."""
    excluded = set(exclude)
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in excluded
    }


async def _rows(session: AsyncSession, model, predicate) -> list[Any]:
    return list((await session.scalars(select(model).where(predicate))).all())


async def export_user_data(session: AsyncSession, user_id: int) -> dict:
    """Return all mapped user-owned data without authentication secrets."""
    user = await session.get(User, user_id)
    if user is None:
        raise LookupError("User does not exist")

    projects = await _rows(session, Project, Project.user_id == user_id)
    project_ids = [project.project_id for project in projects]

    async def project_rows(model) -> list[Any]:
        if not project_ids:
            return []
        return await _rows(session, model, model.project_id.in_(project_ids))

    transactions = await _rows(
        session, CreditTransaction, CreditTransaction.user_id == user_id
    )
    orders = await _rows(session, PaymentOrder, PaymentOrder.user_id == user_id)
    reservations = await _rows(
        session, CreditReservation, CreditReservation.user_id == user_id
    )
    api_keys = await _rows(session, UserAPIKey, UserAPIKey.user_id == user_id)
    sessions = await _rows(session, UserSession, UserSession.user_id == user_id)
    configs = await _rows(session, UserConfig, UserConfig.user_id == user_id)
    artifacts = await _rows(session, Artifact, Artifact.user_id == user_id)
    tasks = await _rows(session, AsyncTask, AsyncTask.user_id == user_id)
    notifications = await _rows(session, Notification, Notification.user_id == user_id)
    templates = await _rows(
        session, GlobalMasterTemplate, GlobalMasterTemplate.user_id == user_id
    )
    checkins = await _rows(session, DailyCheckIn, DailyCheckIn.user_id == user_id)
    invite_usage = await _rows(
        session, InviteCodeUsage, InviteCodeUsage.user_id == user_id
    )
    redeemed_codes = await _rows(
        session, RedemptionCode, RedemptionCode.used_by == user_id
    )
    created_invites = await _rows(session, InviteCode, InviteCode.created_by == user_id)
    verification_codes = (
        await _rows(session, VerificationCode, VerificationCode.email == user.email)
        if user.email
        else []
    )
    metrics = await session.get(UserMetrics, user_id)

    return {
        "user": _row_payload(user, exclude={"password_hash"}),
        "projects": [_row_payload(project) for project in projects],
        "project_records": {
            "todo_boards": [_row_payload(row) for row in await project_rows(TodoBoard)],
            "todo_stages": [_row_payload(row) for row in await project_rows(TodoStage)],
            "versions": [
                _row_payload(row) for row in await project_rows(ProjectVersion)
            ],
            "slides": [_row_payload(row) for row in await project_rows(SlideData)],
            "templates": [_row_payload(row) for row in await project_rows(PPTTemplate)],
            "speech_scripts": [
                _row_payload(row) for row in await project_rows(SpeechScript)
            ],
            "narration_audios": [
                _row_payload(row) for row in await project_rows(NarrationAudio)
            ],
        },
        "credit_transactions": [transaction.to_dict() for transaction in transactions],
        "credit_reservations": [_row_payload(row) for row in reservations],
        # Checkout bearer URLs and provider session IDs are intentionally omitted.
        "payment_orders": [order.to_dict() for order in orders],
        # API key hashes, salts, and browser session IDs are credentials, not export data.
        "api_keys": [
            _row_payload(row, exclude={"key_hash", "salt"}) for row in api_keys
        ],
        "sessions": [_row_payload(row, exclude={"session_id"}) for row in sessions],
        # Password-typed configuration values remain redacted by to_dict().
        "configs": [row.to_dict() for row in configs],
        "metrics": _row_payload(metrics) if metrics is not None else None,
        "artifacts": [_row_payload(row) for row in artifacts],
        "async_tasks": [_row_payload(row) for row in tasks],
        "notifications": [_row_payload(row) for row in notifications],
        "global_master_templates": [_row_payload(row) for row in templates],
        "daily_checkins": [_row_payload(row) for row in checkins],
        "invite_code_usage": [_row_payload(row) for row in invite_usage],
        "redeemed_codes": [
            _row_payload(row, exclude={"code"}) for row in redeemed_codes
        ],
        "created_invites": [
            _row_payload(row, exclude={"code"}) for row in created_invites
        ],
        "verification_codes": [
            _row_payload(row, exclude={"code"}) for row in verification_codes
        ],
    }


async def delete_user_account(
    session: AsyncSession,
    user_id: int,
    password: str = "",
    confirmation: str = "",
) -> bool:
    """Delete owned content after account-type-appropriate reauthentication.

    Local accounts must provide their password. OAuth-only accounts have a
    generated, unknowable password, so they must explicitly type their exact
    username while authenticated through the OAuth-backed session.
    """
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return False
    password_ok = user.check_password(password or "")
    oauth_confirmation_ok = bool(user.oauth_provider) and secrets.compare_digest(
        str(confirmation or "").strip(), user.username
    )
    if not password_ok and not oauth_confirmation_ok:
        return False
    account_email = user.email

    # Delete the bytes before their metadata. If storage deletion fails, leave
    # the account active and the metadata available so the operation can retry.
    artifacts = await _rows(session, Artifact, Artifact.user_id == user_id)
    if artifacts:
        from .storage import get_artifact_storage

        storage = get_artifact_storage()
        for artifact in artifacts:
            if artifact.storage_backend != storage.backend_name:
                raise RuntimeError(
                    "Account artifacts use a storage backend that is not active"
                )
            await storage.delete(artifact.storage_key)

    project_ids = list(
        (
            await session.scalars(
                select(Project.project_id).where(Project.user_id == user_id)
            )
        ).all()
    )
    repository = ProjectRepository(session)
    for project_id in project_ids:
        await repository.delete(
            project_id, user_id=user_id, delete_artifact_bytes=False
        )

    # Remove user-owned non-financial rows that are not covered by project deletion.
    for model in (
        Artifact,
        AsyncTask,
        Notification,
        GlobalMasterTemplate,
        DailyCheckIn,
        InviteCodeUsage,
        UserConfig,
        UserMetrics,
    ):
        await session.execute(delete(model).where(model.user_id == user_id))
    if account_email:
        await session.execute(
            delete(VerificationCode).where(VerificationCode.email == account_email)
        )
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
    await session.execute(delete(UserAPIKey).where(UserAPIKey.user_id == user_id))
    user.username = f"deleted-{user.id}"
    user.email = None
    user.phone = None
    user.avatar = None
    user.github_id = None
    user.linuxdo_id = None
    user.oauth_provider = None
    user.last_login = None
    user.register_ip = None
    user.last_login_ip = None
    user.registration_channel = None
    user.invite_code_id = None
    user.is_active = False
    user.set_password(secrets.token_urlsafe(32))
    await session.commit()
    return True
