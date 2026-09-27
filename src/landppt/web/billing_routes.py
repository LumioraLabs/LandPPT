"""Authenticated billing APIs and the public signed Stripe webhook."""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..auth.middleware import get_current_user_required
from ..auth.request_context import resolve_request_base_url
from ..core.config import app_config
from ..database.database import AsyncSessionLocal
from ..database.models import PaymentOrder, User
from ..services.billing_service import (
    BillingConfigurationError,
    BillingInProgress,
    BillingProviderError,
    BillingRequestError,
    BillingService,
    load_credit_packs,
    verify_stripe_webhook,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    pack_id: str = Field(min_length=1, max_length=64)


@router.get("/api/packages")
async def list_credit_packages(user: User = Depends(get_current_user_required)):
    """Return the safe, server-owned catalog without provider price IDs."""
    del user
    if not app_config.billing_enabled or not app_config.enable_credits_system:
        return {"enabled": False, "packages": []}
    try:
        packs = load_credit_packs()
    except BillingConfigurationError:
        logger.exception("Billing catalog is invalid")
        raise HTTPException(
            status_code=503, detail="Billing is temporarily unavailable"
        )
    return {
        "enabled": bool(packs),
        "packages": [pack.public_dict() for pack in packs.values()],
    }


@router.post("/api/checkout")
async def create_checkout(
    data: CheckoutRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: User = Depends(get_current_user_required),
):
    """Create a Stripe-hosted checkout for one configured credit pack."""
    base_url = resolve_request_base_url(request).rstrip("/")
    success_url = (
        f"{base_url}/credits?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = f"{base_url}/credits?checkout=cancelled"
    async with AsyncSessionLocal() as session:
        try:
            result = await BillingService(session).create_checkout(
                user=user,
                pack_id=data.pack_id,
                request_key=idempotency_key,
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except BillingRequestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BillingConfigurationError as exc:
            logger.error("Billing configuration error: %s", exc)
            raise HTTPException(
                status_code=503, detail="Billing is temporarily unavailable"
            ) from exc
        except BillingProviderError as exc:
            raise HTTPException(
                status_code=502, detail="Payment provider is temporarily unavailable"
            ) from exc
    order = result["order"]
    return {
        "order_id": order.id,
        "checkout_url": result["checkout_url"],
    }


@router.get("/api/orders")
async def list_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user_required),
):
    """List the current user's durable payment orders."""
    async with AsyncSessionLocal() as session:
        where = PaymentOrder.user_id == user.id
        total = await session.scalar(select(func.count(PaymentOrder.id)).where(where))
        orders = list(
            (
                await session.scalars(
                    select(PaymentOrder)
                    .where(where)
                    .order_by(PaymentOrder.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
    return {
        "orders": [order.to_dict() for order in orders],
        "total": total or 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/api/orders/{order_id}")
async def get_order(
    order_id: str,
    user: User = Depends(get_current_user_required),
):
    """Poll a payment order without exposing provider bearer URLs or secrets."""
    async with AsyncSessionLocal() as session:
        order = await session.scalar(
            select(PaymentOrder).where(
                PaymentOrder.id == order_id,
                PaymentOrder.user_id == user.id,
            )
        )
    if order is None:
        raise HTTPException(status_code=404, detail="Payment order not found")
    return order.to_dict()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
):
    """Verify and idempotently fulfill Stripe snapshot events."""
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing webhook signature")
    try:
        event = verify_stripe_webhook(payload, stripe_signature)
    except BillingConfigurationError:
        logger.exception("Stripe webhook is not configured")
        raise HTTPException(status_code=503, detail="Webhook is not configured")
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=400, detail="Invalid webhook signature"
        ) from exc

    payload_sha256 = hashlib.sha256(payload).hexdigest()
    async with AsyncSessionLocal() as session:
        try:
            result = await BillingService(session).process_stripe_event(
                event, payload_sha256
            )
        except BillingInProgress as exc:
            raise HTTPException(
                status_code=409, detail="Webhook receipt is in progress"
            ) from exc
        except BillingRequestError:
            logger.exception("Stripe webhook order validation failed")
            raise HTTPException(
                status_code=422, detail="Webhook order validation failed"
            )
        except Exception:
            logger.exception("Stripe webhook processing failed")
            raise HTTPException(status_code=500, detail="Webhook processing failed")
    return {"received": True, "result": result}
