"""
Database configuration and session management
"""

import logging
import os
import re
from urllib.parse import urlparse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from ..core.config import app_config

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "sqlite:///./landppt.db"
SQLITE_FALLBACK_URL = DEFAULT_DATABASE_URL
LEGACY_DOCKER_POSTGRES_URL = "postgresql://landppt:landppt@postgres:5432/landppt"
LEGACY_DOCKER_POSTGRES_URL_ALT = "postgres://landppt:landppt@postgres:5432/landppt"


def _resolve_database_urls(database_url: str) -> tuple[str, str, dict, dict, int, int]:
    """Build sync/async database URLs and engine settings from one URL."""
    if database_url.startswith("sqlite:///"):
        async_database_url = database_url.replace("sqlite:///", "sqlite+aiosqlite:///")
        return (
            database_url,
            async_database_url,
            {"check_same_thread": False, "timeout": 30},
            {"timeout": 30},
            100,
            200,
        )

    if database_url.startswith("postgresql://"):
        return (
            database_url,
            database_url.replace("postgresql://", "postgresql+asyncpg://"),
            {},
            {},
            20,
            40,
        )

    if database_url.startswith("postgres://"):
        sync_url = database_url.replace("postgres://", "postgresql://")
        return (
            sync_url,
            database_url.replace("postgres://", "postgresql+asyncpg://"),
            {},
            {},
            20,
            40,
        )

    return (database_url, database_url, {}, {}, 20, 40)


def _is_fallback_eligible_postgres_url(original_url: str) -> bool:
    normalized_url = str(original_url or "").strip()
    if normalized_url in {LEGACY_DOCKER_POSTGRES_URL, LEGACY_DOCKER_POSTGRES_URL_ALT}:
        return True
    try:
        hostname = (urlparse(normalized_url).hostname or "").strip().lower()
    except Exception:
        hostname = ""
    return hostname == "postgres"


def _is_postgres_connectivity_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc or "").lower()
    keywords = (
        "could not translate host name",
        "name or service not known",
        "temporary failure in name resolution",
        "connection refused",
        "timed out",
        "timeout expired",
        "could not connect to server",
        "connection to server at",
        "nodename nor servname provided",
        "no address associated with hostname",
    )
    return any(keyword in message for keyword in keywords)


def _should_fallback_to_sqlite(exc: Exception, original_url: str) -> bool:
    missing_driver = isinstance(exc, ModuleNotFoundError) and exc.name in {"psycopg2", "asyncpg"}
    connectivity_error = _is_postgres_connectivity_error(exc)
    fallback_eligible_url = _is_fallback_eligible_postgres_url(original_url)
    if not fallback_eligible_url:
        return False
    return missing_driver or connectivity_error


def mask_database_url(database_url: str) -> str:
    """Return a database URL safe for logs by redacting the password."""
    value = str(database_url or "")
    try:
        parsed = urlparse(value)
    except Exception:
        return "<invalid>"
    if not parsed.password or "@" not in parsed.netloc:
        return value
    userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    return parsed._replace(netloc=f"{username}:***@{hostinfo}").geturl()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        logger.warning("Invalid %s=%r, using default %s", name, os.getenv(name), default)
        return default
    return max(minimum, value)


def _create_sync_engine(sync_database_url: str, sync_connect_args: dict, pool_size: int, max_overflow: int):
    pool_size = _env_int("DB_POOL_SIZE", pool_size, minimum=1)
    max_overflow = _env_int("DB_MAX_OVERFLOW", max_overflow, minimum=0)
    pool_recycle = _env_int("DB_POOL_RECYCLE", 1800, minimum=1)
    return create_engine(
        sync_database_url,
        connect_args=sync_connect_args,
        echo=False,  # Disable SQL logging to reduce noise
        pool_pre_ping=True,  # Verify connections before using
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=pool_recycle,
    )


def _validate_database_connection(sync_engine, sync_database_url: str) -> None:
    if not str(sync_database_url or "").startswith(("postgresql://", "postgres://")):
        return
    with sync_engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _build_async_engine(async_database_url: str, async_connect_args: dict):
    kwargs = {
        "echo": False,  # Disable SQL logging to reduce noise
        "pool_pre_ping": True,
        "connect_args": async_connect_args if "sqlite" in async_database_url else {},
    }
    if "sqlite" not in async_database_url:
        kwargs.update(
            {
                "pool_size": _env_int("DB_ASYNC_POOL_SIZE", _env_int("DB_POOL_SIZE", 10, minimum=1), minimum=1),
                "max_overflow": _env_int("DB_ASYNC_MAX_OVERFLOW", _env_int("DB_MAX_OVERFLOW", 20), minimum=0),
                "pool_recycle": _env_int("DB_POOL_RECYCLE", 1800, minimum=1),
            }
        )
    return create_async_engine(async_database_url, **kwargs)


def _qident(value: str) -> str:
    """Quote a PostgreSQL identifier safely."""
    return '"' + (str(value or "").replace('"', '""')) + '"'


