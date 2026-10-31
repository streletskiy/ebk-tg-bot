"""One-way, transactional import of a stopped legacy SQLite queue."""

import hashlib
import json
import sqlite3
from pathlib import Path

from psycopg import sql

from .store import Store

TABLE_KEYS = {
    "meta": ("key",), "jobs": ("id",), "writes": ("marker",),
    "conversations": ("user_id",), "job_feedback": ("job_id", "part"),
}


def rows_digest(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def migrate_sqlite(settings, filename):
    path = Path(filename).resolve(strict=True)
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = Store(settings)
    try:
        target.acquire_worker_lock()
        source.execute("BEGIN")
        available = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "jobs", "writes", "conversations"} <= available:
            raise ValueError("Legacy database is missing required tables")
        tables = {}
        fingerprint = hashlib.sha256()
        for table, keys in TABLE_KEYS.items():
            if table not in available:
                continue
            columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
            supported = {r["column_name"] for r in target.db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s", (table,))}
            if not set(columns) <= supported:
                raise ValueError("Unsupported legacy schema")
            rows = [dict(r) for r in source.execute(f'SELECT * FROM "{table}" ORDER BY ' + ",".join(keys))]
            digest = rows_digest(rows)
            tables[table] = (columns, rows, digest)
            fingerprint.update((table + ":" + digest).encode())
        fingerprint = fingerprint.hexdigest()
        with target.transaction():
            imported = target.meta("_sqlite_import_digest", "")
            if imported == fingerprint:
                return {"status": "already_imported", "sha256": fingerprint}
            if imported:
                raise ValueError("A different legacy database was already imported")
            for table in (*TABLE_KEYS, "memories"):
                if target.db.execute(sql.SQL("SELECT EXISTS(SELECT 1 FROM {}) AS present").format(sql.Identifier(table))).fetchone()["present"]:
                    raise ValueError("PostgreSQL target must be empty; no data was overwritten")
            for table, (columns, rows, digest) in tables.items():
                if rows:
                    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns))
                    with target.db.cursor() as cursor:
                        cursor.executemany(statement, [tuple(row[c] for c in columns) for row in rows])
                copied = target.db.execute(sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                    sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, TABLE_KEYS[table])))).fetchall()
                if rows_digest(copied) != digest:
                    raise RuntimeError("Migration verification failed; transaction rolled back")
            target.set_meta("_sqlite_import_digest", fingerprint)
        return {"status": "imported", "rows": {t: len(v[1]) for t, v in tables.items()}, "sha256": fingerprint}
    finally:
        source.close()
        target.close()
