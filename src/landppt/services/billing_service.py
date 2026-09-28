"""Stripe-hosted checkout with durable, idempotent credit fulfillment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

import stripe
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import app_config
from ..database.models import (
    CreditTransaction,
    PaymentOrder,
    PaymentWebhookEvent,
    User,
)

logger = logging.getLogger(__name__)

PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BillingConfigurationError(RuntimeError):
    """Billing is disabled or its server-owned catalog is invalid."""


class BillingRequestError(ValueError):
    """A client-selected pack or webhook order is invalid."""


class BillingProviderError(RuntimeError):
    """The payment provider could not complete an API request."""


class BillingInProgress(RuntimeError):
    """Another delivery still holds the webhook lease. Callers must not ack success."""


WEBHOOK_LEASE_SECONDS = 120
WEBHOOK_RETENTION_SECONDS = 90 * 24 * 60 * 60
_SECRET_MARKERS = ("sk_live_", "sk_test_", "whsec_", "rk_live_", "rk_test_")


@dataclass(frozen=True)
class CreditPack:
    pack_id: str
    name: str
    credits: int
    amount_minor: int
    currency: str
    stripe_price_id: str

    def public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.pack_id,
            "name": self.name,
            "credits": self.credits,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
        }


def load_credit_packs(raw_catalog: Optional[str] = None) -> Dict[str, CreditPack]:
    """Parse and strictly validate the deployment-owned product catalog."""
    raw = app_config.billing_credit_packs_json if raw_catalog is None else raw_catalog
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise BillingConfigurationError(
            "LANDPPT_BILLING_CREDIT_PACKS_JSON is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise BillingConfigurationError("Billing credit pack catalog must be an object")

    packs: Dict[str, CreditPack] = {}
    for raw_pack_id, details in parsed.items():
        pack_id = str(raw_pack_id or "").strip().lower()
        if not PACK_ID_RE.fullmatch(pack_id) or not isinstance(details, dict):
            raise BillingConfigurationError(f"Invalid billing pack: {raw_pack_id!r}")
        try:
            name = str(details["name"]).strip()
            credits = int(details["credits"])
            amount_minor = int(details["amount_minor"])
            currency = str(details["currency"]).strip().lower()
            price_id = str(details["stripe_price_id"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} is incomplete"
            ) from exc
        if not name or len(name) > 200:
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} has an invalid name"
            )
        if credits <= 0 or credits > 1_000_000_000:
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} has invalid credits"
            )
        if amount_minor <= 0 or amount_minor > 1_000_000_000:
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} has invalid amount"
            )
        if len(currency) != 3 or not currency.isalpha():
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} has invalid currency"
            )
        if not price_id.startswith("price_") or len(price_id) > 255:
            raise BillingConfigurationError(
                f"Billing pack {pack_id!r} has invalid Stripe price"
            )
        packs[pack_id] = CreditPack(
            pack_id=pack_id,
            name=name,
            credits=credits,
            amount_minor=amount_minor,
            currency=currency,
            stripe_price_id=price_id,
        )
    return packs


def verify_stripe_webhook(payload: bytes, signature: str) -> Mapping[str, Any]:
    """Verify a Stripe snapshot event against the unmodified request body."""
    secret = str(app_config.stripe_webhook_secret or "").strip()
    if not secret:
        raise BillingConfigurationError("STRIPE_WEBHOOK_SECRET is not configured")
    return stripe.Webhook.construct_event(payload, signature, secret)


def _object_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    converter = getattr(value, "to_dict_recursive", None)
    if callable(converter):
        return dict(converter())
    return dict(value)


def _provider_request_id(event_data: Mapping[str, Any]) -> Optional[str]:
    request = event_data.get("request")
    if isinstance(request, str):
        value = request.strip()
    elif isinstance(request, Mapping):
        value = str(request.get("id") or "").strip()
    else:
        value = ""
    return value[:255] or None


def _safe_error_text(exc: Exception) -> str:
    text = " ".join(str(exc).split())[:500]
    for marker in _SECRET_MARKERS:
        if marker in text:
            text = text.replace(marker, "[redacted]")
    secret = str(app_config.stripe_secret_key or "")
    webhook_secret = str(app_config.stripe_webhook_secret or "")
    for value in (secret, webhook_secret):
        if value and value in text:
            text = text.replace(value, "[redacted]")
    return text or "payment processing failed"


def _sanitized_event_summary(event_data: Mapping[str, Any]) -> str:
    """Keep the fields needed to repair an order. Never store the raw payload."""
    obj = _object_dict((event_data.get("data") or {}).get("object") or {})
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    summary = {
        "id": str(event_data.get("id") or "")[:255],
        "type": str(event_data.get("type") or "")[:100],
        "livemode": bool(event_data.get("livemode")),
        "request_id": _provider_request_id(event_data),
        "object": {
            "id": str(obj.get("id") or "")[:255],
            "payment_intent": str(obj.get("payment_intent") or "")[:255],
            "payment_status": str(obj.get("payment_status") or "")[:32],
            "currency": str(obj.get("currency") or "")[:3],
            "amount_subtotal": obj.get("amount_subtotal"),
            "amount_total": obj.get("amount_total"),
            "amount_refunded": obj.get("amount_refunded"),
            "amount": obj.get("amount"),
            "refunded": bool(obj.get("refunded")),
            "status": str(obj.get("status") or "")[:32],
            "client_reference_id": str(obj.get("client_reference_id") or "")[:64],
            "metadata": {
                "order_id": str(metadata.get("order_id") or "")[:64],
                "user_id": str(metadata.get("user_id") or "")[:32],
                "pack_id": str(metadata.get("pack_id") or "")[:64],
            },
        },
    }
    return json.dumps(summary, separators=(",", ":"), sort_keys=True)[:8000]


def event_from_stored_summary(summary_json: str) -> Dict[str, Any]:
    """Rebuild the minimum event the processor needs from a stored summary."""
    summary = json.loads(summary_json or "{}")
    if not isinstance(summary, dict):
        raise BillingRequestError("Stored payment summary is invalid")
    obj = summary.get("object") if isinstance(summary.get("object"), dict) else {}
    return {
        "id": summary.get("id"),
        "type": summary.get("type"),
        "livemode": bool(summary.get("livemode")),
        "request": {"id": summary.get("request_id")},
        "data": {"object": obj},
    }


class BillingService:
    """Coordinates Checkout sessions, webhook receipts, and the credit ledger."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _require_enabled() -> str:
        if not app_config.billing_enabled or not app_config.enable_credits_system:
            raise BillingConfigurationError("Billing is not enabled")
        api_key = str(app_config.stripe_secret_key or "").strip()
        if not api_key:
            raise BillingConfigurationError("STRIPE_SECRET_KEY is not configured")
        return api_key

    async def create_checkout(
        self,
        *,
        user: User,
        pack_id: str,
        request_key: str,
        success_url: str,
        cancel_url: str,
    ) -> Dict[str, Any]:
        """Create or resume one idempotent Stripe Checkout Session."""
        api_key = self._require_enabled()
        pack = load_credit_packs().get(str(pack_id or "").strip().lower())
        if pack is None:
            raise BillingRequestError("Unknown credit pack")
        normalized_request_key = str(request_key or "").strip()
        if not 8 <= len(normalized_request_key) <= 200:
            raise BillingRequestError("Idempotency-Key must be 8 to 200 characters")
        request_digest = hashlib.sha256(
            f"{user.id}\0{normalized_request_key}".encode("utf-8")
        ).hexdigest()

        existing = await self.session.scalar(
            select(PaymentOrder).where(
                PaymentOrder.checkout_request_key == request_digest
            )
        )
        if existing is not None:
            if existing.user_id != user.id or existing.pack_id != pack.pack_id:
                raise BillingRequestError("Idempotency-Key was used for another order")
            if existing.checkout_url and (
                not existing.checkout_expires_at
                or existing.checkout_expires_at > time.time()
            ):
                return {"order": existing, "checkout_url": existing.checkout_url}
            order = existing
        else:
            order = PaymentOrder(
                user_id=user.id,
                checkout_request_key=request_digest,
                pack_id=pack.pack_id,
                pack_name=pack.name,
                credits_amount=pack.credits,
                amount_minor=pack.amount_minor,
                currency=pack.currency,
                status="pending",
            )
            self.session.add(order)
            try:
                await self.session.commit()
                await self.session.refresh(order)
            except IntegrityError:
                await self.session.rollback()
                order = await self.session.scalar(
                    select(PaymentOrder).where(
                        PaymentOrder.checkout_request_key == request_digest
                    )
                )
                if (
                    order is None
                    or order.user_id != user.id
                    or order.pack_id != pack.pack_id
                ):
                    raise BillingRequestError("Conflicting checkout request")

        client = stripe.StripeClient(api_key)
        params: Dict[str, Any] = {
            "mode": "payment",
            "line_items": [{"price": pack.stripe_price_id, "quantity": 1}],
            "client_reference_id": order.id,
            "metadata": {
                "order_id": order.id,
                "user_id": str(user.id),
                "pack_id": pack.pack_id,
            },
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": app_config.billing_allow_promotion_codes,
            "automatic_tax": {
                "enabled": app_config.billing_automatic_tax_enabled,
            },
        }
        if user.email:
            params["customer_email"] = user.email
        try:
            checkout = await asyncio.to_thread(
                client.v1.checkout.sessions.create,
                params,
                {"idempotency_key": f"landppt-checkout-{order.id}"},
            )
        except Exception as exc:
            logger.exception("Stripe Checkout creation failed for order %s", order.id)
            raise BillingProviderError("Unable to start payment checkout") from exc

        checkout_data = _object_dict(checkout)
        checkout_id = str(checkout_data.get("id") or "")
        checkout_url = str(checkout_data.get("url") or "")
        if not checkout_id or not checkout_url.startswith("https://"):
            raise BillingProviderError("Payment provider returned an invalid checkout")
        returned_currency = str(checkout_data.get("currency") or "").lower()
        returned_subtotal = checkout_data.get("amount_subtotal")
        if returned_currency and returned_currency != order.currency:
            raise BillingProviderError("Payment provider returned the wrong currency")
        if (
            returned_subtotal is not None
            and int(returned_subtotal) != order.amount_minor
        ):
            raise BillingProviderError("Payment provider returned the wrong price")

        order.provider_checkout_session_id = checkout_id
        order.checkout_url = checkout_url
        order.checkout_expires_at = float(checkout_data.get("expires_at") or 0) or None
        order.charged_amount_minor = int(
            checkout_data.get("amount_total") or order.amount_minor
        )
        order.livemode = bool(checkout_data.get("livemode", False))
        order.updated_at = time.time()
        await self.session.commit()
        return {"order": order, "checkout_url": checkout_url}

    async def process_stripe_event(
        self, event: Mapping[str, Any], payload_sha256: str
    ) -> str:
        """Process one verified event; returns processed, duplicate, or ignored.

        A receipt left in processing can be finished by a later delivery once its
        lease expires. A delivery that loses the claim raises BillingInProgress
        instead of acknowledging success.
        """
        event_data = _object_dict(event)
        event_id = str(event_data.get("id") or "")
        event_type = str(event_data.get("type") or "")
        if not event_id or len(event_id) > 255 or not event_type:
            raise BillingRequestError("Malformed payment event")
        if not payload_sha256 or len(payload_sha256) != 64:
            raise BillingRequestError("Malformed payment event")

        claimed = await self._claim_webhook_receipt(event_data, payload_sha256)
        if claimed == "duplicate":
            return "duplicate"
        try:
            result = await self._dispatch_stripe_event(event_data)
            receipt = await self._load_receipt(event_id)
            receipt.status = "completed"
            receipt.processed_at = time.time()
            receipt.lease_expires_at = None
            receipt.last_error = None
            await self.session.commit()
            return result
        except Exception as exc:
            await self.session.rollback()
            await self._mark_receipt_failed(event_id, event_type, payload_sha256, exc)
            raise

    async def _checkout_order(self, obj: Mapping[str, Any]) -> PaymentOrder:
        metadata = obj.get("metadata") or {}
        order_id = str(metadata.get("order_id") or obj.get("client_reference_id") or "")
        checkout_id = str(obj.get("id") or "")
        if not order_id or not checkout_id:
            raise BillingRequestError("Checkout event is missing order identity")
        order = await self.session.scalar(
            select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
        )
        if order is None:
            raise BillingRequestError("Payment order does not exist")
        if order.provider_checkout_session_id not in {None, checkout_id}:
            raise BillingRequestError("Checkout session does not match order")
        if str(metadata.get("user_id") or "") != str(order.user_id):
            raise BillingRequestError("Checkout user does not match order")
        if str(metadata.get("pack_id") or "") != order.pack_id:
            raise BillingRequestError("Checkout pack does not match order")
        if str(obj.get("currency") or "").lower() != order.currency:
            raise BillingRequestError("Checkout currency does not match order")
        if int(obj.get("amount_subtotal") or -1) != order.amount_minor:
            raise BillingRequestError("Checkout amount does not match order")
        return order

    async def _load_receipt(self, event_id: str) -> PaymentWebhookEvent:
        receipt = await self.session.scalar(
            select(PaymentWebhookEvent).where(
                PaymentWebhookEvent.provider == "stripe",
                PaymentWebhookEvent.event_id == event_id,
            )
        )
        if receipt is None:
            raise BillingRequestError("Payment receipt does not exist")
        return receipt

    async def _claim_webhook_receipt(
        self, event_data: Mapping[str, Any], payload_sha256: str
    ) -> str:
        event_id = str(event_data.get("id") or "")
        now = time.time()
        summary = _sanitized_event_summary(event_data)
        request_id = _provider_request_id(event_data)
        existing = await self.session.scalar(
            select(PaymentWebhookEvent)
            .where(
                PaymentWebhookEvent.provider == "stripe",
                PaymentWebhookEvent.event_id == event_id,
            )
            .with_for_update()
        )
        if existing is None:
            self.session.add(
                PaymentWebhookEvent(
                    provider="stripe",
                    event_id=event_id,
                    event_type=str(event_data.get("type") or ""),
                    payload_sha256=payload_sha256,
                    status="processing",
                    attempts=1,
                    lease_expires_at=now + WEBHOOK_LEASE_SECONDS,
                    provider_request_id=request_id,
                    summary_json=summary,
                )
            )
            try:
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                existing = await self._load_receipt(event_id)
            else:
                return "claimed"
        return await self._reclaim_webhook_receipt(
            existing,
            event_data,
            payload_sha256,
            summary,
            request_id,
        )

    async def _reclaim_webhook_receipt(
        self,
        receipt: PaymentWebhookEvent,
        event_data: Mapping[str, Any],
        payload_sha256: str,
        summary: str,
        request_id: Optional[str],
    ) -> str:
        if receipt.payload_sha256 != payload_sha256:
            await self.session.rollback()
            raise BillingRequestError("Webhook payload does not match the stored receipt")
        if receipt.status == "completed":
            await self.session.rollback()
            return "duplicate"
        now = time.time()
        lease_open = (receipt.lease_expires_at or 0) > now
        if receipt.status == "processing" and lease_open:
            await self.session.rollback()
            raise BillingInProgress("Webhook receipt is already being processed")
        receipt.status = "processing"
        receipt.attempts = int(receipt.attempts or 0) + 1
        receipt.lease_expires_at = now + WEBHOOK_LEASE_SECONDS
        receipt.last_error = None
        receipt.event_type = str(event_data.get("type") or receipt.event_type)
        receipt.provider_request_id = request_id or receipt.provider_request_id
        receipt.summary_json = summary
        await self.session.commit()
        return "claimed"

    async def _mark_receipt_failed(
        self,
        event_id: str,
        event_type: str,
        payload_sha256: str,
        exc: Exception,
    ) -> None:
        failed = await self.session.scalar(
            select(PaymentWebhookEvent).where(
                PaymentWebhookEvent.provider == "stripe",
                PaymentWebhookEvent.event_id == event_id,
            )
        )
        if failed is None:
            failed = PaymentWebhookEvent(
                provider="stripe",
                event_id=event_id,
                event_type=event_type,
                payload_sha256=payload_sha256,
                status="failed",
                attempts=1,
            )
            self.session.add(failed)
        else:
            failed.status = "failed"
            failed.lease_expires_at = None
        failed.last_error = _safe_error_text(exc)
        if int(failed.attempts or 0) >= 3:
            logger.warning(
                "Payment receipt needs attention event_id=%s attempts=%s",
                event_id,
                failed.attempts,
            )
        await self.session.commit()

    async def _dispatch_stripe_event(self, event_data: Mapping[str, Any]) -> str:
        event_type = str(event_data.get("type") or "")
        obj = _object_dict((event_data.get("data") or {}).get("object") or {})
        livemode = bool(event_data.get("livemode"))
        request_id = _provider_request_id(event_data)
        if event_type in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        }:
            await self._fulfill_checkout(obj, livemode, request_id)
        elif event_type == "checkout.session.async_payment_failed":
            await self._mark_checkout_status(obj, "payment_failed", livemode)
        elif event_type == "checkout.session.expired":
            await self._mark_checkout_status(obj, "expired", livemode)
        elif event_type == "charge.refunded":
            await self._apply_refund(obj, str(event_data.get("id") or ""), livemode)
        elif event_type in {
            "charge.dispute.created",
            "charge.dispute.funds_withdrawn",
        }:
            await self._apply_dispute(obj, livemode)
        elif event_type == "charge.dispute.closed":
            await self._close_dispute(obj, livemode)
        else:
            return "ignored"
        return "processed"

    async def reconcile_stored_events(self) -> Dict[str, int]:
        """Repair local orders from sanitized receipts. Does not call Stripe."""
        now = time.time()
        receipts = list(
            (
                await self.session.scalars(
                    select(PaymentWebhookEvent).where(
                        PaymentWebhookEvent.provider == "stripe",
                        PaymentWebhookEvent.status.in_(("failed", "processing")),
                    )
                )
            ).all()
        )
        repaired = 0
        stalled = 0
        for receipt in receipts:
            if int(receipt.attempts or 0) >= 5:
                stalled += 1
                logger.warning(
                    "Payment receipt needs attention event_id=%s attempts=%s",
                    receipt.event_id,
                    receipt.attempts,
                )
            if (
                receipt.status == "processing"
                and (receipt.lease_expires_at or 0) > now
            ):
                continue
            if not receipt.summary_json:
                continue
            try:
                event = event_from_stored_summary(receipt.summary_json)
            except (BillingRequestError, json.JSONDecodeError):
                logger.warning(
                    "Payment reconciliation skipped an unreadable receipt event_id=%s",
                    receipt.event_id,
                )
                continue
            try:
                await self.process_stripe_event(event, receipt.payload_sha256)
                repaired += 1
            except BillingInProgress:
                continue
            except Exception:
                logger.warning(
                    "Payment reconciliation could not repair event_id=%s",
                    receipt.event_id,
                )
        unfulfilled = await self._count_unfulfilled_orders()
        if unfulfilled:
            logger.warning("Unfulfilled payment orders remain count=%s", unfulfilled)
        return {
            "repaired": repaired,
            "stalled": stalled,
            "unfulfilled": unfulfilled,
        }

    async def _count_unfulfilled_orders(self) -> int:
        rows = (
            await self.session.scalars(
                select(PaymentOrder.id).where(
                    PaymentOrder.status.in_(("pending", "awaiting_payment"))
                )
            )
        ).all()
        return len(rows)

    async def purge_completed_webhook_receipts(
        self, *, older_than_seconds: int = WEBHOOK_RETENTION_SECONDS
    ) -> int:
        """Delete completed receipts past retention. Failed receipts stay for repair."""
        cutoff = time.time() - max(0, int(older_than_seconds))
        result = await self.session.execute(
            delete(PaymentWebhookEvent).where(
                PaymentWebhookEvent.status == "completed",
                PaymentWebhookEvent.processed_at.is_not(None),
                PaymentWebhookEvent.processed_at < cutoff,
            )
        )
        await self.session.commit()
        return int(result.rowcount or 0)

    async def _fulfill_checkout(
        self,
        obj: Mapping[str, Any],
        event_livemode: bool,
        provider_request_id: Optional[str] = None,
    ) -> None:
        order = await self._checkout_order(obj)
        if bool(event_livemode) != bool(order.livemode):
            raise BillingRequestError("Checkout mode does not match order")
        if str(obj.get("payment_status") or "") != "paid":
            order.status = "awaiting_payment"
            order.updated_at = time.time()
            return

        credit_key = f"payment:stripe:{order.id}"
        existing_credit = await self.session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == credit_key
            )
        )
        if existing_credit is None:
            result = await self.session.execute(
                update(User)
                .where(User.id == order.user_id)
                .values(credits_balance=User.credits_balance + order.credits_amount)
                .returning(User.credits_balance)
            )
            new_balance = result.scalar_one_or_none()
            if new_balance is None:
                raise BillingRequestError("Payment user does not exist")
            self.session.add(
                CreditTransaction(
                    user_id=order.user_id,
                    amount=order.credits_amount,
                    balance_after=new_balance,
                    transaction_type="recharge",
                    description=f"Stripe purchase: {order.pack_name}",
                    reference_id=order.id,
                    idempotency_key=credit_key,
                )
            )
        elif (
            existing_credit.user_id != order.user_id
            or existing_credit.amount != order.credits_amount
        ):
            raise BillingRequestError("Payment credit idempotency conflict")

        order.provider_checkout_session_id = str(obj.get("id"))
        order.provider_payment_id = str(obj.get("payment_intent") or "") or None
        if provider_request_id:
            order.provider_request_id = provider_request_id
        order.charged_amount_minor = int(obj.get("amount_total") or order.amount_minor)
        order.status = "fulfilled"
        order.paid_at = order.paid_at or time.time()
        order.updated_at = time.time()

    async def _mark_checkout_status(
        self, obj: Mapping[str, Any], status: str, event_livemode: bool = False
    ) -> None:
        order = await self._checkout_order(obj)
        if bool(event_livemode) != bool(order.livemode):
            raise BillingRequestError("Checkout mode does not match order")
        if order.status not in {"fulfilled", "refunded", "partially_refunded"}:
            order.status = status
            order.updated_at = time.time()

    def _reversal_target(self, order: PaymentOrder, refunded_minor: int, *, full: bool) -> int:
        """Floor partial reversals. A full refund or dispute reverses the whole grant."""
        charged = int(order.charged_amount_minor or order.amount_minor)
        if charged <= 0:
            raise BillingRequestError("Payment order has no charge to reverse")
        refunded = max(0, min(int(refunded_minor or 0), charged))
        if full or refunded >= charged:
            return order.credits_amount
        return min(order.credits_amount, (order.credits_amount * refunded) // charged)

    async def _debit_reversal(
        self,
        order: PaymentOrder,
        delta: int,
        *,
        idempotency_key: str,
        description: str,
    ) -> None:
        if delta <= 0:
            return
        existing = await self.session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if existing.user_id != order.user_id or existing.amount != -delta:
                raise BillingRequestError("Payment reversal idempotency conflict")
            return
        result = await self.session.execute(
            update(User)
            .where(User.id == order.user_id)
            .values(credits_balance=User.credits_balance - delta)
            .returning(User.credits_balance)
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            raise BillingRequestError("Refund user does not exist")
        self.session.add(
            CreditTransaction(
                user_id=order.user_id,
                amount=-delta,
                balance_after=balance,
                transaction_type="refund_reversal",
                description=description[:500],
                reference_id=order.id,
                idempotency_key=idempotency_key,
            )
        )

    async def _credit_restore(
        self,
        order: PaymentOrder,
        amount: int,
        *,
        idempotency_key: str,
        description: str,
    ) -> None:
        if amount <= 0:
            return
        existing = await self.session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if existing.user_id != order.user_id or existing.amount != amount:
                raise BillingRequestError("Payment reversal idempotency conflict")
            return
        result = await self.session.execute(
            update(User)
            .where(User.id == order.user_id)
            .values(credits_balance=User.credits_balance + amount)
            .returning(User.credits_balance)
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            raise BillingRequestError("Refund user does not exist")
        self.session.add(
            CreditTransaction(
                user_id=order.user_id,
                amount=amount,
                balance_after=balance,
                transaction_type="dispute_restore",
                description=description[:500],
                reference_id=order.id,
                idempotency_key=idempotency_key,
            )
        )

    async def _locked_order_for_payment(
        self, payment_id: str, event_livemode: bool
    ) -> PaymentOrder:
        if not payment_id:
            raise BillingRequestError("Refund event is missing payment identity")
        order = await self.session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.provider_payment_id == payment_id)
            .with_for_update()
        )
        if order is None:
            raise BillingRequestError("Refunded payment order does not exist")
        if bool(event_livemode) != bool(order.livemode):
            raise BillingRequestError("Checkout mode does not match order")
        return order

    async def _apply_refund(
        self, obj: Mapping[str, Any], event_id: str, event_livemode: bool = False
    ) -> None:
        order = await self._locked_order_for_payment(
            str(obj.get("payment_intent") or ""), event_livemode
        )
        charged = int(order.charged_amount_minor or order.amount_minor)
        refunded = max(0, min(int(obj.get("amount_refunded") or 0), charged))
        target_reversal = self._reversal_target(
            order, refunded, full=bool(obj.get("refunded")) or refunded >= charged
        )
        delta = max(0, target_reversal - int(order.credits_reversed or 0))
        await self._debit_reversal(
            order,
            delta,
            idempotency_key=f"refund:stripe:{event_id}",
            description=f"Stripe refund: {order.pack_name}",
        )
        order.refunded_amount_minor = refunded
        order.credits_reversed = target_reversal
        order.status = "refunded" if refunded >= charged else "partially_refunded"
        order.updated_at = time.time()

    async def _apply_dispute(self, obj: Mapping[str, Any], event_livemode: bool) -> None:
        dispute_id = str(obj.get("id") or "")
        if not dispute_id:
            raise BillingRequestError("Dispute event is missing dispute identity")
        order = await self._locked_order_for_payment(
            str(obj.get("payment_intent") or ""), event_livemode
        )
        charged = int(order.charged_amount_minor or order.amount_minor)
        disputed = max(0, min(int(obj.get("amount") or 0), charged))
        target_reversal = self._reversal_target(
            order, disputed, full=disputed >= charged
        )
        delta = max(0, target_reversal - int(order.credits_reversed or 0))
        await self._debit_reversal(
            order,
            delta,
            idempotency_key=f"dispute:stripe:{dispute_id}",
            description=f"Stripe dispute: {order.pack_name}",
        )
        order.credits_reversed = max(int(order.credits_reversed or 0), target_reversal)
        if order.credits_reversed >= order.credits_amount:
            order.status = "disputed"
        order.updated_at = time.time()

    async def _close_dispute(self, obj: Mapping[str, Any], event_livemode: bool) -> None:
        dispute_id = str(obj.get("id") or "")
        if not dispute_id:
            raise BillingRequestError("Dispute event is missing dispute identity")
        status = str(obj.get("status") or "").lower()
        if status != "won":
            return
        order = await self._locked_order_for_payment(
            str(obj.get("payment_intent") or ""), event_livemode
        )
        original = await self.session.scalar(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == f"dispute:stripe:{dispute_id}"
            )
        )
        if original is None:
            return
        restored = max(0, -int(original.amount))
        await self._credit_restore(
            order,
            restored,
            idempotency_key=f"dispute-restore:stripe:{dispute_id}",
            description=f"Stripe dispute won: {order.pack_name}",
        )
        order.credits_reversed = max(0, int(order.credits_reversed or 0) - restored)
        if order.credits_reversed <= 0 and order.status == "disputed":
            order.status = "fulfilled"
        order.updated_at = time.time()
