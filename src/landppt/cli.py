"""Operational CLI commands for LandPPT deployments."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import sys
import time
from pathlib import Path

import click

from .auth.auth_service import init_default_admin
from .database.create_default_template import ensure_default_templates_exist
from .database.database import SessionLocal, close_db, init_db
from .database.migrations import migration_manager


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _migrate() -> bool:
    from .core.config import app_config
    from .core.production_config import validate_production_config

    validate_production_config(app_config)
    await init_db(bootstrap_admin=False)
    return await migration_manager.migrate_up()


async def _bootstrap_default_templates() -> int:
    template_ids = await ensure_default_templates_exist()
    return len(template_ids or [])


def _bootstrap_admin() -> None:
    db = SessionLocal()
    try:
        init_default_admin(db)
    finally:
        db.close()


async def _backfill_image_artifacts(cache_dirs: tuple[str, ...], default_user_id: int | None = None) -> dict[str, int]:
    """Backfill legacy filesystem image cache files into artifact storage."""
    await init_db(bootstrap_admin=False)
    ok = await migration_manager.migrate_up()
    if not ok:
        raise RuntimeError("Database migration failed")

    from .services.image.models import ImageFormat, ImageInfo, ImageMetadata, ImageProvider, ImageSourceType
    from .services.storage import get_artifact_service

    artifact_service = get_artifact_service()
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".tiff", ".tif"}
    skipped = 0
    created = 0
    updated = 0
    failed = 0

    def _format_for_suffix(suffix: str) -> ImageFormat:
        return {
            ".jpg": ImageFormat.JPEG,
            ".jpeg": ImageFormat.JPEG,
            ".png": ImageFormat.PNG,
            ".webp": ImageFormat.WEBP,
            ".gif": ImageFormat.GIF,
            ".svg": ImageFormat.SVG,
            ".bmp": ImageFormat.BMP,
            ".tiff": ImageFormat.TIFF,
            ".tif": ImageFormat.TIFF,
        }.get(suffix.lower(), ImageFormat.JPEG)

    def _iter_files(root: Path):
        if not root.exists():
            return
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "metadata" in path.parts or "thumbnails" in path.parts or ".artifact_materialized" in path.parts:
                continue
            if path.suffix.lower() in image_exts:
                yield path

    for cache_dir in cache_dirs:
        root = Path(cache_dir)
        for file_path in _iter_files(root) or []:
            cache_key = file_path.stem
            try:
                existing = await artifact_service.get_task_artifact(cache_key, artifact_type="image_cache")
                metadata_path = root / "metadata" / f"{cache_key}.json"
                image_info = None
                if metadata_path.exists():
                    try:
                        image_info = ImageInfo(**json.loads(metadata_path.read_text(encoding="utf-8")))
                    except Exception as exc:
                        logger.warning("Invalid image metadata %s: %s", metadata_path, exc)

                stat = file_path.stat()
                if image_info is None:
                    owner_user_id = default_user_id
                    if owner_user_id is None:
                        match = re.match(r"^u(\d+)_", cache_key)
                        if match:
                            owner_user_id = int(match.group(1))
                    if owner_user_id is None:
                        skipped += 1
                        continue

                    rel_parts = file_path.relative_to(root).parts if file_path.is_relative_to(root) else file_path.parts
                    source_type = ImageSourceType.LOCAL_STORAGE
                    provider = ImageProvider.LOCAL_STORAGE
                    if "ai_generated" in rel_parts:
                        source_type = ImageSourceType.AI_GENERATED
                        provider = ImageProvider.OPENAI_IMAGE
                    elif "web_search" in rel_parts:
                        source_type = ImageSourceType.WEB_SEARCH
                        provider = ImageProvider.UNSPLASH

                    image_info = ImageInfo(
                        image_id=cache_key,
                        owner_user_id=owner_user_id,
                        source_type=source_type,
                        provider=provider,
                        original_url="",
                        local_path=str(file_path),
                        filename=file_path.name,
                        title=file_path.name,
                        metadata=ImageMetadata(
                            width=0,
                            height=0,
                            format=_format_for_suffix(file_path.suffix),
                            file_size=stat.st_size,
                        ),
                        created_at=stat.st_ctime,
                        updated_at=stat.st_mtime,
                    )
                else:
                    image_info.image_id = cache_key
                    image_info.local_path = str(file_path)
                    if image_info.owner_user_id is None:
                        image_info.owner_user_id = default_user_id
                    if image_info.owner_user_id is None:
                        skipped += 1
                        continue

                metadata_json = image_info.model_dump(mode="json")
                if existing:
                    await artifact_service.update_artifact_metadata(existing.id, metadata_json=metadata_json)
                    updated += 1
                    continue

                content_type = mimetypes.guess_type(image_info.filename or file_path.name)[0]
                if not content_type:
                    content_type = f"image/{image_info.metadata.format.value}"
                await artifact_service.save_file(
                    local_path=str(file_path),
                    user_id=int(image_info.owner_user_id),
                    task_id=cache_key,
                    artifact_type="image_cache",
                    filename=image_info.filename or file_path.name,
                    content_type=content_type,
                    metadata_json=metadata_json,
                )
                created += 1
            except Exception as exc:
                failed += 1
                logger.warning("Failed to backfill image artifact %s: %s", file_path, exc)

    return {"created": created, "updated": updated, "skipped": skipped, "failed": failed}


async def _run_with_db_close(coro):
    try:
        return await coro
    finally:
        await close_db()


def _run_async(coro):
    return asyncio.run(_run_with_db_close(coro))


@click.group()
def cli() -> None:
    """Run LandPPT operational commands."""


@cli.command("migrate")
def migrate() -> None:
    """Initialize database tables and apply pending migrations."""
    ok = _run_async(_migrate())
    if not ok:
        raise click.ClickException("Database migration failed")
    click.echo("Database migration completed")


@cli.command("bootstrap-default-templates")
def bootstrap_default_templates() -> None:
    """Create default templates when none exist."""
    count = _run_async(_bootstrap_default_templates())
    click.echo(f"Default template bootstrap completed: {count} available")


@cli.command("bootstrap-admin")
def bootstrap_admin() -> None:
    """Create the initial admin user when bootstrap is enabled and no users exist."""
    _bootstrap_admin()
    click.echo("Admin bootstrap completed")


@cli.command("migrate-and-bootstrap")
def migrate_and_bootstrap() -> None:
    """Run migrations, then bootstrap templates and the optional initial admin."""
    ok = _run_async(_migrate())
    if not ok:
        raise click.ClickException("Database migration failed")
    count = _run_async(_bootstrap_default_templates())
    _bootstrap_admin()
    click.echo(f"Migration and bootstrap completed: {count} templates available")


@cli.command("backfill-image-artifacts")
@click.option(
    "--cache-dir",
    "cache_dirs",
    multiple=True,
    default=("/app/temp/images_cache", "/app/temp/ai_responses_cache/images_cache"),
    help="Legacy image cache directory to scan. Can be provided multiple times.",
)
@click.option("--default-user-id", type=int, default=None, help="Owner for files without metadata; omitted files are skipped by default.")
def backfill_image_artifacts(cache_dirs: tuple[str, ...], default_user_id: int | None = None) -> None:
    """Copy legacy filesystem image gallery files into artifact storage (S3/MinIO)."""
    result = _run_async(_backfill_image_artifacts(cache_dirs=cache_dirs, default_user_id=default_user_id))
    click.echo(
        "Image artifact backfill completed: "
        f"created={result['created']} updated={result['updated']} "
        f"skipped={result['skipped']} failed={result['failed']}"
    )


async def _reconcile_payments() -> dict[str, int]:
    from .core.config import app_config
    from .core.production_config import validate_production_config
    from .database.database import AsyncSessionLocal
    from .services.billing_service import BillingService

    validate_production_config(app_config)
    async with AsyncSessionLocal() as session:
        return await BillingService(session).reconcile_stored_events()


@cli.command("reconcile-payments")
def reconcile_payments() -> None:
    """Repair local payment orders from stored webhook receipts."""
    result = _run_async(_reconcile_payments())
    click.echo(
        "Payment reconciliation completed: "
        f"repaired={result['repaired']} stalled={result['stalled']} "
        f"unfulfilled={result['unfulfilled']}"
    )


@cli.command("ready")
def ready() -> None:
    """Exit 0 only when database, cache/queue, and storage are usable."""
    from .core.readiness import assess_readiness

    failures = _run_async(assess_readiness())
    if failures:
        raise click.ClickException("Not ready: " + ", ".join(failures))
    click.echo("ready")


@cli.command("worker")
@click.option("--queue", "queue_name", default=None, help="Queue name to consume. Defaults to TASK_QUEUE_NAME.")
def worker(queue_name: str | None = None) -> None:
    """Run a queued task worker process."""
    from .core.config import app_config
    from .core.production_config import validate_production_config
    from .tasks.worker import run_worker

    validate_production_config(app_config)
    _run_async(run_worker(queue_name=queue_name))


def main() -> None:
    cli(prog_name="landppt")


if __name__ == "__main__":
    sys.exit(main())
