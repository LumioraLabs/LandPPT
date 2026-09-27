"""
Email service for LandPPT - handles sending verification codes
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any, Dict, Optional, Tuple

from ..core.config import app_config

logger = logging.getLogger(__name__)

VERIFICATION_CODE_PREFIX = "hmac:v1:"


def generate_verification_code(length: int = 6) -> str:
    """Generate a cryptographically secure numeric verification code."""
    if length < 4:
        raise ValueError("Verification code length must be at least 4")
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _verification_code_digest(email: str, code: str, code_type: str) -> str:
    """Return a keyed, context-bound digest so codes are not stored in plaintext."""
    message = f"{email.strip().lower()}\0{code_type}\0{code}".encode("utf-8")
    key = str(app_config.secret_key or "").encode("utf-8")
    return VERIFICATION_CODE_PREFIX + hmac.new(key, message, hashlib.sha256).hexdigest()


async def _get_email_settings() -> Dict[str, Any]:
    """Merge encrypted system settings over environment-backed defaults."""
    settings: Dict[str, Any] = {
        "email_provider": app_config.email_provider,
        "smtp_host": app_config.smtp_host,
        "smtp_port": app_config.smtp_port,
        "smtp_user": app_config.smtp_user,
        "smtp_password": app_config.smtp_password,
        "smtp_use_ssl": app_config.smtp_use_ssl,
        "smtp_from_email": app_config.smtp_from_email,
        "smtp_from_name": app_config.smtp_from_name,
        "resend_api_key": app_config.resend_api_key,
        "resend_from_email": app_config.resend_from_email,
        "resend_from_name": app_config.resend_from_name,
    }
    try:
        from ..database.database import AsyncSessionLocal
        from ..database.repositories import UserConfigRepository

        async with AsyncSessionLocal() as session:
            stored = await UserConfigRepository(session).get_configs_by_category(
                None, "email_settings"
            )
        settings.update(stored)
    except Exception:
        logger.exception("Unable to load database-backed email settings")
    return settings


async def send_email(
    to_email: str, subject: str, html_content: str
) -> Tuple[bool, str]:
    """
    Send email via configured provider.
    Returns (success, message) tuple.
    """
    settings = await _get_email_settings()
    provider = str(settings["email_provider"] or "smtp").strip().lower()

    if provider == "resend":
        if not settings["resend_api_key"]:
            logger.warning("Resend not configured, skipping email send")
            return False, "邮件服务未配置"
        if not settings["resend_from_email"]:
            return False, "请配置 RESEND_FROM_EMAIL"

        try:
            try:
                import resend
            except ModuleNotFoundError:
                return False, "Resend 依赖未安装"

            def _send():
                resend.api_key = settings["resend_api_key"]
                from_name = str(settings["resend_from_name"] or "LandPPT").strip()
                from_value = (
                    f"{from_name} <{settings['resend_from_email']}>"
                    if from_name
                    else settings["resend_from_email"]
                )
                params: resend.Emails.SendParams = {
                    "from": from_value,
                    "to": [to_email],
                    "subject": subject,
                    "html": html_content,
                }
                return resend.Emails.send(params)

            await asyncio.to_thread(_send)
            logger.info(f"Email sent successfully to {to_email} via Resend")
            return True, "发送成功"

        except Exception as e:
            logger.error(f"Resend error: {e}")
            return False, "邮件发送失败，请检查服务配置"

    # Default to SMTP
    if not settings["smtp_host"] or not settings["smtp_user"]:
        logger.warning("SMTP not configured, skipping email send")
        return False, "邮件服务未配置"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = (
            f"{settings['smtp_from_name']} "
            f"<{settings['smtp_from_email'] or settings['smtp_user']}>"
        )
        msg["To"] = to_email

        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)

        def _send_smtp():
            if settings["smtp_use_ssl"]:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=30
                )
            else:
                server = smtplib.SMTP(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=30
                )
                server.starttls()

            server.login(settings["smtp_user"], settings["smtp_password"])
            server.sendmail(
                settings["smtp_from_email"] or settings["smtp_user"],
                [to_email],
                msg.as_string(),
            )
            server.quit()

        # smtplib is blocking; connect + login + send can stall the event loop for
        # up to the 30s socket timeout, which stalls every other request on the worker.
        await asyncio.to_thread(_send_smtp)

        logger.info(f"Email sent successfully to {to_email}")
        return True, "发送成功"

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication error: {e}")
        return False, "邮件服务认证失败"
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return False, "邮件发送失败，请检查服务配置"
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False, "邮件发送失败，请稍后重试"


async def create_verification_code(
    email: str, code_type: str  # 'register' or 'reset'
) -> Tuple[bool, str, Optional[str]]:
    """
    Create and store a verification code for email.
    Returns (success, message, code) tuple.
    """
    from sqlalchemy import select, delete
    from ..database.database import AsyncSessionLocal
    from ..database.models import VerificationCode

    code = generate_verification_code()
    expires_at = time.time() + (app_config.verification_code_expire_minutes * 60)

    try:
        async with AsyncSessionLocal() as session:
            # Delete old codes for this email and type
            await session.execute(
                delete(VerificationCode).where(
                    VerificationCode.email == email,
                    VerificationCode.code_type == code_type,
                )
            )

            # Create new code
            verification = VerificationCode(
                email=email,
                code=_verification_code_digest(email, code, code_type),
                code_type=code_type,
                expires_at=expires_at,
            )
            session.add(verification)
            await session.commit()

            return True, "验证码已创建", code

    except Exception as e:
        logger.error(f"Error creating verification code: {e}")
        return False, f"创建验证码失败: {str(e)}", None


async def verify_code(email: str, code: str, code_type: str) -> Tuple[bool, str]:
    """
    Verify a verification code.
    Returns (success, message) tuple.
    """
    from sqlalchemy import select
    from ..database.database import AsyncSessionLocal
    from ..database.models import VerificationCode

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerificationCode)
                .where(
                    VerificationCode.email == email,
                    VerificationCode.code_type == code_type,
                )
                .order_by(VerificationCode.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            verification = result.scalar_one_or_none()

            if not verification:
                return False, "验证码无效"

            if verification.is_used:
                return False, "验证码已使用"

            if time.time() > verification.expires_at:
                return False, "验证码已过期"

            if verification.attempts >= verification.max_attempts:
                return False, "验证码尝试次数过多，请重新获取"

            expected = _verification_code_digest(email, code, code_type)
            stored = str(verification.code or "")
            valid = hmac.compare_digest(stored, expected)
            if not stored.startswith(VERIFICATION_CODE_PREFIX):
                # Transitional compatibility for a short-lived code created by
                # a pre-migration process during a rolling deployment.
                valid = hmac.compare_digest(stored, str(code))
            if not valid:
                verification.attempts += 1
                await session.commit()
                return False, "验证码无效"

            # Mark as used
            verification.is_used = True
            await session.commit()

            return True, "验证成功"

    except Exception as e:
        logger.error(f"Error verifying code: {e}")
        return False, f"验证失败: {str(e)}"


async def send_verification_email(
    email: str, code_type: str  # 'register' or 'reset'
) -> Tuple[bool, str]:
    """
    Generate and send verification code to email.
    Returns (success, message) tuple.
    """
    # Create code
    success, message, code = await create_verification_code(email, code_type)
    if not success:
        return False, message

    # Prepare email content
    if code_type == "register":
        subject = "LandPPT 注册验证码"
        action = "注册账户"
    else:
        subject = "LandPPT 密码重置验证码"
        action = "重置密码"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: 'Inter', Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
            .container {{ max-width: 500px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 40px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); }}
            .logo {{ font-size: 28px; font-weight: 700; color: #111; margin-bottom: 20px; }}
            .code {{ font-size: 36px; font-weight: 700; letter-spacing: 8px; color: #111; background: #f0f0f0; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0; }}
            .info {{ color: #666; font-size: 14px; margin-top: 20px; }}
            .footer {{ color: #999; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="logo">LandPPT</div>
            <p>您好，</p>
            <p>您正在{action}，请使用以下验证码完成验证：</p>
            <div class="code">{code}</div>
            <p class="info">验证码有效期为 {app_config.verification_code_expire_minutes} 分钟。如果您没有进行此操作，请忽略此邮件。</p>
            <div class="footer">
                © LandPPT - AI PPT 生成平台
            </div>
        </div>
    </body>
    </html>
    """

    # Send email
    return await send_email(email, subject, html_content)
