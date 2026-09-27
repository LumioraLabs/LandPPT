# Backup and restore

Last updated: 2026-09-26 (America/Toronto)

This rehearsal is a local SQLite drill. It is not a production PostgreSQL or object-storage drill. The production backup destination, encryption keys, and restore owner remain external launch dependencies.

## Database

Production PostgreSQL:

```bash
pg_dump --format=custom --file "landppt-$(date -u +%Y%m%dT%H%M%SZ).dump" "$DATABASE_URL"
pg_restore --clean --if-exists --dbname "$RESTORE_DATABASE_URL" landppt-YYYYMMDDTHHMMSSZ.dump
```

Run the restore against an empty database that is not the live primary. Compare a known row, then discard the isolated copy.

Local SQLite rehearsal, implemented by `landppt.database.backup`:

```bash
sqlite3 /path/to/landppt.db ".backup /path/to/landppt-backup.db"
sqlite3 /path/to/isolated-restore.db ".restore /path/to/landppt-backup.db"
```

`backup_sqlite_database` and `restore_sqlite_database` shell out to `sqlite3`. The automated test writes a row, backs up, deletes the source row, restores into a new file, and reads the row back.

## Object storage

For the in-chart MinIO bucket, copy the bucket to a second prefix or bucket before a restore:

```bash
mc mirror --overwrite local/landppt local/landppt-backup
```

A local artifact directory is copied by `backup_directory` and `restore_directory`, which shell out to `cp -a`. The automated test writes a file, copies the directory, deletes the live file, restores into an isolated directory, and reads the file back. Do not point that copy at the production bucket until a backup destination is chosen.

## What a restore does not repair

Payment provider state stays at Stripe. Restoring an old database can reintroduce a balance that a later refund already reversed. After a database restore, run `landppt-cli reconcile-payments` against the stored webhook receipts before serving traffic.

## Retention

Completed webhook receipts older than 90 days may be purged. Failed receipts and payment orders stay so a paid purchase can still be repaired. Credit ledger rows and payment orders are kept when a customer deletes an account.
