"""Local backup and restore shell out to sqlite3."""

import sqlite3

import pytest

from landppt.database.backup import backup_sqlite_database, restore_sqlite_database


def test_backup_and_restore_round_trip_a_written_row(tmp_path):
    source = tmp_path / "source.db"
    backup = tmp_path / "backups" / "source.backup"
    restored = tmp_path / "restored.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO kept (note) VALUES ('row-before-backup')")
        connection.commit()

    backup_sqlite_database(source, backup)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM kept")
        connection.commit()

    restore_sqlite_database(backup, restored)
    with sqlite3.connect(restored) as connection:
        note = connection.execute("SELECT note FROM kept").fetchone()[0]
    assert note == "row-before-backup"
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM kept").fetchone()[0] == 0


def test_directory_backup_restores_a_deleted_artifact(tmp_path):
    from landppt.database.backup import backup_directory, restore_directory

    live = tmp_path / "artifacts"
    live.mkdir()
    (live / "slide.html").write_text("<p>kept artifact</p>", encoding="utf-8")
    backup = tmp_path / "artifact-backup"
    restored = tmp_path / "artifact-restore"
    backup_directory(live, backup)
    (live / "slide.html").unlink()
    restore_directory(backup, restored)
    assert (restored / "slide.html").read_text(encoding="utf-8") == "<p>kept artifact</p>"
