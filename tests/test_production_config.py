"""Production configuration must fail closed before the process migrates or serves."""

import json

import pytest

from landppt.core.config import AppConfig
from landppt.core.production_config import ProductionConfigError, validate_production_config

_PACK = json.dumps(
    {
        "starter": {
            "name": "Starter",
            "credits": 500,
            "amount_minor": 999,
            "currency": "usd",
            "stripe_price_id": "price_test_catalog_starter",
        }
    }
)
_SECRET = "p" * 48
_ENCRYPTION_KEY = "e" * 32
_STRIPE_KEY = "sk_test_" + ("k" * 24)
_WEBHOOK_SECRET = "whsec_" + ("w" * 24)
_METRICS_TOKEN = "m" * 48


def _config(**overrides) -> AppConfig:
    values = {
        "deployment_mode": "production",
        "secret_key": _SECRET,
        "config_encryption_keys": _ENCRYPTION_KEY,
        "bootstrap_admin_enabled": False,
        "bootstrap_admin_username": "ops",
        "bootstrap_admin_password": "not-a-default-password",
        "public_base_url": "https://slides.example.com",
        "allowed_hosts": "slides.example.com",
        "session_cookie_secure": True,
        "csrf_origin_check_enabled": True,
        "trusted_proxy_cidrs": "10.0.0.0/8",
        "cors_allowed_origins": "https://slides.example.com",
        "database_url": "postgresql://landppt:landppt@db.internal:5432/landppt",
        "allow_sqlite_in_production": False,
        "billing_enabled": True,
        "enable_credits_system": True,
        "stripe_secret_key": _STRIPE_KEY,
        "stripe_webhook_secret": _WEBHOOK_SECRET,
        "billing_credit_packs_json": _PACK,
        "metrics_bearer_token": _METRICS_TOKEN,
    }
    values.update(overrides)
    return AppConfig(**values)


def _refusal(setting: str, **overrides) -> str:
    with pytest.raises(ProductionConfigError) as caught:
        validate_production_config(_config(**overrides))
    message = str(caught.value)
    assert setting in message
    for secret in (
        _SECRET,
        _ENCRYPTION_KEY,
        _STRIPE_KEY,
        _WEBHOOK_SECRET,
        _METRICS_TOKEN,
    ):
        assert secret not in message
    return message


def test_development_defaults_are_not_rejected():
    validate_production_config(AppConfig(deployment_mode="development"))
    validate_production_config(
        AppConfig(
            deployment_mode="development",
            secret_key="your-secret-key-here",
            database_url="sqlite:///./landppt.db",
            bootstrap_admin_enabled=True,
            bootstrap_admin_username="admin",
            bootstrap_admin_password="admin123",
        )
    )


def test_complete_production_config_is_accepted():
    validate_production_config(_config())
    validate_production_config(
        _config(
            billing_enabled=False,
            enable_credits_system=False,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
            billing_credit_packs_json="{}",
            database_url="sqlite:///./dev.db",
            allow_sqlite_in_production=True,
        )
    )


@pytest.mark.parametrize(
    ("setting", "overrides"),
    [
        ("SECRET_KEY", {"secret_key": "short"}),
        ("SECRET_KEY", {"secret_key": "your-secret-key-here-your-secret-key-here"}),
        ("LANDPPT_CONFIG_ENCRYPTION_KEYS", {"config_encryption_keys": None}),
        ("LANDPPT_CONFIG_ENCRYPTION_KEYS", {"config_encryption_keys": "too-short"}),
        ("LANDPPT_BOOTSTRAP_ADMIN_ENABLED", {"bootstrap_admin_enabled": True}),
        (
            "LANDPPT_BOOTSTRAP_ADMIN_PASSWORD",
            {
                "bootstrap_admin_enabled": False,
                "bootstrap_admin_username": "admin",
                "bootstrap_admin_password": "admin123",
            },
        ),
        ("LANDPPT_PUBLIC_BASE_URL", {"public_base_url": None}),
        ("LANDPPT_PUBLIC_BASE_URL", {"public_base_url": "http://slides.example.com"}),
        ("LANDPPT_ALLOWED_HOSTS", {"allowed_hosts": "*"}),
        ("LANDPPT_ALLOWED_HOSTS", {"allowed_hosts": "*.example.com"}),
        ("LANDPPT_SESSION_COOKIE_SECURE", {"session_cookie_secure": False}),
        ("LANDPPT_CSRF_ORIGIN_CHECK_ENABLED", {"csrf_origin_check_enabled": False}),
        ("LANDPPT_TRUSTED_PROXY_CIDRS", {"trusted_proxy_cidrs": ""}),
        ("LANDPPT_TRUSTED_PROXY_CIDRS", {"trusted_proxy_cidrs": "not-a-cidr"}),
        ("LANDPPT_CORS_ALLOWED_ORIGINS", {"cors_allowed_origins": "*"}),
        ("LANDPPT_METRICS_BEARER_TOKEN", {"metrics_bearer_token": None}),
        ("LANDPPT_METRICS_BEARER_TOKEN", {"metrics_bearer_token": "short"}),
        (
            "DATABASE_URL",
            {"database_url": "sqlite:///./landppt.db", "allow_sqlite_in_production": False},
        ),
        ("ENABLE_CREDITS_SYSTEM", {"enable_credits_system": False}),
        ("STRIPE_SECRET_KEY", {"stripe_secret_key": ""}),
        ("STRIPE_SECRET_KEY", {"stripe_secret_key": "pk_test_publishable"}),
        ("STRIPE_WEBHOOK_SECRET", {"stripe_webhook_secret": ""}),
        ("LANDPPT_BILLING_CREDIT_PACKS_JSON", {"billing_credit_packs_json": "{}"}),
        ("LANDPPT_BILLING_CREDIT_PACKS_JSON", {"billing_credit_packs_json": "{"}),
        (
            "LANDPPT_BILLING_CREDIT_PACKS_JSON",
            {
                "billing_credit_packs_json": json.dumps(
                    {
                        "starter": {
                            "name": "Starter",
                            "credits": 0,
                            "amount_minor": 999,
                            "currency": "usd",
                            "stripe_price_id": "price_test_catalog_starter",
                        }
                    }
                )
            },
        ),
    ],
)
def test_production_refuses_insecure_settings(setting, overrides):
    _refusal(setting, **overrides)


@pytest.mark.asyncio
async def test_startup_validates_before_migrations(monkeypatch):
    from landppt import main as main_module

    calls = []

    async def _must_not_run():
        calls.append("startup")
        return True

    monkeypatch.setattr(main_module, "app_config", _config(secret_key="short-secret"))
    monkeypatch.setattr(main_module, "run_startup_initialization", _must_not_run)

    with pytest.raises(ProductionConfigError) as caught:
        await main_module.startup_application()

    assert calls == []
    assert "SECRET_KEY" in str(caught.value)
    assert "short-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_valid_production_startup_reaches_initialization(monkeypatch):
    from landppt import main as main_module

    calls = []

    async def _run():
        calls.append("startup")
        return True

    monkeypatch.setattr(main_module, "app_config", _config())
    monkeypatch.setattr(main_module, "run_startup_initialization", _run)

    assert await main_module.startup_application() is True
    assert calls == ["startup"]
