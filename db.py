"""SQLite database access via aiosqlite.

Single shared connection pool wrapper. All queries go through `Database`,
which keeps the connection simple and lets the app layer avoid passing a
`db` object around (it imports the `db` singleton).
"""
from __future__ import annotations

import logging

import aiosqlite

log = logging.getLogger("db")

from config import DATABASE_PATH

SCHEMA_SQL = """
-- Users (clients + the bootstrap admin)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'client' CHECK (role IN ('admin', 'client')),
    -- per-user concurrency cap (defaults to policy in config)
    max_concurrent INTEGER NOT NULL DEFAULT 3,
    -- opaque handle for a future Remnawave-style webhook source; NULL = local
    external_ref  TEXT UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    password_must_change INTEGER NOT NULL DEFAULT 0
);

-- Pre-configured services the binary can connect to (Service A, Service B, ...)
CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    -- path to the compiled binary that handles this service
    binary_path TEXT NOT NULL,
    -- server-side cookies / session tokens passed to the binary.
    -- Stored as opaque text (could be a JSON blob, raw cookie string, ...).
    credentials TEXT NOT NULL,
    -- extra static args appended after credentials (e.g. --resources ...)
    extra_args  TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Active/recorded binary instances (one row per spawned process lifecycle)
CREATE TABLE IF NOT EXISTS instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    service_id   INTEGER NOT NULL,
    pid          INTEGER,                       -- OS pid of the spawned binary
    status       TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','stopping','stopped','exited','crashed','timeout')),
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at     TEXT,                          -- set when terminal
    exit_code    INTEGER,
    timeout_at   TEXT,                          -- absolute datetime the instance should be killed
    error        TEXT,
    output_link  TEXT,                          -- join_link extracted from binary stdout (e.g. wbstream://...)
    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_instances_user_status ON instances(user_id, status);
CREATE INDEX IF NOT EXISTS idx_instances_pid         ON instances(pid);

-- Session tokens (opaque bearer tokens for the SPA)
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# Safe migration: add output_link column to an existing instances table that
# was created before this column existed. SQLite supports ALTER TABLE ADD
# COLUMN if the column is not already present; we guard with a pragma check.
_MIGRATION_ADD_OUTPUT_LINK = """
PRAGMA table_info(instances);
"""


class Database:
    """Thin async wrapper around a single aiosqlite connection."""


class Database:
    """Thin async wrapper around a single aiosqlite connection."""

    def __init__(self, path):
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        # autocommit off; we use explicit transactions per logical op.
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(SCHEMA_SQL)
        # Migration: add output_link column if missing (safe to re-run).
        rows = await self._conn.execute_fetchall("PRAGMA table_info(instances)")
        columns = [row[1] for row in rows]
        if "output_link" not in columns:
            await self._conn.execute("ALTER TABLE instances ADD COLUMN output_link TEXT")
        # Migration: add password_must_change column if missing.
        rows = await self._conn.execute_fetchall("PRAGMA table_info(users)")
        user_columns = [row[1] for row in rows]
        if "password_must_change" not in user_columns:
            await self._conn.execute("ALTER TABLE users ADD COLUMN password_must_change INTEGER NOT NULL DEFAULT 0")

        # Migration: change instances.service_id FK from RESTRICT to CASCADE.
        # SQLite cannot ALTER constraints, so we check the sql schema and
        # recreate the table if it still has RESTRICT.
        fk_rows = await self._conn.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='instances'"
        )
        fk_sql = fk_rows[0]["sql"] if fk_rows else ""
        if fk_sql and "ON DELETE RESTRICT" in fk_sql:
            log.warning("Migrating instances FK from RESTRICT to CASCADE ...")
            # Disable FK enforcement for the DDL operations.
            await self._conn.execute("PRAGMA foreign_keys=OFF")
            # Rename old table out of the way.
            await self._conn.execute("ALTER TABLE instances RENAME TO _instances_old")
            # Recreate instances with the correct CASCADE constraint.
            await self._conn.execute(
                """CREATE TABLE instances (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    service_id   INTEGER NOT NULL,
                    pid          INTEGER,
                    status       TEXT NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending','running','stopping','stopped','exited','crashed','timeout')),
                    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
                    ended_at     TEXT,
                    exit_code    INTEGER,
                    timeout_at   TEXT,
                    error        TEXT,
                    output_link  TEXT,
                    FOREIGN KEY (user_id)    REFERENCES users(id)    ON DELETE CASCADE,
                    FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
                )"""
            )
            # Copy data back.
            await self._conn.execute(
                "INSERT INTO instances "
                "(id,user_id,service_id,pid,status,started_at,ended_at,exit_code,"
                "timeout_at,error,output_link) "
                "SELECT id,user_id,service_id,pid,status,started_at,ended_at,exit_code,"
                "timeout_at,error,output_link "
                "FROM _instances_old"
            )
            # Drop the old table.
            await self._conn.execute("DROP TABLE _instances_old")
            # Re-enable FK enforcement.
            await self._conn.execute("PRAGMA foreign_keys=ON")
            log.warning("FK migration complete (table recreated with CASCADE).")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._conn is not None, "Database not connected"
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        assert self._conn is not None, "Database not connected"
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None, "Database not connected"
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows


# Process-wide singleton. The FastAPI app initializes/closes it via lifespan.
db = Database(DATABASE_PATH)
