"""Owner-scoped account export and deletion.

Payment orders and credit ledger rows are retained after deletion so the
balance and the audit history still match. Profile, sessions, API keys, and
owned projects are removed or anonymized.
"""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.models import PaymentOrder, Project, User, UserAPIKey, UserSession
from ..database.repositories import CreditTransactionRepository, ProjectRepository


async def export_user_data(session: AsyncSession, user_id: int) -> dict:
    """Return one user's account data. Password hashes and checkout URLs stay out."""
    user = await session.get(User, user_id)
    if user is None:
        raise LookupError("User does not exist")
    projects = (
        await session.scalars(select(Project).where(Project.user_id == user_id))
    ).all()
    transactions, _total = await CreditTransactionRepository(
        session
    ).get_user_transactions(user_id, page=1, page_size=10000)
    orders = (
        await session.scalars(
            select(PaymentOrder).where(PaymentOrder.user_id == user_id)
        )
    ).all()
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_active": user.is_active,
            "credits_balance": user.credits_balance,
            "created_at": user.created_at,
        },
        "projects": [
            {
                "project_id": project.project_id,
                "title": project.title,
                "topic": project.topic,
                "status": project.status,
            }
            for project in projects
        ],
        "credit_transactions": [transaction.to_dict() for transaction in transactions],
        "payment_orders": [
            {
                "id": order.id,
                "pack_id": order.pack_id,
                "credits_amount": order.credits_amount,
                "amount_minor": order.amount_minor,
                "currency": order.currency,
                "status": order.status,
                "provider_request_id": order.provider_request_id,
            }
            for order in orders
        ],
    }


async def delete_user_account(
    session: AsyncSession, user_id: int, password: str
) -> bool:
    """Delete owned content after password reauthentication. Ledger rows stay."""
    user = await session.get(User, user_id)
    if user is None or not user.is_active or not user.check_password(password or ""):
        return False
    project_ids = list(
        (
            await session.scalars(
                select(Project.project_id).where(Project.user_id == user_id)
            )
        ).all()
    )
    repository = ProjectRepository(session)
    for project_id in project_ids:
        await repository.delete(project_id, user_id=user_id)
    await session.execute(delete(UserSession).where(UserSession.user_id == user_id))
    await session.execute(delete(UserAPIKey).where(UserAPIKey.user_id == user_id))
    user.username = f"deleted-{user.id}"
    user.email = None
    user.phone = None
    user.avatar = None
    user.github_id = None
    user.linuxdo_id = None
    user.is_active = False
    user.set_password(secrets.token_urlsafe(32))
    await session.commit()
    return True
