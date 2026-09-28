"""Spent-credit refunds, partial refunds, disputes, and admin adjustments."""

import pytest
from sqlalchemy import func, select

from test_billing_service import (
    _billing_database,
    _checkout_event,
    _configure,
    _create_user,
)


async def _fulfilled_order(maker, user):
    from landppt.database.models import PaymentOrder
    from landppt.services.billing_service import BillingService

    async with maker() as session:
        order = PaymentOrder(
            user_id=user.id,
            checkout_request_key="c" * 64,
            provider_checkout_session_id="cs_test_checkout",
            pack_id="starter",
            pack_name="Starter",
            credits_amount=250,
            amount_minor=1000,
            charged_amount_minor=1000,
            currency="usd",
            livemode=False,
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)
        order_id = order.id
    event = _checkout_event(order, "evt_refund_base")
    async with maker() as session:
        await BillingService(session).process_stripe_event(event, "a" * 64)
    return order_id


@pytest.mark.asyncio
async def test_spent_credits_can_go_negative_and_refunds_are_idempotent(
    tmp_path, monkeypatch
):
    from landppt.database.models import CreditTransaction, PaymentOrder, User
    from landppt.services.billing_service import BillingService
    from landppt.services.credits_service import CreditsService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order_id = await _fulfilled_order(maker, user)
    try:
        async with maker() as session:
            ok, _message = await CreditsService(session).consume_credits(
                user.id, "ai_other", 200, idempotency_key="spend-200"
            )
            assert ok
        refund = {
            "id": "evt_refund_spent",
            "type": "charge.refunded",
            "livemode": False,
            "data": {
                "object": {
                    "payment_intent": "pi_test_payment",
                    "amount_refunded": 1000,
                    "refunded": True,
                }
            },
        }
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(refund, "b" * 64)
                == "processed"
            )
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(refund, "b" * 64)
                == "duplicate"
            )
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            ledger = await session.scalar(
                select(func.coalesce(func.sum(CreditTransaction.amount), 0)).where(
                    CreditTransaction.user_id == user.id
                )
            )
            order = await session.get(PaymentOrder, order_id)
        assert balance == -200
        assert int(ledger) == balance
        assert order.credits_reversed == 250
        assert order.status == "refunded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_refund_floors_credit_reversal(tmp_path, monkeypatch):
    from landppt.database.models import PaymentOrder, User
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order_id = await _fulfilled_order(maker, user)
    tiny = {
        "id": "evt_refund_one_cent",
        "type": "charge.refunded",
        "livemode": False,
        "data": {
            "object": {
                "payment_intent": "pi_test_payment",
                "amount_refunded": 1,
                "refunded": False,
            }
        },
    }
    half = {
        "id": "evt_refund_half_again",
        "type": "charge.refunded",
        "livemode": False,
        "data": {
            "object": {
                "payment_intent": "pi_test_payment",
                "amount_refunded": 500,
                "refunded": False,
            }
        },
    }
    try:
        async with maker() as session:
            await BillingService(session).process_stripe_event(tiny, "c" * 64)
        async with maker() as session:
            await BillingService(session).process_stripe_event(half, "d" * 64)
            order = await session.get(PaymentOrder, order_id)
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert order.credits_reversed == 125
        assert order.status == "partially_refunded"
        assert balance == 125
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispute_reversal_and_win_restore_once(tmp_path, monkeypatch):
    from landppt.database.models import PaymentOrder, User
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order_id = await _fulfilled_order(maker, user)
    opened = {
        "id": "evt_dispute_open",
        "type": "charge.dispute.created",
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test",
                "payment_intent": "pi_test_payment",
                "amount": 1000,
                "status": "needs_response",
            }
        },
    }
    repeated = dict(opened)
    repeated["id"] = "evt_dispute_funds"
    repeated["type"] = "charge.dispute.funds_withdrawn"
    won = {
        "id": "evt_dispute_won",
        "type": "charge.dispute.closed",
        "livemode": False,
        "data": {
            "object": {
                "id": "dp_test",
                "payment_intent": "pi_test_payment",
                "amount": 1000,
                "status": "won",
            }
        },
    }
    try:
        async with maker() as session:
            await BillingService(session).process_stripe_event(opened, "e" * 64)
        async with maker() as session:
            await BillingService(session).process_stripe_event(repeated, "f" * 64)
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert balance == 0
        async with maker() as session:
            await BillingService(session).process_stripe_event(won, "g" * 64)
            await BillingService(session).process_stripe_event(won, "g" * 64)
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            order = await session.get(PaymentOrder, order_id)
        assert balance == 250
        assert order.status == "fulfilled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admin_adjustment_is_audited_and_idempotent(tmp_path, monkeypatch):
    from landppt.database.models import CreditTransaction, User
    from landppt.services.credits_service import CreditsService

    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    try:
        async with maker() as session:
            admin = User(
                username="ops",
                email="ops@example.com",
                credits_balance=0,
                is_active=True,
                is_admin=True,
            )
            admin.set_password("another long password")
            session.add(admin)
            await session.commit()
            await session.refresh(admin)
            admin_id = admin.id
        secret = "sk_test_must_not_leak"
        async with maker() as session:
            ok, message = await CreditsService(session).adjust_credits(
                user.id,
                -30,
                reason="goodwill correction",
                actor_user_id=admin_id,
                idempotency_key="adjust-1",
            )
            assert ok
            assert secret not in message
        async with maker() as session:
            ok, message = await CreditsService(session).adjust_credits(
                user.id,
                -30,
                reason="goodwill correction",
                actor_user_id=admin_id,
                idempotency_key="adjust-1",
            )
            assert ok
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            rows = (
                await session.scalars(
                    select(CreditTransaction).where(
                        CreditTransaction.transaction_type == "admin_adjust"
                    )
                )
            ).all()
        assert balance == -30
        assert len(rows) == 1
        assert f"actor={admin_id}" in rows[0].description
        assert rows[0].idempotency_key == "adjust-1"
        assert secret not in rows[0].description
    finally:
        await engine.dispose()
