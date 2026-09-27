"""Dependency checks used by the readiness probe and worker command."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)


async def assess_readiness(config=None) -> list[str]:
    """Return the names of required dependencies that are not usable."""
    if config is None:
        from .config import app_config

        config = app_config

    failures: list[str] = []
    if not await _database_is_ready():
        failures.append("database")
    if _cache_is_required(config) and not await _cache_is_ready(config):
        failures.append("cache")
    if not await _storage_is_ready(config):
        failures.append("storage")
    return failures


def _cache_is_required(config) -> bool:
    backend = str(getattr(config, "cache_backend", "memory") or "memory").lower()
    mode = str(getattr(config, "task_execution_mode", "inline") or "inline").lower()
    return backend == "valkey" or mode == "queue"


async def _database_is_ready() -> bool:
    from ..database.database import async_engine

    if async_engine is None:
        return False
    try:
        async with async_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("Readiness database check failed: %s", exc)
        return False


def _cache_endpoint(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(str(url or "").replace("valkey://", "redis://", 1))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    return host, port


async def _cache_is_ready(config) -> bool:
    """Ping Valkey with a short-lived socket so a refused probe cannot leak threads."""
    host, port = _cache_endpoint(str(getattr(config, "valkey_url", "") or ""))
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=2,
        )
        writer.write(b"PING\r\n")
        await writer.drain()
        reply = await asyncio.wait_for(_reader.readline(), timeout=2)
        return reply.startswith(b"+PONG") or reply.startswith(b"-NOAUTH") or reply.startswith(b"-ERR")
    except Exception as exc:
        logger.warning("Readiness cache check failed: %s", exc)
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _storage_is_ready(config) -> bool:
    backend = str(getattr(config, "storage_backend", "local") or "local").lower()
    try:
        if backend == "local":
            root = Path(str(getattr(config, "local_storage_root", "") or ""))
            return root.is_dir() and os.access(root, os.W_OK)
        if backend == "s3":
            return await _s3_bucket_is_ready(config)
    except Exception as exc:
        logger.warning("Readiness storage check failed: %s", exc)
        return False
    logger.warning("Readiness storage backend is not supported: %s", backend)
    return False


async def _s3_bucket_is_ready(config) -> bool:
    import boto3
    from botocore.client import Config

    bucket = str(getattr(config, "s3_bucket", "") or "").strip()
    if not bucket:
        return False
    client = boto3.client(
        "s3",
        endpoint_url=getattr(config, "s3_endpoint_url", None),
        region_name=str(getattr(config, "s3_region", "") or "us-east-1"),
        aws_access_key_id=getattr(config, "s3_access_key_id", None),
        aws_secret_access_key=getattr(config, "s3_secret_access_key", None),
        config=Config(
            connect_timeout=2,
            read_timeout=2,
            retries={"max_attempts": 1},
            s3={
                "addressing_style": "path"
                if getattr(config, "s3_force_path_style", True)
                else "virtual"
            },
        ),
    )
    await asyncio.to_thread(client.head_bucket, Bucket=bucket)
    return True
