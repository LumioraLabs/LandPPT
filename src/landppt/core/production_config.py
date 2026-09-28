"""Fail-fast checks for an explicit production deployment.

Messages name the setting that must change and never include secret values.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable
from urllib.parse import urlparse

from .config import AppConfig

_PLACEHOLDER_SECRETS = {
    "your-secret-key-here",
    "changeme",
    "change-me",
    "secret",
    "password",
    "admin",
    "admin123",
}
_DEFAULT_BOOTSTRAP_USERS = {"admin", "root", "administrator"}
_DEFAULT_BOOTSTRAP_PASSWORDS = {"admin", "admin123", "password", "changeme"}
_MIN_SECRET_LENGTH = 32


class ProductionConfigError(RuntimeError):
    """Production configuration is incomplete or unsafe."""


def validate_production_config(config: AppConfig) -> None:
    """Reject unsafe settings when deployment mode is production."""
    mode = str(getattr(config, "deployment_mode", "") or "").strip().lower()
    if mode not in {"production", "prod"}:
        return

    errors: list[str] = []
    _check_secret_key(config, errors)
    _check_encryption_keys(config, errors)
    _check_bootstrap_admin(config, errors)
    _check_public_url(config, errors)
    _check_allowed_hosts(config, errors)
    _check_cookies_and_csrf(config, errors)
    _check_trusted_proxies(config, errors)
    _check_cors(config, errors)
    _check_metrics_token(config, errors)
    _check_database(config, errors)
    _check_billing(config, errors)

    if errors:
        raise ProductionConfigError("; ".join(errors))


def _check_secret_key(config: AppConfig, errors: list[str]) -> None:
    secret = str(config.secret_key or "").strip()
    lowered = secret.lower()
    if (
        len(secret) < _MIN_SECRET_LENGTH
        or lowered in _PLACEHOLDER_SECRETS
        or "your-secret-key" in lowered
        or lowered.startswith("change-me")
        or lowered.startswith("changeme")
    ):
        errors.append(
            "SECRET_KEY must be a non-placeholder value of at least "
            f"{_MIN_SECRET_LENGTH} characters"
        )


def _check_encryption_keys(config: AppConfig, errors: list[str]) -> None:
    raw = str(config.config_encryption_keys or "").strip()
    keys = [item.strip() for item in raw.split(",") if item.strip()]
    if not keys or any(len(key) < 16 for key in keys):
        errors.append(
            "LANDPPT_CONFIG_ENCRYPTION_KEYS must contain a dedicated encryption key"
        )


def _check_bootstrap_admin(config: AppConfig, errors: list[str]) -> None:
    if config.bootstrap_admin_enabled:
        errors.append(
            "LANDPPT_BOOTSTRAP_ADMIN_ENABLED must be false after initial provisioning"
        )
    username = str(config.bootstrap_admin_username or "").strip().lower()
    password = str(config.bootstrap_admin_password or "")
    if username in _DEFAULT_BOOTSTRAP_USERS and password in _DEFAULT_BOOTSTRAP_PASSWORDS:
        errors.append(
            "LANDPPT_BOOTSTRAP_ADMIN_USERNAME and LANDPPT_BOOTSTRAP_ADMIN_PASSWORD "
            "must not use default credentials"
        )


def _check_public_url(config: AppConfig, errors: list[str]) -> None:
    parsed = urlparse(str(config.public_base_url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        errors.append("LANDPPT_PUBLIC_BASE_URL must be an https URL")


def _check_allowed_hosts(config: AppConfig, errors: list[str]) -> None:
    hosts = config.get_allowed_hosts()
    if not hosts or any(host == "*" or host.startswith("*.") for host in hosts):
        errors.append("LANDPPT_ALLOWED_HOSTS must list explicit hosts")


def _check_cookies_and_csrf(config: AppConfig, errors: list[str]) -> None:
    if not config.session_cookie_secure:
        errors.append("LANDPPT_SESSION_COOKIE_SECURE must be true")
    if not config.csrf_origin_check_enabled:
        errors.append("LANDPPT_CSRF_ORIGIN_CHECK_ENABLED must be true")


def _check_trusted_proxies(config: AppConfig, errors: list[str]) -> None:
    raw = str(config.trusted_proxy_cidrs or "").strip()
    cidrs = [item.strip() for item in raw.split(",") if item.strip()]
    if not cidrs or not _valid_cidrs(cidrs):
        errors.append(
            "LANDPPT_TRUSTED_PROXY_CIDRS must list proxy networks when "
            "forwarded headers are used"
        )


def _valid_cidrs(cidrs: Iterable[str]) -> bool:
    try:
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return True


def _check_cors(config: AppConfig, errors: list[str]) -> None:
    origins = config.get_cors_allowed_origins()
    if any(origin == "*" for origin in origins):
        errors.append(
            "LANDPPT_CORS_ALLOWED_ORIGINS must not be * when credentialed "
            "production traffic is enabled"
        )


def _check_metrics_token(config: AppConfig, errors: list[str]) -> None:
    token = str(config.metrics_bearer_token or "").strip()
    if len(token) < _MIN_SECRET_LENGTH:
        errors.append(
            "LANDPPT_METRICS_BEARER_TOKEN must be at least "
            f"{_MIN_SECRET_LENGTH} characters"
        )


def _check_database(config: AppConfig, errors: list[str]) -> None:
    url = str(config.database_url or "").strip().lower()
    if url.startswith("sqlite") and not config.allow_sqlite_in_production:
        errors.append(
            "DATABASE_URL must not use SQLite in production unless "
            "LANDPPT_ALLOW_SQLITE_IN_PRODUCTION is true"
        )


def _check_billing(config: AppConfig, errors: list[str]) -> None:
    if not config.billing_enabled:
        return
    if not config.enable_credits_system:
        errors.append(
            "ENABLE_CREDITS_SYSTEM must be true when LANDPPT_BILLING_ENABLED is true"
        )
    stripe_key = str(config.stripe_secret_key or "").strip()
    webhook_secret = str(config.stripe_webhook_secret or "").strip()
    if not (stripe_key.startswith("sk_") or stripe_key.startswith("rk_")) or len(stripe_key) < 20:
        errors.append("STRIPE_SECRET_KEY must be a Stripe secret key")
    if not webhook_secret.startswith("whsec_") or len(webhook_secret) < 16:
        errors.append("STRIPE_WEBHOOK_SECRET must be set")
    _check_pack_catalog(config, errors)


def _check_pack_catalog(config: AppConfig, errors: list[str]) -> None:
    from ..services.billing_service import BillingConfigurationError, load_credit_packs

    try:
        packs = load_credit_packs(config.billing_credit_packs_json)
    except BillingConfigurationError:
        errors.append("LANDPPT_BILLING_CREDIT_PACKS_JSON is empty or invalid")
        return
    if not packs:
        errors.append("LANDPPT_BILLING_CREDIT_PACKS_JSON is empty or invalid")
