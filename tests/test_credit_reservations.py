"""Reserve, commit, and release drive the shipped billable workflow."""

import asyncio

import pytest
from sqlalchemy import select

from test_billing_service import _billing_database, _create_user


@pytest.mark.asyncio
async def test_billable_work_commits_success_and_releases_failure(tmp_path):
    from landppt.database.models import CreditReservation, User
    from landppt.services.credits_service import CreditsService, run_billable_work

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        from landppt.database.models import CreditReservation as Reservation

        await connection.run_sync(
            lambda sync_connection: Reservation.__table__.create(sync_connection)
        )
    user = await _create_user(maker)
    async with maker() as session:
        await session.execute(
            User.__table__.update()
            .where(User.id == user.id)
            .values(credits_balance=20)
        )
        await session.commit()
    calls = {"count": 0}

    async def work():
        calls["count"] += 1
        return {"units": 1}

    try:
        result = await run_billable_work(
            user_id=user.id,
            operation_id="edit-1",
            operation_type="ai_edit",
            quantity=1,
            work=work,
            successful_units=lambda payload: payload["units"],
            session_factory=maker,
        )
        assert result["state"] == "committed"
        assert calls["count"] == 1
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert balance == 17

        async def fail():
            calls["count"] += 1
            raise RuntimeError("model failed")

        with pytest.raises(RuntimeError, match="model failed"):
            await run_billable_work(
                user_id=user.id,
                operation_id="edit-2",
                operation_type="ai_edit",
                quantity=1,
                work=fail,
                successful_units=lambda payload: 1,
                session_factory=maker,
            )
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            released = await session.scalar(
                select(CreditReservation.status).where(
                    CreditReservation.operation_id == "edit-2"
                )
            )
        assert balance == 17
        assert released == "released"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_retry_of_one_operation_runs_work_once(tmp_path):
    from landppt.database.models import User
    from landppt.services.credits_service import run_billable_work

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        from landppt.database.models import CreditReservation as Reservation

        await connection.run_sync(
            lambda sync_connection: Reservation.__table__.create(sync_connection)
        )
    user = await _create_user(maker)
    async with maker() as session:
        await session.execute(
            User.__table__.update().where(User.id == user.id).values(credits_balance=30)
        )
        await session.commit()
    calls = {"count": 0}

    async def work():
        calls["count"] += 1
        await asyncio.sleep(0.05)
        return {"units": 2}

    try:
        await asyncio.gather(
            *[
                run_billable_work(
                    user_id=user.id,
                    operation_id="slides-7",
                    operation_type="slide_generation",
                    quantity=2,
                    work=work,
                    successful_units=lambda payload: payload["units"],
                    session_factory=maker,
                )
                for _ in range(2)
            ]
        )
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert calls["count"] == 1
        assert balance == 20
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_reservation_is_released_after_worker_death(tmp_path):
    from landppt.database.models import User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        from landppt.database.models import CreditReservation as Reservation

        await connection.run_sync(
            lambda sync_connection: Reservation.__table__.create(sync_connection)
        )
    user = await _create_user(maker)
    async with maker() as session:
        await session.execute(
            User.__table__.update().where(User.id == user.id).values(credits_balance=10)
        )
        await session.commit()
    try:
        async with maker() as session:
            ok, _message, state = await CreditsService(session).reserve_for_operation(
                user.id,
                "orphan-job",
                "template_generation",
                1,
                ttl_seconds=1,
            )
            assert ok and state == "created"
        async with maker() as session:
            from landppt.database.models import CreditReservation

            reservation = await session.scalar(
                select(CreditReservation).where(
                    CreditReservation.operation_id == "orphan-job"
                )
            )
            reservation.expires_at = 1
            await session.commit()
        async with maker() as session:
            released = await CreditsService(session).release_expired_reservations(
                now=10
            )
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert released == 1
        assert balance == 10
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_release_of_the_same_operation_restores_the_balance(tmp_path):
    """reserve/release/reserve/release must not collide on the release idempotency key."""
    from landppt.database.models import User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        from landppt.database.models import CreditReservation as Reservation

        await connection.run_sync(
            lambda sync_connection: Reservation.__table__.create(sync_connection)
        )
    user = await _create_user(maker)
    async with maker() as session:
        await session.execute(
            User.__table__.update().where(User.id == user.id).values(credits_balance=20)
        )
        await session.commit()
    try:
        for _attempt in range(2):
            async with maker() as session:
                ok, _message, state = await CreditsService(session).reserve_for_operation(
                    user.id, "edit-again", "ai_edit", 1
                )
                assert ok and state == "created"
            async with maker() as session:
                ok, _message = await CreditsService(session).release_reservation(
                    "edit-again", "client cancelled"
                )
                assert ok
        async with maker() as session:
            from landppt.database.models import CreditReservation

            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            status = await session.scalar(
                select(CreditReservation.status).where(
                    CreditReservation.operation_id == "edit-again"
                )
            )
        assert balance == 20
        assert status == "released"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_sweep_releases_a_second_hold_of_the_same_operation(tmp_path, monkeypatch):
    from landppt.database.models import CreditReservation, User
    from landppt.services.credits_service import (
        CreditsService,
        sweep_expired_credit_reservations,
    )

    engine, maker = await _billing_database(tmp_path)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: CreditReservation.__table__.create(sync_connection)
        )
    user = await _create_user(maker)
    async with maker() as session:
        await session.execute(
            User.__table__.update().where(User.id == user.id).values(credits_balance=20)
        )
        await session.commit()
    monkeypatch.setattr("landppt.database.database.AsyncSessionLocal", maker)
    try:
        async with maker() as session:
            ok, _message, state = await CreditsService(session).reserve_for_operation(
                user.id, "edit-expire", "ai_edit", 1
            )
            assert ok and state == "created"
        async with maker() as session:
            ok, _message = await CreditsService(session).release_reservation(
                "edit-expire", "first"
            )
            assert ok
        async with maker() as session:
            ok, _message, state = await CreditsService(session).reserve_for_operation(
                user.id, "edit-expire", "ai_edit", 1
            )
            assert ok and state == "created"
        async with maker() as session:
            reservation = await session.scalar(
                select(CreditReservation).where(
                    CreditReservation.operation_id == "edit-expire"
                )
            )
            reservation.expires_at = 1
            await session.commit()

        task = asyncio.create_task(sweep_expired_credit_reservations(interval_seconds=0.01))
        try:
            restored = False
            for _ in range(40):
                async with maker() as session:
                    balance = await session.scalar(
                        select(User.credits_balance).where(User.id == user.id)
                    )
                if balance == 20:
                    restored = True
                    break
                await asyncio.sleep(0.02)
            assert restored
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await engine.dispose()
