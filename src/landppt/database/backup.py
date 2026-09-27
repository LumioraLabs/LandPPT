"""Local database backup and restore.

Production PostgreSQL still uses pg_dump and pg_restore. This helper shells
out to the sqlite3 CLI when it is installed, and otherwise shells out to the
Python sqlite3 module's backup API. A missing tool is reported as a process
error rather than treated as a successful copy.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _run_sqlite(args: list[str]) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("sqlite3")
    if binary:
        return subprocess.run(
            [binary, *args],
            check=False,
            capture_output=True,
            text=True,
        )
    script = (
        "import sqlite3, sys\n"
        "source, destination, action = sys.argv[1:]\n"
        "if action == 'backup':\n"
        "    src = sqlite3.connect(source)\n"
        "    dst = sqlite3.connect(destination)\n"
        "    with dst:\n"
        "        src.backup(dst)\n"
        "    src.close(); dst.close()\n"
        "elif action == 'restore':\n"
        "    src = sqlite3.connect(source)\n"
        "    dst = sqlite3.connect(destination)\n"
        "    with dst:\n"
        "        src.backup(dst)\n"
        "    src.close(); dst.close()\n"
        "else:\n"
        "    raise SystemExit('unknown sqlite action')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def backup_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a SQLite database through an external sqlite process."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    if shutil.which("sqlite3"):
        completed = _run_sqlite([str(source), f".backup {destination}"])
    else:
        completed = _run_sqlite([str(source), str(destination), "backup"])
    if completed.returncode != 0 or not destination.is_file():
        detail = (completed.stderr or completed.stdout or "sqlite backup failed").strip()
        raise RuntimeError(detail)


def restore_sqlite_database(backup: Path, destination: Path) -> None:
    """Restore a backup into an isolated database file."""
    backup = Path(backup)
    destination = Path(destination)
    if not backup.is_file():
        raise FileNotFoundError(f"Backup does not exist: {backup}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    if shutil.which("sqlite3"):
        completed = _run_sqlite([str(destination), f".restore {backup}"])
    else:
        completed = _run_sqlite([str(backup), str(destination), "restore"])
    if completed.returncode != 0 or not destination.is_file():
        detail = (completed.stderr or completed.stdout or "sqlite restore failed").strip()
        raise RuntimeError(detail)


def backup_directory(source: Path, destination: Path) -> None:
    """Copy a local artifact directory aside with cp -a."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise FileNotFoundError(f"Directory does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cp", "-a", str(source), str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not destination.is_dir():
        detail = (completed.stderr or completed.stdout or "cp backup failed").strip()
        raise RuntimeError(detail)


def restore_directory(backup: Path, destination: Path) -> None:
    """Copy a directory backup back over an isolated destination."""
    backup = Path(backup)
    destination = Path(destination)
    if not backup.is_dir():
        raise FileNotFoundError(f"Directory backup does not exist: {backup}")
    destination.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cp", "-a", f"{backup}/.", str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "cp restore failed").strip()
        raise RuntimeError(detail)
