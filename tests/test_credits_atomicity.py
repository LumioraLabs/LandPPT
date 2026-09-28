import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


async def _database(tmp_path):
    from landppt.database.models import (
        Base,
        CreditTransaction,
        RedemptionCode,
        User,
        UserMetrics,
    )

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'credits.db'}",
        connect_args={"timeout": 10},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    UserMetrics.__table__,
                    CreditTransaction.__table__,
                    RedemptionCode.__table__,
                ],
            )
        )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, maker


async def _create_user(maker, username, balance):
    from landppt.database.models import User

    async with maker() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            is_active=True,
            is_admin=False,
            credits_balance=balance,
        )
        user.set_password("a sufficiently long password")
        session.add(user)
        await session.commit()
        return user.id


@pytest.mark.asyncio
async def test_concurrent_consumption_cannot_overspend(tmp_path):
    from landppt.database.models import CreditTransaction, User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _database(tmp_path)
    try:
        user_id = await _create_user(maker, "spender", 5)

        async def consume(key):
            async with maker() as session:
                return await CreditsService(session).consume_credits(
                    user_id,
                    "slide_generation",
                    idempotency_key=key,
                )

        results = await asyncio.gather(consume("consume-one"), consume("consume-two"))
        assert [success for success, _ in results].count(True) == 1

        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user_id)
            )
            transaction_count = await session.scalar(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.user_id == user_id
                )
            )
        assert balance == 0
        assert transaction_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credit_idempotency_prevents_double_recharge(tmp_path):
    from landppt.database.models import CreditTransaction, User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _database(tmp_path)
    try:
        user_id = await _create_user(maker, "recharge", 0)
        for _ in range(2):
            async with maker() as session:
                success, _ = await CreditsService(session).add_credits(
                    user_id,
                    25,
                    "recharge",
                    "paid order",
                    reference_id="order-1",
                    idempotency_key="payment:order-1",
                )
                assert success is True

        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user_id)
            )
            transaction_count = await session.scalar(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.user_id == user_id
                )
            )
        assert balance == 25
        assert transaction_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redemption_claim_and_credit_are_one_atomic_operation(tmp_path):
    from landppt.database.models import CreditTransaction, RedemptionCode, User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _database(tmp_path)
    try:
        creator_id = await _create_user(maker, "creator", 0)
        first_user_id = await _create_user(maker, "first", 0)
        second_user_id = await _create_user(maker, "second", 0)
        async with maker() as session:
            session.add(
                RedemptionCode(
                    code="ONE-TIME",
                    credits_amount=40,
                    created_by=creator_id,
                )
            )
            await session.commit()

        async def redeem(user_id):
            async with maker() as session:
                return await CreditsService(session).redeem_code(user_id, "one-time")

        results = await asyncio.gather(redeem(first_user_id), redeem(second_user_id))
        assert [success for success, _ in results].count(True) == 1

        async with maker() as session:
            balances = list(
                (
                    await session.scalars(
                        select(User.credits_balance)
                        .where(User.id.in_([first_user_id, second_user_id]))
                        .order_by(User.id)
                    )
                ).all()
            )
            transaction_count = await session.scalar(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.transaction_type == "redemption"
                )
            )
            code = await session.scalar(
                select(RedemptionCode).where(RedemptionCode.code == "ONE-TIME")
            )
        assert sorted(balances) == [0, 40]
        assert transaction_count == 1
        assert code is not None and code.is_used is True
        assert code.used_by in {first_user_id, second_user_id}
    finally:
        await engine.dispose()


def test_credit_migration_is_registered():
    from landppt.database.migrations import migration_manager

    assert any(
        migration["version"] == "019"
        and migration["name"] == "add_credit_transaction_idempotency"
        for migration in migration_manager.migrations
    )
