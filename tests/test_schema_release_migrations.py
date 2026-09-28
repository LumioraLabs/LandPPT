"""Fresh, upgrade, rollback, and encryption-failure paths through the shipped runner."""

import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _bind_database(monkeypatch, database_url: str):
    import landppt.database.migrations as migrations

    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(migrations, "AsyncSessionLocal", maker)
    monkeypatch.setattr(migrations, "async_engine", engine)
    return migrations.DatabaseMigration(), engine


def _bind_temp_database(monkeypatch, db_path):
    return _bind_database(monkeypatch, f"sqlite+aiosqlite:///{db_path}")


async def _tables(engine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
        return {row[0] for row in rows}


async def _columns(engine, table: str) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in rows}


async def _rebuild_without_columns(engine, table: str, drop_columns: set[str]) -> None:
    """SQLite cannot drop an indexed column in place; rebuild the released shape."""
    async with engine.begin() as connection:
        info = (
            await connection.execute(text(f'PRAGMA table_info("{table}")'))
        ).fetchall()
        keep = [row for row in info if row[1] not in drop_columns]
        if len(keep) == len(info):
            return
        definitions = []
        for _cid, name, coltype, notnull, default, primary_key in keep:
            piece = f'"{name}" {coltype or "TEXT"}'
            if primary_key:
                piece += " PRIMARY KEY"
            elif notnull:
                piece += " NOT NULL"
            if default is not None:
                piece += f" DEFAULT ({default})"
            definitions.append(piece)
        names = ", ".join(f'"{row[1]}"' for row in keep)
        await connection.execute(text("PRAGMA foreign_keys=OFF"))
        await connection.execute(text(f'ALTER TABLE "{table}" RENAME TO "{table}_released"'))
        await connection.execute(
            text(f'CREATE TABLE "{table}" ({", ".join(definitions)})')
        )
        await connection.execute(
            text(
                f'INSERT INTO "{table}" ({names}) '
                f'SELECT {names} FROM "{table}_released"'
            )
        )
        await connection.execute(text(f'DROP TABLE "{table}_released"'))


async def _scalar(engine, statement: str, **params):
    async with engine.connect() as connection:
        result = await connection.execute(text(statement), params)
        return result.scalar()