def _extract_duplicate_pg_type_name(exc: Exception) -> str | None:
    """
    Extract the conflicting PostgreSQL composite type name from a duplicate-type error.

    Example detail:
    Key (typname, typnamespace)=(invite_codes, 2200) already exists.
    """
    message = str(exc or "")
    if "pg_type_typname_nsp_index" not in message:
        return None

    match = re.search(
        r"Key \(typname, typnamespace\)=\(([^,]+),\s*\d+\) already exists",
        message,
    )
    if not match:
        return None
    return (match.group(1) or "").strip().strip('"') or None


def _drop_postgres_orphan_composite_type(connection, table_name: str, schema: str = "public") -> bool:
    """
    Drop an orphan PostgreSQL composite type left behind by a failed CREATE TABLE.

    PostgreSQL creates a composite row type per table. In rare broken states, the
    type can exist without the table relation, which later causes:
    pg_type_typname_nsp_index duplicate key errors on CREATE TABLE.
    """
    dialect_name = getattr(getattr(connection, "dialect", None), "name", "")
    if dialect_name != "postgresql":
        return False

    table_name = (table_name or "").strip()
    schema = (schema or "public").strip() or "public"
    if not table_name:
        return False

    orphan_type_oid = connection.execute(
        text(
            """
            SELECT t.oid
            FROM pg_type t
            JOIN pg_namespace n
              ON n.oid = t.typnamespace
            LEFT JOIN pg_class c
              ON c.oid = t.typrelid
             AND c.relnamespace = t.typnamespace
             AND c.relname = t.typname
             AND c.relkind IN ('r', 'p')
            WHERE n.nspname = :schema
              AND t.typname = :table_name
              AND t.typtype = 'c'
              AND c.oid IS NULL
            """
        ),
        {"schema": schema, "table_name": table_name},
    ).scalar()

    if not orphan_type_oid:
        return False

    connection.execute(
        text(f"DROP TYPE IF EXISTS {_qident(schema)}.{_qident(table_name)} CASCADE")
    )
    logger.warning(
        "Dropped orphan PostgreSQL composite type for missing table: %s.%s",
        schema,
        table_name,
    )
    return True


def _drop_orphan_postgres_table_types(connection, metadata) -> None:
    """Best-effort cleanup for orphan PostgreSQL composite types in metadata tables."""
    from sqlalchemy import inspect

    if getattr(getattr(connection, "dialect", None), "name", "") != "postgresql":
        return

    inspector = inspect(connection)
    for table in metadata.sorted_tables:
        table_name = getattr(table, "name", None)
        if not table_name:
            continue

        schema = getattr(table, "schema", None) or "public"
        try:
            if inspector.has_table(table_name, schema=schema):
                continue
        except Exception:
            # If inspection fails, do not risk destructive cleanup.
            continue

        try:
            _drop_postgres_orphan_composite_type(connection, table_name=table_name, schema=schema)
        except Exception:
            # Best-effort only; create_all may still fail and surface the real error.
            continue


def _resolve_metadata_schema_for_type_name(metadata, type_name: str) -> str:
    """Resolve a metadata table name back to its schema, defaulting to public."""
    normalized = (type_name or "").strip()
    if not normalized:
        return "public"

    for table in metadata.sorted_tables:
        if getattr(table, "name", None) == normalized:
            return getattr(table, "schema", None) or "public"
    return "public"


class _MissingAsyncSessionFactory:
    def __init__(self, error: Exception):
        self._error = error

    def __call__(self, *args, **kwargs):
        raise RuntimeError(
            "Async database session is unavailable because the async database driver is not installed"
        ) from self._error

# Create database URL
configured_database_url = app_config.database_url
selected_database_url = configured_database_url
DATABASE_URL, ASYNC_DATABASE_URL, connect_args, async_connect_args, pool_size, max_overflow = (
    _resolve_database_urls(selected_database_url)
)

try:
    engine = _create_sync_engine(DATABASE_URL, connect_args, pool_size, max_overflow)
    _validate_database_connection(engine, DATABASE_URL)
except Exception as exc:
    if not _should_fallback_to_sqlite(exc, configured_database_url):
        raise

    logger.warning(
        "Default PostgreSQL backend is unavailable for %s; falling back to SQLite at %s",
        mask_database_url(configured_database_url),
        SQLITE_FALLBACK_URL,
    )
    selected_database_url = SQLITE_FALLBACK_URL
    DATABASE_URL, ASYNC_DATABASE_URL, connect_args, async_connect_args, pool_size, max_overflow = (
        _resolve_database_urls(selected_database_url)
    )
    engine = _create_sync_engine(DATABASE_URL, connect_args, pool_size, max_overflow)

logger.info("Database backend selected: %s", mask_database_url(DATABASE_URL))

try:
    async_engine = _build_async_engine(ASYNC_DATABASE_URL, async_connect_args)
    AsyncSessionLocal = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
