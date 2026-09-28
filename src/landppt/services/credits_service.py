"""
Credits service for managing user credits consumption and recharge
"""

import asyncio
import logging
import time
from contextlib import suppress
from typing import Dict, Any, Optional, List, Tuple
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.repositories import UserRepository, CreditTransactionRepository, RedemptionCodeRepository
from ..database.models import CreditReservation, CreditTransaction, RedemptionCode, User

logger = logging.getLogger(__name__)


def _attempt_stamp(updated_at: Optional[float]) -> str:
    """Identity of one hold. Microseconds keep a same-second retry unique."""
    if updated_at is None:
        return "0"
    return str(int(round(float(updated_at) * 1_000_000)))


def _next_timestamp(previous: Optional[float] = None) -> float:
    now = time.time()
    if previous is None:
        return now
    previous_value = float(previous)
    if now <= previous_value:
        return previous_value + 0.000001
    return now


def _fit_idempotency_key(key: str) -> str:
    if len(key) <= 128:
        return key
    import hashlib

    return "k:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


class CreditsService:
    """Service for managing user credits"""
    
    # Credit costs for different operations (user-confirmed pricing)
    COSTS = {
        "outline_generation": 3,      # 大纲生成（按LLM调用次数）3积分/次
        "slide_generation": 5,        # 幻灯片每页 5积分
        "template_generation": 10,    # 模板生成 10积分
        "ai_edit": 3,                 # AI编辑每次 3积分
        "ai_other": 1,                # 其他AI操作每次 1积分
    }
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
        self.transaction_repo = CreditTransactionRepository(session)
        self.redemption_repo = RedemptionCodeRepository(session)
    
    async def get_balance(self, user_id: int) -> int:
        """Get user's current credit balance"""
        user = await self.user_repo.get_by_id(user_id)
        return user.credits_balance if user else 0
    
    async def check_balance(self, user_id: int, required: int) -> bool:
        """Check if user has enough credits"""
        if required < 0:
            return False
        balance = await self.get_balance(user_id)
        return balance >= required

    @staticmethod
    def _normalize_idempotency_key(value: Optional[str]) -> Optional[str]:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if len(normalized) > 128:
            raise ValueError("Idempotency key must not exceed 128 characters")
        return normalized

    async def _get_idempotent_transaction(
        self,
        idempotency_key: Optional[str],
    ) -> Optional[CreditTransaction]:
        if not idempotency_key:
            return None
        result = await self.session.execute(
            select(CreditTransaction).where(
                CreditTransaction.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none()
    
    async def consume_credits(
        self, 
        user_id: int, 
        operation_type: str,
        quantity: int = 1,
        description: Optional[str] = None,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Consume credits for an operation.
        
        Args:
            user_id: User ID
            operation_type: Type of operation (from COSTS)
            quantity: Number of items (e.g., number of slides)
            description: Optional description
            reference_id: Optional reference (project_id, etc.)
        
        Returns:
            Tuple of (success, message)
        """
        if operation_type not in self.COSTS:
            return False, "未知的积分操作类型"
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            return False, "操作数量必须为正整数"

        cost_per_item = self.COSTS[operation_type]
        total_cost = cost_per_item * quantity
        try:
            normalized_key = self._normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            return False, str(exc)

        existing = await self._get_idempotent_transaction(normalized_key)
        if existing:
            if existing.user_id == user_id and existing.amount == -total_cost:
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "幂等键已被其他积分操作使用"

        if not description:
            description = self._get_default_description(operation_type, quantity)

        try:
            result = await self.session.execute(
                update(User)
                .where(
                    User.id == user_id,
                    User.credits_balance >= total_cost,
                )
                .values(credits_balance=User.credits_balance - total_cost)
                .returning(User.credits_balance)
            )
            new_balance = result.scalar_one_or_none()
            if new_balance is None:
                user_result = await self.session.execute(
                    select(User.credits_balance).where(User.id == user_id)
                )
                balance = user_result.scalar_one_or_none()
                await self.session.rollback()
                if balance is None:
                    return False, "用户不存在"
                return False, f"积分不足。需要 {total_cost} 积分，当前余额 {balance} 积分"

            self.session.add(
                CreditTransaction(
                    user_id=user_id,
                    amount=-total_cost,
                    balance_after=new_balance,
                    transaction_type="consume",
                    description=description,
                    reference_id=reference_id,
                    idempotency_key=normalized_key,
                )
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._get_idempotent_transaction(normalized_key)
            if existing and existing.user_id == user_id and existing.amount == -total_cost:
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "积分请求与已有幂等操作冲突"
        except Exception:
            await self.session.rollback()
            logger.exception("Atomic credit consumption failed for user %s", user_id)
            return False, "积分扣除失败，请重试"

        logger.info(f"User {user_id} consumed {total_cost} credits for {operation_type}. New balance: {new_balance}")
        return True, f"消费成功。扣除 {total_cost} 积分，余额 {new_balance} 积分"
    
    async def add_credits(
        self,
        user_id: int,
        amount: int,
        transaction_type: str,
        description: str,
        reference_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Add credits to user account.
        
        Args:
            user_id: User ID
            amount: Amount to add (positive)
            transaction_type: Type (recharge, redemption, admin_adjust, refund)
            description: Description
            reference_id: Optional reference
        
        Returns:
            Tuple of (success, message)
        """
        if amount <= 0:
            return False, "充值金额必须为正数"
        
        try:
            normalized_key = self._normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            return False, str(exc)

        existing = await self._get_idempotent_transaction(normalized_key)
        if existing:
            if existing.user_id == user_id and existing.amount == amount:
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "幂等键已被其他积分操作使用"

        try:
            result = await self.session.execute(
                update(User)
                .where(User.id == user_id)
                .values(credits_balance=User.credits_balance + amount)
                .returning(User.credits_balance)
            )
            new_balance = result.scalar_one_or_none()
            if new_balance is None:
                await self.session.rollback()
                return False, "用户不存在"

            self.session.add(
                CreditTransaction(
                    user_id=user_id,
                    amount=amount,
                    balance_after=new_balance,
                    transaction_type=transaction_type,
                    description=description,
                    reference_id=reference_id,
                    idempotency_key=normalized_key,
                )
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._get_idempotent_transaction(normalized_key)
            if existing and existing.user_id == user_id and existing.amount == amount:
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "积分请求与已有幂等操作冲突"
        except Exception:
            await self.session.rollback()
            logger.exception("Atomic credit addition failed for user %s", user_id)
            return False, "积分充值失败，请重试"

        logger.info(f"User {user_id} received {amount} credits ({transaction_type}). New balance: {new_balance}")
        return True, f"充值成功。增加 {amount} 积分，余额 {new_balance} 积分"

    async def adjust_credits(
        self,
        user_id: int,
        amount: int,
        *,
        reason: str,
        actor_user_id: int,
        idempotency_key: str,
    ) -> Tuple[bool, str]:
        """Apply one audited support adjustment. The same key does not apply twice."""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0:
            return False, "调整金额不能为 0"
        reason_text = " ".join(str(reason or "").split())
        if not reason_text:
            return False, "调整原因不能为空"
        try:
            normalized_key = self._normalize_idempotency_key(idempotency_key)
        except ValueError as exc:
            return False, str(exc)
        if not normalized_key:
            return False, "管理员调整需要幂等键"

        existing = await self._get_idempotent_transaction(normalized_key)
        if existing is not None:
            if (
                existing.user_id == user_id
                and existing.amount == amount
                and existing.transaction_type == "admin_adjust"
            ):
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "幂等键已被其他积分操作使用"

        description = f"管理员调整 actor={int(actor_user_id)}: {reason_text}"[:500]
        try:
            result = await self.session.execute(
                update(User)
                .where(User.id == user_id)
                .values(credits_balance=User.credits_balance + amount)
                .returning(User.credits_balance)
            )
            new_balance = result.scalar_one_or_none()
            if new_balance is None:
                await self.session.rollback()
                return False, "用户不存在"
            self.session.add(
                CreditTransaction(
                    user_id=user_id,
                    amount=amount,
                    balance_after=new_balance,
                    transaction_type="admin_adjust",
                    description=description,
                    reference_id=f"admin:{int(actor_user_id)}",
                    idempotency_key=normalized_key,
                )
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._get_idempotent_transaction(normalized_key)
            if (
                existing is not None
                and existing.user_id == user_id
                and existing.amount == amount
            ):
                return True, f"请求已处理。余额 {existing.balance_after} 积分"
            return False, "积分请求与已有幂等操作冲突"
        except Exception:
            await self.session.rollback()
            logger.exception("Admin credit adjustment failed for user %s", user_id)
            return False, "积分调整失败，请重试"
        return True, f"调整成功。变更 {amount} 积分，余额 {new_balance} 积分"

    async def _reservation(self, operation_id: str) -> Optional[CreditReservation]:
        return await self.session.scalar(
            select(CreditReservation).where(
                CreditReservation.operation_id == operation_id
            )
        )

    async def _change_balance(self, user_id: int, delta: int) -> Optional[int]:
        result = await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(credits_balance=User.credits_balance + delta)
            .returning(User.credits_balance)
        )
        return result.scalar_one_or_none()

    async def reserve_for_operation(
        self,
        user_id: int,
        operation_id: str,
        operation_type: str,
        quantity: int = 1,
        *,
        reference_id: Optional[str] = None,
        ttl_seconds: int = 900,
    ) -> Tuple[bool, str, str]:
        """Hold credits before work. Returns (ok, message, state).

        state is created, reserved, committed, or rejected. Only created should run work.
        """
        try:
            normalized_id = self._normalize_idempotency_key(operation_id)
        except ValueError as exc:
            return False, str(exc), "rejected"
        if not normalized_id:
            return False, "操作编号不能为空", "rejected"
        try:
            amount = self.get_operation_cost(operation_type, quantity)
        except ValueError as exc:
            return False, str(exc), "rejected"

        existing = await self._reservation(normalized_id)
        if existing is not None and existing.status != "released":
            if (
                existing.user_id != user_id
                or existing.operation_type != operation_type
                or existing.quantity != quantity
            ):
                return False, "操作编号已被其他积分操作使用", "rejected"
            return True, f"请求已处理。状态 {existing.status}", existing.status
        if existing is not None and (
            existing.user_id != user_id
            or existing.operation_type != operation_type
            or existing.quantity != quantity
        ):
            return False, "操作编号已被其他积分操作使用", "rejected"

        previous_updated_at = existing.updated_at if existing is not None else None
        now = _next_timestamp(previous_updated_at)
        reserve_key = _fit_idempotency_key(
            f"reserve:{normalized_id}"
            if existing is None
            else f"reserve:{normalized_id}:{_attempt_stamp(previous_updated_at)}"
        )
        balance_result = await self.session.execute(
            update(User)
            .where(User.id == user_id, User.credits_balance >= amount)
            .values(credits_balance=User.credits_balance - amount)
            .returning(User.credits_balance)
        )
        new_balance = balance_result.scalar_one_or_none()
        if new_balance is None:
            user_balance = await self.session.scalar(
                select(User.credits_balance).where(User.id == user_id)
            )
            await self.session.rollback()
            if user_balance is None:
                return False, "用户不存在", "rejected"
            return False, f"积分不足。需要 {amount} 积分，当前余额 {user_balance} 积分", "rejected"

        if existing is None:
            self.session.add(
                CreditReservation(
                    user_id=user_id,
                    operation_id=normalized_id,
                    operation_type=operation_type,
                    quantity=quantity,
                    reserved_amount=amount,
                    committed_amount=0,
                    status="reserved",
                    reference_id=reference_id,
                    expires_at=now + max(1, int(ttl_seconds)),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.status = "reserved"
            existing.reserved_amount = amount
            existing.committed_amount = 0
            existing.expires_at = now + max(1, int(ttl_seconds))
            existing.updated_at = now
            existing.reference_id = reference_id
        self.session.add(
            CreditTransaction(
                user_id=user_id,
                amount=-amount,
                balance_after=new_balance,
                transaction_type="reserve",
                description=self._get_default_description(operation_type, quantity),
                reference_id=reference_id,
                idempotency_key=reserve_key,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._reservation(normalized_id)
            if existing is not None and existing.user_id == user_id:
                return True, f"请求已处理。状态 {existing.status}", existing.status
            return False, "操作编号已被其他积分操作使用", "rejected"
        except Exception:
            await self.session.rollback()
            logger.exception("Credit reservation failed for user %s", user_id)
            return False, "积分预留失败，请重试", "rejected"
        return True, f"已预留 {amount} 积分，余额 {new_balance} 积分", "created"

    async def commit_reservation(
        self, operation_id: str, successful_units: int
    ) -> Tuple[bool, str, int]:
        """Keep the charge for successful units and return the unused hold."""
        try:
            normalized_id = self._normalize_idempotency_key(operation_id)
        except ValueError as exc:
            return False, str(exc), 0
        if not isinstance(successful_units, int) or isinstance(successful_units, bool):
            return False, "成功数量必须为整数", 0
        if successful_units < 0:
            return False, "成功数量不能为负数", 0

        reservation = await self.session.scalar(
            select(CreditReservation)
            .where(CreditReservation.operation_id == normalized_id)
            .with_for_update()
        )
        if reservation is None:
            await self.session.rollback()
            return False, "积分预留不存在", 0
        if reservation.status == "committed":
            await self.session.rollback()
            return True, "请求已处理", int(reservation.committed_amount)
        if reservation.status != "reserved":
            await self.session.rollback()
            return False, "积分预留已结束", 0

        unit_cost = reservation.reserved_amount // max(1, reservation.quantity)
        units = min(reservation.quantity, successful_units)
        charge = unit_cost * units
        if charge <= 0:
            ok, message = await self.release_reservation(
                normalized_id, "no successful units"
            )
            return ok, message, 0
        refund = reservation.reserved_amount - charge
        attempt = _attempt_stamp(reservation.updated_at)
        now = _next_timestamp(reservation.updated_at)
        if refund:
            balance = await self._change_balance(reservation.user_id, refund)
            if balance is None:
                await self.session.rollback()
                return False, "用户不存在", 0
            self.session.add(
                CreditTransaction(
                    user_id=reservation.user_id,
                    amount=refund,
                    balance_after=balance,
                    transaction_type="release",
                    description="释放未成功的积分预留",
                    reference_id=reservation.reference_id,
                    idempotency_key=_fit_idempotency_key(
                        f"release-unused:{normalized_id}:{attempt}"
                    ),
                )
            )
        reservation.committed_amount = charge
        reservation.status = "committed"
        reservation.updated_at = now
        await self.session.commit()
        return True, f"已确认 {charge} 积分", charge

    async def renew_reservation(
        self, operation_id: str, *, ttl_seconds: int = 900
    ) -> Tuple[bool, str]:
        """Extend an active hold so the expiry sweeper cannot release live work."""
        try:
            normalized_id = self._normalize_idempotency_key(operation_id)
        except ValueError as exc:
            return False, str(exc)
        if not normalized_id:
            return False, "操作编号不能为空"

        reservation = await self.session.scalar(
            select(CreditReservation)
            .where(CreditReservation.operation_id == normalized_id)
            .with_for_update()
        )
        if reservation is None:
            await self.session.rollback()
            return False, "积分预留不存在"
        if reservation.status != "reserved":
            await self.session.rollback()
            return False, "积分预留已结束"

        now = _next_timestamp(reservation.updated_at)
        reservation.expires_at = now + max(1, int(ttl_seconds))
        reservation.updated_at = now
        await self.session.commit()
        return True, "积分预留已续期"

    async def release_reservation(
        self,
        operation_id: str,
        reason: str = "released",
        *,
        expired_before: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Return a held reservation. A committed operation stays charged."""
        try:
            normalized_id = self._normalize_idempotency_key(operation_id)
        except ValueError as exc:
            return False, str(exc)
        reservation = await self.session.scalar(
            select(CreditReservation)
            .where(CreditReservation.operation_id == normalized_id)
            .with_for_update()
        )
        if reservation is None:
            await self.session.rollback()
            return False, "积分预留不存在"
        if reservation.status == "committed":
            await self.session.rollback()
            return True, "请求已确认，不再释放"
        if reservation.status == "released":
            await self.session.rollback()
            return True, "请求已释放"
        if (
            expired_before is not None
            and reservation.expires_at > float(expired_before)
        ):
            # The sweeper may have selected this operation immediately before its
            # worker renewed the lease. Re-check while holding the row lock.
            await self.session.rollback()
            return False, "积分预留已续期"
        balance = await self._change_balance(
            reservation.user_id, reservation.reserved_amount
        )
        if balance is None:
            await self.session.rollback()
            return False, "用户不存在"
        safe_reason = " ".join(str(reason or "released").split())[:80]
        released_amount = reservation.reserved_amount
        attempt = _attempt_stamp(reservation.updated_at)
        self.session.add(
            CreditTransaction(
                user_id=reservation.user_id,
                amount=released_amount,
                balance_after=balance,
                transaction_type="release",
                description=f"释放积分预留: {safe_reason}",
                reference_id=reservation.reference_id,
                idempotency_key=_fit_idempotency_key(
                    f"release:{normalized_id}:{attempt}"
                ),
            )
        )
        reservation.status = "released"
        reservation.updated_at = _next_timestamp(reservation.updated_at)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            logger.exception("Credit release conflict for %s", normalized_id)
            return False, "积分释放与已有记录冲突"
        return True, f"已释放 {released_amount} 积分"

    async def release_expired_reservations(self, now: Optional[float] = None) -> int:
        """Release holds whose workers never committed. Safe to run repeatedly."""
        moment = time.time() if now is None else now
        expired = list(
            (
                await self.session.scalars(
                    select(CreditReservation.operation_id).where(
                        CreditReservation.status == "reserved",
                        CreditReservation.expires_at <= moment,
                    )
                )
            ).all()
        )
        released = 0
        for operation_id in expired:
            ok, _message = await self.release_reservation(
                operation_id, "expired", expired_before=moment
            )
            if ok:
                released += 1
        return released
    
    async def redeem_code(self, user_id: int, code: str) -> Tuple[bool, str]:
        """
        Redeem a code for credits.
        
        Args:
            user_id: User ID
            code: Redemption code
        
        Returns:
            Tuple of (success, message)
        """
        normalized_code = str(code or "").strip().upper()
        if not normalized_code:
            return False, "兑换码不能为空"
        idempotency_key = f"redemption:{normalized_code}"

        existing = await self._get_idempotent_transaction(idempotency_key)
        if existing:
            if existing.user_id == user_id:
                return True, f"该兑换请求已处理，获得 {existing.amount} 积分"
            return False, "该兑换码已被使用"

        now = time.time()
        try:
            code_result = await self.session.execute(
                update(RedemptionCode)
                .where(
                    RedemptionCode.code == normalized_code,
                    RedemptionCode.is_used == False,
                    or_(RedemptionCode.expires_at.is_(None), RedemptionCode.expires_at > now),
                )
                .values(is_used=True, used_by=user_id, used_at=now)
                .returning(RedemptionCode.id, RedemptionCode.credits_amount)
            )
            claimed = code_result.first()
            if claimed is None:
                code_lookup = await self.session.execute(
                    select(RedemptionCode).where(RedemptionCode.code == normalized_code)
                )
                redemption_code = code_lookup.scalar_one_or_none()
                await self.session.rollback()
                if redemption_code is None:
                    return False, "兑换码不存在"
                if redemption_code.is_used:
                    return False, "该兑换码已被使用"
                return False, "该兑换码已过期"

            credits_amount = int(claimed.credits_amount)
            balance_result = await self.session.execute(
                update(User)
                .where(User.id == user_id)
                .values(credits_balance=User.credits_balance + credits_amount)
                .returning(User.credits_balance)
            )
            new_balance = balance_result.scalar_one_or_none()
            if new_balance is None:
                await self.session.rollback()
                return False, "用户不存在"

            self.session.add(
                CreditTransaction(
                    user_id=user_id,
                    amount=credits_amount,
                    balance_after=new_balance,
                    transaction_type="redemption",
                    description=f"兑换码充值: {normalized_code}",
                    reference_id=normalized_code,
                    idempotency_key=idempotency_key,
                )
            )
            await self.session.commit()
            return True, f"兑换成功！获得 {credits_amount} 积分"
        except IntegrityError:
            await self.session.rollback()
            existing = await self._get_idempotent_transaction(idempotency_key)
            if existing and existing.user_id == user_id:
                return True, f"该兑换请求已处理，获得 {existing.amount} 积分"
            return False, "该兑换码已被使用"
        except Exception:
            await self.session.rollback()
            logger.exception("Atomic redemption failed for user %s", user_id)
            return False, "兑换失败，请重试"
    
    async def get_transaction_history(
        self,
        user_id: int,
        page: int = 1,
        page_size: int = 20,
        transaction_type: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Get user's transaction history"""
        transactions, total = await self.transaction_repo.get_user_transactions(
            user_id, page, page_size, transaction_type
        )
        return [t.to_dict() for t in transactions], total
    
    async def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Get user's credit statistics"""
        balance = await self.get_balance(user_id)
        stats = await self.transaction_repo.get_user_stats(user_id)
        return {
            "balance": balance,
            **stats
        }
    
    def _get_default_description(self, operation_type: str, quantity: int) -> str:
        """Generate default description for operation"""
        descriptions = {
            "outline_generation": "大纲生成",
            "slide_generation": f"幻灯片生成 ({quantity}页)",
            "template_generation": "模板生成",
            "ai_edit": "AI编辑",
            "ai_other": "AI操作"
        }
        return descriptions.get(operation_type, f"操作: {operation_type}")
    
    def get_operation_cost(self, operation_type: str, quantity: int = 1) -> int:
        """Get the cost for an operation without consuming"""
        if operation_type not in self.COSTS:
            raise ValueError("Unknown credit operation type")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        cost_per_item = self.COSTS[operation_type]
        return cost_per_item * quantity


# Convenience function to get credits service
def billable_operation_id(scope: str, *parts: object) -> str:
    """Stable id for one billable attempt. Identical inputs share one hold."""
    import hashlib

    material = "\0".join(str(part or "") for part in (scope, *parts))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def billable_work_may_start(state: str) -> bool:
    """Only a newly created hold, or a non-billable skip, may call the model."""
    return state in {"created", "skipped"}


async def release_expired_credit_reservations() -> int:
    """Release holds left behind by a dead worker."""
    from ..database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await CreditsService(session).release_expired_reservations()


async def sweep_expired_credit_reservations(interval_seconds: float = 30) -> None:
    """Keep releasing expired holds until this task is cancelled."""
    while True:
        try:
            released = await release_expired_credit_reservations()
            if released:
                logger.info("Released %s expired credit reservations", released)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expired credit reservation release failed")
        await asyncio.sleep(interval_seconds)


async def keep_credit_reservation_alive(
    operation_id: str,
    *,
    session_factory=None,
    ttl_seconds: int = 900,
    interval_seconds: Optional[float] = None,
) -> None:
    """Renew a reservation until cancelled by the worker that owns it."""
    from ..database.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal
    interval = (
        max(0.1, min(float(ttl_seconds) / 3, 300.0))
        if interval_seconds is None
        else max(0.01, float(interval_seconds))
    )
    while True:
        await asyncio.sleep(interval)
        try:
            async with factory() as session:
                ok, message = await CreditsService(session).renew_reservation(
                    operation_id, ttl_seconds=ttl_seconds
                )
            if not ok:
                logger.warning(
                    "Stopped credit reservation heartbeat for %s: %s",
                    operation_id,
                    message,
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A transient database failure must not permanently kill renewal. The
            # next tick can still extend the lease before it expires.
            logger.exception(
                "Credit reservation heartbeat failed for %s", operation_id
            )


async def _stop_reservation_heartbeat(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def run_billable_work(
    *,
    user_id: int,
    operation_id: str,
    operation_type: str,
    quantity: int,
    work,
    successful_units,
    reference_id: Optional[str] = None,
    session_factory=None,
    ttl_seconds: int = 900,
) -> Dict[str, Any]:
    """Reserve credits, run work once, then commit successful units or release the hold."""
    from ..database.database import AsyncSessionLocal

    factory = session_factory or AsyncSessionLocal

    async with factory() as session:
        ok, message, state = await CreditsService(session).reserve_for_operation(
            user_id,
            operation_id,
            operation_type,
            quantity,
            reference_id=reference_id,
            ttl_seconds=ttl_seconds,
        )
    if not ok or state in {"committed", "reserved", "released"}:
        return {
            "ok": ok,
            "state": state,
            "message": message,
            "result": None,
            "repeated": state in {"committed", "reserved"},
        }

    heartbeat = asyncio.create_task(
        keep_credit_reservation_alive(
            operation_id,
            session_factory=factory,
            ttl_seconds=ttl_seconds,
        )
    )
    try:
        result = await work()
        units = int(successful_units(result))
    except asyncio.CancelledError:
        await _stop_reservation_heartbeat(heartbeat)
        async with factory() as session:
            await CreditsService(session).release_reservation(operation_id, "cancelled")
        raise
    except Exception:
        await _stop_reservation_heartbeat(heartbeat)
        async with factory() as session:
            await CreditsService(session).release_reservation(operation_id, "failure")
        raise

    await _stop_reservation_heartbeat(heartbeat)
    async with factory() as session:
        committed_ok, committed_message, charged = await CreditsService(
            session
        ).commit_reservation(operation_id, units)
    return {
        "ok": committed_ok,
        "state": "committed" if committed_ok else "commit_failed",
        "message": committed_message,
        "result": result,
        "charged_amount": charged,
        "repeated": False,
    }


def get_credits_service(session: AsyncSession) -> CreditsService:
    """Get credits service instance"""
    return CreditsService(session)