@pytest.mark.asyncio
async def test_fresh_sqlite_migrates_from_001_through_022(tmp_path, monkeypatch):
    manager, engine = _bind_temp_database(monkeypatch, tmp_path / "fresh.sqlite3")
    try:
        assert await manager.migrate_up() is True
        status = await manager.get_migration_status()
        assert status["current_version"] == "023"
        assert status["pending_migrations"] == []
        assert status["applied_migrations"][0] == "001"
        assert "022" in status["applied_migrations"]
        assert status["applied_migrations"][-1] == "023"

        tables = await _tables(engine)
        assert {"payment_orders", "payment_webhook_events", "credit_transactions"} <= tables
        credit_columns = await _columns(engine, "credit_transactions")
        assert "idempotency_key" in credit_columns
        verification_columns = await _columns(engine, "verification_codes")
        assert {"attempts", "max_attempts"} <= verification_columns
        index_names = await _scalar(
            engine,
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_credit_transactions_idempotency_key'",
        )
        assert index_names == "uq_credit_transactions_idempotency_key"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_upgrade_from_018_applies_019_through_022(tmp_path, monkeypatch):
    from landppt.core.secret_storage import ENCRYPTED_VALUE_PREFIX
    from landppt.services.email_service import VERIFICATION_CODE_PREFIX

    manager, engine = _bind_temp_database(monkeypatch, tmp_path / "upgrade.sqlite3")
    try:
        assert await manager.migrate_up(target_version="018") is True
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS payment_webhook_events"))
            await connection.execute(text("DROP TABLE IF EXISTS payment_orders"))
        await _rebuild_without_columns(
            engine, "credit_transactions", {"idempotency_key"}
        )
        await _rebuild_without_columns(
            engine, "verification_codes", {"attempts", "max_attempts"}
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO user_configs "
                    "(user_id, config_key, config_value, config_type, category, created_at, updated_at) "
                    "VALUES (NULL, 'smtp_password', :value, 'password', 'email', :now, :now)"
                ),
                {"value": "plaintext-smtp-secret", "now": time.time()},
            )
            await connection.execute(
                text(
                    "INSERT INTO verification_codes "
                    "(email, code, code_type, expires_at, is_used, created_at) "
                    "VALUES ('person@example.com', '123456', 'register', :expires, 0, :now)"
                ),
                {"expires": time.time() + 600, "now": time.time()},
            )

        assert "idempotency_key" not in await _columns(engine, "credit_transactions")
        assert "payment_orders" not in await _tables(engine)

        assert await manager.migrate_up() is True
        assert "idempotency_key" in await _columns(engine, "credit_transactions")
        assert {"attempts", "max_attempts"} <= await _columns(engine, "verification_codes")
        assert {"payment_orders", "payment_webhook_events"} <= await _tables(engine)

        stored_secret = await _scalar(
            engine,
            "SELECT config_value FROM user_configs WHERE config_key = 'smtp_password'",
        )
        assert str(stored_secret).startswith(ENCRYPTED_VALUE_PREFIX)
        assert "plaintext-smtp-secret" not in str(stored_secret)

        stored_code = await _scalar(
            engine,
            "SELECT code FROM verification_codes WHERE email = 'person@example.com'",
        )
        assert str(stored_code).startswith(VERIFICATION_CODE_PREFIX)
        assert stored_code != "123456"
        status = await manager.get_migration_status()
        assert status["current_version"] == "023"
        assert "lease_expires_at" in await _columns(engine, "payment_webhook_events")
        assert "credit_reservations" in await _tables(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rollback_of_payment_migration_restores_prior_schema(tmp_path, monkeypatch):
    manager, engine = _bind_temp_database(monkeypatch, tmp_path / "rollback.sqlite3")
    try:
        assert await manager.migrate_up() is True
        assert "payment_orders" in await _tables(engine)
        assert await manager.migrate_down("021") is True
        assert "payment_orders" not in await _tables(engine)
        assert "payment_webhook_events" not in await _tables(engine)
        status = await manager.get_migration_status()
        assert "022" not in status["applied_migrations"]
        assert status["current_version"] == "021"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_secret_encryption_stops_startup_without_rewriting_secrets(
    tmp_path, monkeypatch
):
    import landppt.database.migrations as migrations
    from landppt.database.startup_migrations import run_startup_migrations

    manager, engine = _bind_temp_database(monkeypatch, tmp_path / "encrypt-fail.sqlite3")
    plaintext = "plaintext-must-survive"
    ciphertext = "enc:v1:already-ciphertext"
    try:
        assert await manager.migrate_up(target_version="019") is True
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO user_configs "
                    "(user_id, config_key, config_value, config_type, category, created_at, updated_at) "
                    "VALUES (NULL, 'smtp_password', :value, 'password', 'email', :now, :now)"
                ),
                {"value": plaintext, "now": time.time()},
            )
            await connection.execute(
                text(
                    "INSERT INTO user_configs "
                    "(user_id, config_key, config_value, config_type, category, created_at, updated_at) "
                    "VALUES (NULL, 'api_key', :value, 'password', 'provider', :now, :now)"
                ),
                {"value": ciphertext, "now": time.time()},
            )

        def _explode(_value):
            raise RuntimeError("encryption backend unavailable")

        monkeypatch.setattr(migrations, "encrypt_secret", _explode, raising=False)
        import landppt.core.secret_storage as secret_storage

        monkeypatch.setattr(secret_storage, "encrypt_secret", _explode)

        assert await manager.migrate_up() is False
        assert (
            await _scalar(
                engine,
                "SELECT config_value FROM user_configs WHERE config_key = 'smtp_password'",
            )
            == plaintext
        )
        assert (
            await _scalar(
                engine,
                "SELECT config_value FROM user_configs WHERE config_key = 'api_key'",
            )
            == ciphertext
        )

        with pytest.raises(RuntimeError, match="Startup migrations failed"):
            await run_startup_migrations()

        assert (
            await _scalar(
                engine,
                "SELECT config_value FROM user_configs WHERE config_key = 'smtp_password'",
            )
            == plaintext
        )
        assert (
            await _scalar(
                engine,
                "SELECT config_value FROM user_configs WHERE config_key = 'api_key'",
            )
            == ciphertext
        )
    finally:
        await engine.dispose()