except Exception as exc:
    if not isinstance(exc, ModuleNotFoundError) or exc.name not in {"asyncpg", "aiosqlite"}:
        raise
    logger.warning(
        "Async database driver is unavailable for %s; async DB endpoints will be disabled until the driver is installed",
        mask_database_url(ASYNC_DATABASE_URL),
    )
    async_engine = None
    AsyncSessionLocal = _MissingAsyncSessionFactory(exc)


# Create session makers
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False  # Prevent errors after commit
)
def get_db():
    """Dependency to get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db():
    """Dependency to get async database session"""
    if async_engine is None:
        raise RuntimeError("Async database engine is unavailable")
    async with AsyncSessionLocal() as session:
        yield session


async def init_db(*, bootstrap_admin: bool = True):
    """Initialize database tables"""
    # Import here to avoid circular imports
    from .models import Base
    import logging
    from sqlalchemy import inspect
    
    logger = logging.getLogger(__name__)

    # PostgreSQL safety: pre-clean known orphan composite types before create_all.
    async with async_engine.begin() as conn:
        await conn.run_sync(lambda connection: _drop_orphan_postgres_table_types(connection, Base.metadata))

    try:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    except Exception as exc:
        duplicate_type_name = _extract_duplicate_pg_type_name(exc)
        if not duplicate_type_name:
            raise

        schema = _resolve_metadata_schema_for_type_name(Base.metadata, duplicate_type_name)
        logger.warning(
            "Detected PostgreSQL duplicate composite type during create_all for %s.%s; "
            "attempting orphan-type cleanup and one retry",
            schema,
            duplicate_type_name,
        )

        async with async_engine.begin() as conn:
            await conn.run_sync(
                lambda connection: _drop_postgres_orphan_composite_type(
                    connection,
                    table_name=duplicate_type_name,
                    schema=schema,
                )
            )

        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    # Check and add missing OAuth columns for existing databases
    def add_missing_columns(connection):
        inspector = inspect(connection)
        if inspector.has_table("users"):
            existing_columns = {col['name'] for col in inspector.get_columns("users")}
            
            # Define columns to add if missing
            columns_to_add = []
            if 'github_id' not in existing_columns:
                columns_to_add.append("ADD COLUMN github_id VARCHAR(50) UNIQUE")
            if 'linuxdo_id' not in existing_columns:
                columns_to_add.append("ADD COLUMN linuxdo_id VARCHAR(50) UNIQUE")
            if 'oauth_provider' not in existing_columns:
                columns_to_add.append("ADD COLUMN oauth_provider VARCHAR(20)")
            if 'register_ip' not in existing_columns:
                columns_to_add.append("ADD COLUMN register_ip VARCHAR(45)")
            if 'last_login_ip' not in existing_columns:
                columns_to_add.append("ADD COLUMN last_login_ip VARCHAR(45)")
            if 'registration_channel' not in existing_columns:
                columns_to_add.append("ADD COLUMN registration_channel VARCHAR(20)")
            if 'invite_code_id' not in existing_columns:
                columns_to_add.append("ADD COLUMN invite_code_id INTEGER")
             
            for col_sql in columns_to_add:
                try:
                    connection.execute(text(f"ALTER TABLE users {col_sql}"))
                    logger.info(f"Added column to users table: {col_sql}")
                except Exception as e:
                    # Column might already exist, ignore error
                    logger.debug(f"Column already exists or error: {e}")

    async with async_engine.begin() as conn:
        await conn.run_sync(add_missing_columns)

    if bootstrap_admin:
        # Optionally bootstrap an initial admin user.
        from ..auth.auth_service import init_default_admin
        db = SessionLocal()
        try:
            init_default_admin(db)
        finally:
            db.close()


def _install_daemon_sqlite_workers() -> None:
    """aiosqlite workers are non-daemon and block interpreter shutdown.

    Pooled connections stay open for the life of the process. Mark their
    worker threads daemon so exit does not wait on an idle pool.
    """
    try:
        import aiosqlite.core as core
    except Exception:
        return
    if getattr(core.Connection.__await__, "_landppt_daemon_workers", False):
        return
    original = core.Connection.__await__

    def _await_with_daemon_worker(self):
        self._thread.daemon = True
        return original(self)

    _await_with_daemon_worker._landppt_daemon_workers = True
    core.Connection.__await__ = _await_with_daemon_worker


def _stop_sqlite_worker_threads() -> None:
    """Stop pooled aiosqlite workers so process exit does not wait on them."""
    try:
        import threading

        from aiosqlite.core import _STOP_RUNNING_SENTINEL
    except Exception:
        return

    def _stop():
        return _STOP_RUNNING_SENTINEL

    for thread in threading.enumerate():
        if not thread.is_alive() or thread.daemon:
            continue
        target = getattr(thread, "_target", None)
        if getattr(target, "__name__", "") != "_connection_worker_thread":
            continue
        args = getattr(thread, "_args", None) or ()
        if not args:
            continue
        try:
            args[0].put_nowait((None, _stop))
        except Exception:
            continue
        thread.join(timeout=2)


_install_daemon_sqlite_workers()


async def close_db():
    """Close database connections"""
    if async_engine is not None:
        await async_engine.dispose()
    _stop_sqlite_worker_threads()

