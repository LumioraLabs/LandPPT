import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


async def _config_database(tmp_path):
    from landppt.database.models import Base, User, UserConfig

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'secrets.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[User.__table__, UserConfig.__table__],
            )
        )
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest.mark.asyncio
async def test_password_config_is_encrypted_at_rest_and_redacted(tmp_path, monkeypatch):
    from landppt.core.config import app_config
    from landppt.core.secret_storage import ENCRYPTED_VALUE_PREFIX, _cipher
    from landppt.database.models import UserConfig
    from landppt.database.repositories import UserConfigRepository

    monkeypatch.setattr(app_config, "config_encryption_keys", "test-encryption-key")
    _cipher.cache_clear()
    engine, maker = await _config_database(tmp_path)
    try:
        async with maker() as session:
            repo = UserConfigRepository(session)
            assert await repo.set_config(
                None,
                "provider_api_key",
                "plain-secret-value",
                config_type="password",
                category="ai_providers",
            )
            await session.commit()

        async with maker() as session:
            record = await session.scalar(
                select(UserConfig).where(UserConfig.config_key == "provider_api_key")
            )
            assert record is not None
            assert record.config_value.startswith(ENCRYPTED_VALUE_PREFIX)
            assert "plain-secret-value" not in record.config_value
            assert record.to_dict()["config_value"] == "********"
            assert (
                await UserConfigRepository(session).get_config(None, "provider_api_key")
                == "plain-secret-value"
            )
    finally:
        _cipher.cache_clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_plaintext_config_remains_readable(tmp_path):
    from landppt.database.models import UserConfig
    from landppt.database.repositories import UserConfigRepository

    engine, maker = await _config_database(tmp_path)
    try:
        async with maker() as session:
            session.add(
                UserConfig(
                    config_key="legacy_secret",
                    config_value="legacy-plaintext",
                    config_type="password",
                    category="ai_providers",
                )
            )
            await session.commit()
        async with maker() as session:
            assert (
                await UserConfigRepository(session).get_config(None, "legacy_secret")
                == "legacy-plaintext"
            )
    finally:
        await engine.dispose()


async def _verification_database(tmp_path):
    from landppt.database.models import Base, VerificationCode

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'codes.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=[VerificationCode.__table__]
            )
        )
    return engine, async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest.mark.asyncio
async def test_verification_codes_are_hashed_and_attempt_limited(tmp_path, monkeypatch):
    from landppt.core.config import app_config
    from landppt.database import database as database_module
    from landppt.database.models import VerificationCode
    from landppt.services.email_service import (
        VERIFICATION_CODE_PREFIX,
        create_verification_code,
        verify_code,
    )

    engine, maker = await _verification_database(tmp_path)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", maker)
    monkeypatch.setattr(app_config, "secret_key", "verification-test-secret")
    try:
        created, _, code = await create_verification_code(
            "person@example.com", "register"
        )
        assert created is True and code

        async with maker() as session:
            stored = await session.scalar(select(VerificationCode))
            assert stored is not None
            assert stored.code.startswith(VERIFICATION_CODE_PREFIX)
            assert code not in stored.code

        for _ in range(5):
            success, _ = await verify_code("person@example.com", "000000", "register")
            assert success is False
        success, message = await verify_code("person@example.com", code, "register")
        assert success is False
        assert "次数过多" in message

        _, _, replacement = await create_verification_code(
            "person@example.com", "register"
        )
        success, _ = await verify_code("person@example.com", replacement, "register")
        assert success is True
    finally:
        await engine.dispose()


def test_security_data_migrations_are_registered():
    from landppt.database.migrations import migration_manager

    migrations = {
        item["version"]: item["name"] for item in migration_manager.migrations
    }
    assert migrations["020"] == "encrypt_stored_config_secrets"
    assert migrations["021"] == "harden_verification_codes"


def test_database_config_service_does_not_write_environment_files():
    from pathlib import Path

    source = Path("src/landppt/services/db_config_service.py").read_text(
        encoding="utf-8"
    )
    assert "get_config_service" not in source
    assert "system_only_updates" not in source