def _postgres_url() -> str:
    raw = os.environ.get("LANDPPT_TEST_POSTGRES_URL", "").strip()
    if not raw:
        pytest.skip("LANDPPT_TEST_POSTGRES_URL is not set")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw[len("postgres://") :]
    return raw


async def _reset_postgres(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))


async def _strip_post_018_objects(engine) -> None:
    dialect = engine.dialect.name
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS payment_webhook_events"))
        await connection.execute(text("DROP TABLE IF EXISTS payment_orders"))
    if dialect == "sqlite":
        await _rebuild_without_columns(engine, "credit_transactions", {"idempotency_key"})
        await _rebuild_without_columns(
            engine, "verification_codes", {"attempts", "max_attempts"}
        )
        return
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE credit_transactions DROP COLUMN IF EXISTS idempotency_key")
        )
        await connection.execute(
            text("ALTER TABLE verification_codes DROP COLUMN IF EXISTS attempts")
        )
        await connection.execute(
            text("ALTER TABLE verification_codes DROP COLUMN IF EXISTS max_attempts")
        )


async def _insert_pre_upgrade_rows(engine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO user_configs "
                "(user_id, config_key, config_value, config_type, category, created_at, updated_at) "
                "VALUES (NULL, 'smtp_password', :value, 'password', 'email', :now, :now)"
            ),
            {"value": "plaintext-smtp-secret", "now": time.time()},
        )
        await connection.execute(
            text(
                "INSERT INTO verification_codes "
                "(email, code, code_type, expires_at, is_used, created_at) "
                "VALUES ('person@example.com', '123456', 'register', :expires, :used, :now)"
            ),
            {"expires": time.time() + 600, "now": time.time(), "used": False},
        )


@pytest.mark.asyncio
async def test_postgres_fresh_and_018_upgrade(monkeypatch):
    from landppt.core.secret_storage import ENCRYPTED_VALUE_PREFIX
    from landppt.services.email_service import VERIFICATION_CODE_PREFIX

    manager, engine = _bind_database(monkeypatch, _postgres_url())
    try:
        await _reset_postgres(engine)
        assert await manager.migrate_up() is True
        status = await manager.get_migration_status()
        assert status["current_version"] == "023"
        assert status["pending_migrations"] == []

        await _reset_postgres(engine)
        assert await manager.migrate_up(target_version="018") is True
        await _strip_post_018_objects(engine)
        await _insert_pre_upgrade_rows(engine)
        assert await manager.migrate_up() is True
        stored_secret = await _scalar(
            engine,
            "SELECT config_value FROM user_configs WHERE config_key = 'smtp_password'",
        )
        stored_code = await _scalar(
            engine,
            "SELECT code FROM verification_codes WHERE email = 'person@example.com'",
        )
        assert str(stored_secret).startswith(ENCRYPTED_VALUE_PREFIX)
        assert "plaintext-smtp-secret" not in str(stored_secret)
        assert str(stored_code).startswith(VERIFICATION_CODE_PREFIX)
        assert stored_code != "123456"
        tables = await _scalar(
            engine,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('payment_orders', 'payment_webhook_events')",
        )
        assert tables == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_concurrent_credit_consumption_cannot_overspend(monkeypatch):
    from sqlalchemy import func, select

    from landppt.database.models import CreditTransaction, User
    from landppt.services.credits_service import CreditsService

    _manager, engine = _bind_database(monkeypatch, _postgres_url())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _reset_postgres(engine)
        assert await _manager.migrate_up() is True
        async with maker() as session:
            user = User(
                username="spender",
                email="spender@example.com",
                is_active=True,
                is_admin=False,
                credits_balance=5,
            )
            user.set_password("a sufficiently long password")
            session.add(user)
            await session.commit()
            user_id = user.id

        async def consume(key):
            async with maker() as session:
                return await CreditsService(session).consume_credits(
                    user_id,
                    "slide_generation",
                    idempotency_key=key,
                )

        import asyncio

        results = await asyncio.gather(consume("consume-one"), consume("consume-two"))
        assert [success for success, _message in results].count(True) == 1
        async with maker() as session:
            balance = await session.scalar(
                select(User.credits_balance).where(User.id == user_id)
            )
            transaction_count = await session.scalar(
                select(func.count(CreditTransaction.id)).where(
                    CreditTransaction.user_id == user_id
                )
            )
        assert balance == 0
        assert transaction_count == 1
    finally:
        await engine.dispose()
