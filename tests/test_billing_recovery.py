"""Webhook recovery drives BillingService.process_stripe_event."""

import asyncio
import time

import pytest
from sqlalchemy import func, select

from test_billing_service import (
    _billing_database,
    _checkout_event,
    _configure,
    _create_user,
)


async def _order(maker, user):
    from landppt.database.models import PaymentOrder

    async with maker() as session:
        order = PaymentOrder(
            user_id=user.id,
            checkout_request_key="b" * 64,
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
        return order


@pytest.mark.asyncio
async def test_duplicate_deliveries_grant_credits_once(tmp_path, monkeypatch):
    from landppt.database.models import CreditTransaction, User
    from landppt.services.billing_service import BillingInProgress, BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order = await _order(maker, user)
    event = _checkout_event(order, "evt_parallel")
    try:
        async def deliver():
            async with maker() as session:
                return await BillingService(session).process_stripe_event(event, "d" * 64)

        results = await asyncio.gather(deliver(), deliver(), return_exceptions=True)
        follow_up = await deliver()
        assert follow_up == "duplicate"
        assert any(result == "processed" for result in results)
        assert all(
            result in {"processed", "duplicate"} or isinstance(result, BillingInProgress)
            for result in results
        )
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            grants = await session.scalar(select(func.count(CreditTransaction.id)))
        assert balance == 250
        assert grants == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_race_loser_does_not_acknowledge_when_winner_fails(tmp_path, monkeypatch):
    from landppt.database.models import User
    from landppt.services.billing_service import BillingInProgress, BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order = await _order(maker, user)
    event = _checkout_event(order, "evt_race")
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail_after_claim(self, obj, livemode, provider_request_id=None):
        started.set()
        await release.wait()
        raise RuntimeError("winner failed after claim")

    monkeypatch.setattr(BillingService, "_fulfill_checkout", fail_after_claim)
    try:
        async def winner():
            async with maker() as session:
                await BillingService(session).process_stripe_event(event, "e" * 64)

        async def loser():
            await started.wait()
            async with maker() as session:
                return await BillingService(session).process_stripe_event(event, "e" * 64)

        winner_task = asyncio.create_task(winner())
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(BillingInProgress):
            await loser()
        release.set()
        with pytest.raises(RuntimeError, match="winner failed after claim"):
            await winner_task
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert balance == 0
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_processing_receipt_is_finished_by_a_later_delivery(
    tmp_path, monkeypatch
):
    from landppt.database.models import PaymentWebhookEvent, User
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order = await _order(maker, user)
    event = _checkout_event(order, "evt_stale")
    try:
        async with maker() as session:
            session.add(
                PaymentWebhookEvent(
                    provider="stripe",
                    event_id="evt_stale",
                    event_type="checkout.session.completed",
                    payload_sha256="f" * 64,
                    status="processing",
                    attempts=1,
                    lease_expires_at=time.time() - 30,
                )
            )
            await session.commit()
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(event, "f" * 64)
                == "processed"
            )
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            receipt = await session.scalar(
                select(PaymentWebhookEvent).where(
                    PaymentWebhookEvent.event_id == "evt_stale"
                )
            )
        assert balance == 250
        assert receipt.status == "completed"
        assert receipt.provider_request_id is None or isinstance(
            receipt.provider_request_id, str
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delayed_payment_and_mode_isolation(tmp_path, monkeypatch, caplog):
    from landppt.database.models import PaymentOrder, PaymentWebhookEvent, User
    from landppt.services.billing_service import BillingRequestError, BillingService

    _configure(monkeypatch)
    monkeypatch.setattr(
        "landppt.core.config.app_config.stripe_secret_key", "sk_test_do_not_print"
    )
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order = await _order(maker, user)
    unpaid = _checkout_event(order, "evt_unpaid")
    unpaid["data"]["object"]["payment_status"] = "unpaid"
    succeeded = _checkout_event(order, "evt_delayed_paid")
    succeeded["type"] = "checkout.session.async_payment_succeeded"
    succeeded["request"] = {"id": "req_support_123"}
    failed = _checkout_event(order, "evt_delayed_failed")
    failed["type"] = "checkout.session.async_payment_failed"
    live = _checkout_event(order, "evt_live")
    live["livemode"] = True
    try:
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(unpaid, "1" * 64)
                == "processed"
            )
        async with maker() as session:
            saved = await session.get(PaymentOrder, order.id)
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert saved.status == "awaiting_payment"
        assert balance == 0

        async with maker() as session:
            with pytest.raises(BillingRequestError, match="mode"):
                await BillingService(session).process_stripe_event(live, "2" * 64)
        assert "sk_test_do_not_print" not in caplog.text
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            live_receipt = await session.scalar(
                select(PaymentWebhookEvent).where(
                    PaymentWebhookEvent.event_id == "evt_live"
                )
            )
        assert balance == 0
        assert live_receipt.status == "failed"
        assert "sk_test_do_not_print" not in (live_receipt.last_error or "")

        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(succeeded, "3" * 64)
                == "processed"
            )
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(failed, "4" * 64)
                == "processed"
            )
            saved = await session.get(PaymentOrder, order.id)
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
            receipt = await session.scalar(
                select(PaymentWebhookEvent).where(
                    PaymentWebhookEvent.event_id == "evt_delayed_paid"
                )
            )
        assert balance == 250
        assert saved.status == "fulfilled"
        assert saved.provider_request_id == "req_support_123"
        assert receipt.provider_request_id == "req_support_123"
        assert "sk_test" not in (receipt.summary_json or "")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_repairs_a_failed_receipt_from_its_summary(
    tmp_path, monkeypatch
):
    from landppt.database.models import User
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    order = await _order(maker, user)
    event = _checkout_event(order, "evt_repair")
    event["request"] = {"id": "req_repair_1"}
    calls = {"count": 0}
    original = BillingService._fulfill_checkout

    async def fail_once(self, obj, livemode, provider_request_id=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("provider timeout")
        return await original(self, obj, livemode, provider_request_id)

    monkeypatch.setattr(BillingService, "_fulfill_checkout", fail_once)
    try:
        async with maker() as session:
            with pytest.raises(RuntimeError, match="provider timeout"):
                await BillingService(session).process_stripe_event(event, "9" * 64)
        async with maker() as session:
            result = await BillingService(session).reconcile_stored_events()
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user.id)
            )
        assert result["repaired"] == 1
        assert balance == 250
        assert "sk_test" not in str(result)
    finally:
        await engine.dispose()
