import json
import time

import pytest
import stripe
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


CATALOG = json.dumps(
    {
        "starter": {
            "name": "Starter",
            "credits": 250,
            "amount_minor": 1000,
            "currency": "usd",
            "stripe_price_id": "price_starter",
        }
    }
)


async def _billing_database(tmp_path):
    from landppt.database.models import (
        Base,
        CreditTransaction,
        PaymentOrder,
        PaymentWebhookEvent,
        User,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'billing.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    CreditTransaction.__table__,
                    PaymentOrder.__table__,
                    PaymentWebhookEvent.__table__,
                ],
            )
        )
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


async def _create_user(maker):
    from landppt.database.models import User

    async with maker() as session:
        user = User(
            username="buyer",
            email="buyer@example.com",
            credits_balance=0,
            is_active=True,
        )
        user.set_password("a sufficiently long password")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _configure(monkeypatch):
    from landppt.core.config import app_config

    monkeypatch.setattr(app_config, "enable_credits_system", True)
    monkeypatch.setattr(app_config, "billing_enabled", True)
    monkeypatch.setattr(app_config, "billing_credit_packs_json", CATALOG)
    monkeypatch.setattr(app_config, "stripe_secret_key", "sk_test_example")
    monkeypatch.setattr(app_config, "billing_allow_promotion_codes", False)
    monkeypatch.setattr(app_config, "billing_automatic_tax_enabled", False)


class _FakeSession(dict):
    def to_dict_recursive(self):
        return dict(self)


class _FakeSessionService:
    def __init__(self, calls):
        self.calls = calls

    def create(self, params, options):
        self.calls.append((params, options))
        return _FakeSession(
            id="cs_test_checkout",
            url="https://checkout.stripe.test/session",
            expires_at=time.time() + 3600,
            amount_subtotal=1000,
            amount_total=1000,
            currency="usd",
            livemode=False,
        )


class _FakeStripeClient:
    calls = []

    def __init__(self, _api_key):
        sessions = _FakeSessionService(self.calls)
        self.v1 = type(
            "V1",
            (),
            {"checkout": type("Checkout", (), {"sessions": sessions})()},
        )()


@pytest.mark.asyncio
async def test_checkout_is_server_priced_and_idempotent(tmp_path, monkeypatch):
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    _FakeStripeClient.calls = []
    monkeypatch.setattr(stripe, "StripeClient", _FakeStripeClient)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    try:
        for _ in range(2):
            async with maker() as session:
                result = await BillingService(session).create_checkout(
                    user=user,
                    pack_id="starter",
                    request_key="request-key-123",
                    success_url="https://app.example/credits?checkout=success",
                    cancel_url="https://app.example/credits?checkout=cancelled",
                )
                assert result["checkout_url"].startswith("https://checkout.stripe")

        assert len(_FakeStripeClient.calls) == 1
        params, options = _FakeStripeClient.calls[0]
        assert params["line_items"] == [{"price": "price_starter", "quantity": 1}]
        assert params["metadata"]["user_id"] == str(user.id)
        assert options["idempotency_key"].startswith("landppt-checkout-")
    finally:
        await engine.dispose()


def _checkout_event(order, event_id="evt_paid"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_test_checkout",
                "client_reference_id": order.id,
                "metadata": {
                    "order_id": order.id,
                    "user_id": str(order.user_id),
                    "pack_id": order.pack_id,
                },
                "currency": "usd",
                "amount_subtotal": 1000,
                "amount_total": 1000,
                "payment_status": "paid",
                "payment_intent": "pi_test_payment",
            }
        },
    }


@pytest.mark.asyncio
async def test_webhook_fulfillment_and_refunds_are_idempotent(tmp_path, monkeypatch):
    from landppt.database.models import CreditTransaction, PaymentOrder, User
    from landppt.services.billing_service import BillingService

    _configure(monkeypatch)
    engine, maker = await _billing_database(tmp_path)
    user = await _create_user(maker)
    try:
        async with maker() as session:
            order = PaymentOrder(
                user_id=user.id,
                checkout_request_key="a" * 64,
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
            event = _checkout_event(order)

        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(event, "a" * 64)
                == "processed"
            )
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(event, "a" * 64)
                == "duplicate"
            )
            assert (
                await session.scalar(
                    select(User.credits_balance).where(User.id == user.id)
                )
                == 250
            )
            assert await session.scalar(select(func.count(CreditTransaction.id))) == 1

        half_refund = {
            "id": "evt_refund_half",
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
        full_refund = {
            "id": "evt_refund_full",
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
                await BillingService(session).process_stripe_event(
                    half_refund, "b" * 64
                )
                == "processed"
            )
        async with maker() as session:
            assert (
                await BillingService(session).process_stripe_event(
                    full_refund, "c" * 64
                )
                == "processed"
            )
        async with maker() as session:
            assert (
                await session.scalar(
                    select(User.credits_balance).where(User.id == user.id)
                )
                == 0
            )
            saved_order = await session.get(PaymentOrder, order_id)
            assert saved_order.status == "refunded"
            assert saved_order.credits_reversed == 250
            assert await session.scalar(select(func.count(CreditTransaction.id))) == 3
    finally:
        await engine.dispose()


def test_stripe_webhook_signature_uses_raw_body(monkeypatch):
    from landppt.core.config import app_config
    from landppt.services.billing_service import verify_stripe_webhook

    payload = b'{"id":"evt_test","type":"ignored","data":{"object":{}}}'
    secret = "whsec_test_secret"
    monkeypatch.setattr(app_config, "stripe_webhook_secret", secret)
    header = stripe.WebhookSignature.generate_signature_header(
        payload.decode("utf-8"), secret
    )
    event = verify_stripe_webhook(payload, header)
    assert event["id"] == "evt_test"
    with pytest.raises(stripe.error.SignatureVerificationError):
        verify_stripe_webhook(payload + b" ", header)


def test_billing_migration_is_registered():
    from landppt.database.migrations import migration_manager

    assert any(
        migration["version"] == "022" and migration["name"] == "add_payment_orders"
        for migration in migration_manager.migrations
    )


@pytest.mark.asyncio
async def test_stripe_webhook_body_has_a_hard_size_limit():
    from fastapi import HTTPException
    from starlette.requests import Request

    from landppt.web.billing_routes import (
        MAX_STRIPE_WEBHOOK_BYTES,
        _read_webhook_body,
    )

    oversized = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/billing/webhooks/stripe",
            "headers": [
                (b"content-length", str(MAX_STRIPE_WEBHOOK_BYTES + 1).encode())
            ],
        }
    )
    with pytest.raises(HTTPException) as declared_error:
        await _read_webhook_body(oversized)
    assert declared_error.value.status_code == 413

    chunks = [
        {
            "type": "http.request",
            "body": b"a" * MAX_STRIPE_WEBHOOK_BYTES,
            "more_body": True,
        },
        {"type": "http.request", "body": b"b", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    streamed = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/billing/webhooks/stripe",
            "headers": [],
        },
        receive,
    )
    with pytest.raises(HTTPException) as streamed_error:
        await _read_webhook_body(streamed)
    assert streamed_error.value.status_code == 413
