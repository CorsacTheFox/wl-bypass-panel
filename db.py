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
    password_must_change INTEGER NOT NULL DEFAULT 0,
    -- Telegram WebApp linkage; NULL means the account is not linked to Telegram.
    telegram_id          INTEGER,
    -- Instance-creation privilege. Defaults to 0 (OFF): new users — including
    -- those auto-created via Telegram — cannot launch instances until an admin
    -- grants it. Existing users are backfilled to 1 once on upgrade (see the
    -- migration in Database.connect) so no current client loses a capability.
    can_create_instances INTEGER NOT NULL DEFAULT 0
);
-- NOTE: the unique index on telegram_id is created in Database.connect()
-- (after the migration that adds the column), NOT here, because executescript
-- runs the whole SCHEMA_SQL as one batch before any ALTER TABLE — so on an
-- upgrade the index would reference a column that does not yet exist.

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
    proxychains_type  TEXT NOT NULL DEFAULT '',
    proxychains_host  TEXT NOT NULL DEFAULT '',
    proxychains_port  TEXT NOT NULL DEFAULT '',
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
    is_quick     INTEGER NOT NULL DEFAULT 0,    -- 1 if created via the public /quick flow
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

-- Runtime settings (key/value). Overrides config defaults; managed from the
-- Admin panel. Rows are simple "key = TEXT value" pairs.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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
        if "is_quick" not in columns:
            await self._conn.execute("ALTER TABLE instances ADD COLUMN is_quick INTEGER NOT NULL DEFAULT 0")
        # Migration: add password_must_change column if missing.
        rows = await self._conn.execute_fetchall("PRAGMA table_info(users)")
        user_columns = [row[1] for row in rows]
        if "password_must_change" not in user_columns:
            await self._conn.execute("ALTER TABLE users ADD COLUMN password_must_change INTEGER NOT NULL DEFAULT 0")

        # Migration: add Telegram linkage + instance-creation privilege.
        # telegram_id is nullable; can_create_instances defaults to 0 so all
        # newly created users (incl. auto-created via Telegram) start OFF.
        # When can_create_instances is first added, we backfill every existing
        # user to 1 (inside this guard) so the upgrade preserves current
        # behavior — no existing client loses the ability to launch instances.
        if "telegram_id" not in user_columns:
            await self._conn.execute("ALTER TABLE users ADD COLUMN telegram_id INTEGER")
        if "can_create_instances" not in user_columns:
            await self._conn.execute(
                "ALTER TABLE users ADD COLUMN can_create_instances INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.execute("UPDATE users SET can_create_instances = 1")
            log.warning("Backfilled can_create_instances=1 for existing users (one-time)")
        # Partial unique index on telegram_id (re-runnable). Created here (and
        # in SCHEMA_SQL for fresh installs) so upgrades get it too.
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_id "
            "ON users(telegram_id) WHERE telegram_id IS NOT NULL"
        )

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

        # Migration: add proxychains columns to services if missing.
        for col in ("proxychains_type", "proxychains_host", "proxychains_port"):
            rows = await self._conn.execute_fetchall("PRAGMA table_info(services)")
            svc_cols = [row[1] for row in rows]
            if col not in svc_cols:
                await self._conn.execute(
                    f"ALTER TABLE services ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
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
