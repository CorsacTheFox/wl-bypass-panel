"""Service layer — business logic + the extensibility seam.

This module is intentionally the *only* place that creates/manages users
and instances. Routers are thin HTTP adapters; all rules live here.

Why a separate layer?
  The spec calls for an abstraction so a future Remnawave webhook/API
  listener can create clients automatically. By routing *all* user creation
  through :class:`UserService` (instead of doing SQL inline in the admin
  router), a webhook handler later just calls::

      await user_service.create_client(..., external_ref=remnawave_uuid)

  with zero changes to the admin UI or the rest of the app.
"""
from __future__ import annotations

import os
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    BINARIES_DIR,
    COOKIES_DIR,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_TIMEOUT_SECONDS,
    ensure_dirs,
)
from db import db
from process_manager import LIVE, ProcessError, S_RUNNING, process_manager
from security import hash_password, verify_password


class ConcurrencyLimitError(Exception):
    """User has reached their concurrent-instance cap."""


class NotFoundError(Exception):
    pass


class CookiesError(Exception):
    """Raised on a malformed cookies zip upload."""


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
class UserService:
    """All user lifecycle operations go through here (the Remnawave seam)."""

    async def create_client(
        self,
        username: str,
        password: str,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        external_ref: str | None = None,
    ) -> dict:
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("username and password are required")
        if max_concurrent < 0:
            raise ValueError("max_concurrent must be >= 0")
        pw_hash = hash_password(password)
        try:
            cur = await db.execute(
                """
                INSERT INTO users (username, password_hash, role, max_concurrent, external_ref)
                VALUES (?, ?, 'client', ?, ?)
                """,
                (username, pw_hash, max_concurrent, external_ref),
            )
        except Exception as e:  # unique violation etc.
            raise ValueError(f"could not create user: {e}") from e
        return await self.get(cur.lastrowid)

    async def create_admin(self, username: str, password: str) -> dict:
        pw_hash = hash_password(password)
        cur = await db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?, 'admin')",
            (username, pw_hash),
        )
        return await self.get(cur.lastrowid)

    async def ensure_bootstrap_admin(self, username: str, password: str) -> None:
        row = await db.fetchone("SELECT id FROM users WHERE role='admin' LIMIT 1")
        if row is None:
            await self.create_admin(username, password)

    async def get(self, user_id: int) -> dict:
        row = await db.fetchone(
            """SELECT id, username, role, max_concurrent, external_ref,
                      created_at, enabled FROM users WHERE id=?""",
            (user_id,),
        )
        if row is None:
            raise NotFoundError("user not found")
        return dict(row)

    async def list_clients(self) -> list[dict]:
        rows = await db.fetchall(
            """SELECT u.id, u.username, u.role, u.max_concurrent, u.external_ref,
                      u.created_at, u.enabled,
                      (SELECT COUNT(*) FROM instances i
                         WHERE i.user_id=u.id AND i.status IN ('pending','running','stopping')) AS active
               FROM users u ORDER BY u.id"""
        )
        return [dict(r) for r in rows]

    async def authenticate(self, username: str, password: str) -> dict | None:
        row = await db.fetchone(
            "SELECT id, username, password_hash, role, max_concurrent, enabled FROM users WHERE username=?",
            (username,),
        )
        if row is None or not row["enabled"]:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "max_concurrent": row["max_concurrent"],
        }

    async def update(self, user_id: int, *, password: str | None = None,
                     max_concurrent: int | None = None,
                     enabled: bool | None = None) -> dict:
        sets, params = [], []
        if password is not None:
            sets.append("password_hash=?"); params.append(hash_password(password))
        if max_concurrent is not None:
            sets.append("max_concurrent=?"); params.append(max_concurrent)
        if enabled is not None:
            sets.append("enabled=?"); params.append(1 if enabled else 0)
        if sets:
            params.append(user_id)
            await db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(params))
        return await self.get(user_id)

    async def delete(self, user_id: int) -> None:
        # Stopping their live instances first is the caller's responsibility.
        await db.execute("DELETE FROM users WHERE id=? AND role='client'", (user_id,))


user_service = UserService()


# --------------------------------------------------------------------------- #
# Services (the things the binary connects to)
# --------------------------------------------------------------------------- #
class ServiceRegistry:
    async def create(self, name: str, binary_path: str, credentials: str,
                     extra_args: str = "", enabled: bool = True) -> dict:
        cur = await db.execute(
            """INSERT INTO services (name, binary_path, credentials, extra_args, enabled)
               VALUES (?,?,?,?,?)""",
            (name, binary_path, credentials, extra_args, 1 if enabled else 0),
        )
        return await self.get(cur.lastrowid)

    async def get(self, service_id: int) -> dict:
        row = await db.fetchone("SELECT * FROM services WHERE id=?", (service_id,))
        if row is None:
            raise NotFoundError("service not found")
        return dict(row)

    async def list(self) -> list[dict]:
        rows = await db.fetchall(
            "SELECT id, name, binary_path, credentials, extra_args, enabled, created_at FROM services ORDER BY id"
        )
        return [dict(r) for r in rows]

    async def update(self, service_id: int, **fields) -> dict:
        allowed = {"name", "binary_path", "credentials", "extra_args", "enabled"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "enabled":
                v = 1 if v else 0
            sets.append(f"{k}=?"); params.append(v)
        if sets:
            params.append(service_id)
            await db.execute(f"UPDATE services SET {', '.join(sets)} WHERE id=?", tuple(params))
        return await self.get(service_id)

    async def delete(self, service_id: int) -> None:
        await db.execute("DELETE FROM services WHERE id=?", (service_id,))


service_registry = ServiceRegistry()


# --------------------------------------------------------------------------- #
# Cookies files — zip upload of cookies-<binary>.json, listed/deleted by admin
# --------------------------------------------------------------------------- #
class CookiesStore:
    """Manages the on-disk pool of cookies-*.json files.

    Admin uploads a zip (e.g. cookies-dion.json, cookies-wbstream.json, ...).
    We extract every top-level ``cookies-*.json`` member into COOKIES_DIR and
    expose them as a dropdown when creating/editing a Service. The chosen
    absolute path is what gets stored in ``services.credentials`` and passed
    to the binary as ``-cookies <path>``.
    """

    def list(self) -> list[dict]:
        """All cookies-*.json currently on disk, newest first."""
        if not COOKIES_DIR.exists():
            return []
        files = sorted(
            COOKIES_DIR.glob("cookies-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for p in files:
            st = p.stat()
            out.append({
                "name": p.name,
                # absolute path is what services.credentials stores
                "path": str(p.resolve()),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S"),
            })
        return out

    def save_zip(self, data: bytes, replace: bool = True) -> list[str]:
        """Extract cookies-*.json members from an in-memory zip.

        Only top-level ``cookies-*.json`` files are accepted (no path
        traversal, no nested dirs, no other extensions). Returns the list of
        extracted filenames. Raises CookiesError on anything suspicious.
        """
        ensure_dirs()
        import io
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise CookiesError(f"not a valid zip file: {e}") from e

        members = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # flatten: take basename only
            if not name.startswith("cookies-") or not name.endswith(".json"):
                continue
            # Reject absolute / parent-traversal entries defensively, even
            # though we only ever write into COOKIES_DIR by basename.
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise CookiesError(f"unsafe path in zip: {info.filename}")
            members.append((name, info))

        if not members:
            raise CookiesError(
                "zip contained no 'cookies-*.json' files "
                "(expected names like cookies-dion.json)"
            )

        extracted: list[str] = []
        for name, info in members:
            target = COOKIES_DIR / name
            if target.exists() and not replace:
                continue
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            # cookies are a credential — keep them off group/world.
            target.chmod(0o600)
            extracted.append(name)
        return extracted

    def delete(self, name: str) -> None:
        """Remove a single cookies-<name>.json by filename.

        ``name`` is validated to be a bare filename under COOKIES_DIR to
        prevent traversal. Raises NotFoundError if absent, CookiesError if
        ``name`` looks unsafe.
        """
        # Only allow a plain basename like 'cookies-dion.json'.
        if "/" in name or "\\" in name or ".." in name:
            raise CookiesError("invalid cookies filename")
        if not name.startswith("cookies-") or not name.endswith(".json"):
            raise CookiesError("invalid cookies filename")
        target = (COOKIES_DIR / name).resolve()
        # Final guard: resolved path must live inside COOKIES_DIR.
        try:
            target.relative_to(COOKIES_DIR.resolve())
        except ValueError as e:
            raise CookiesError("invalid cookies filename") from e
        if not target.exists():
            raise NotFoundError("cookies file not found")
        target.unlink()


cookies_store = CookiesStore()


# --------------------------------------------------------------------------- #
# Binaries — zip upload of headless-*-creator/joiner executables
# --------------------------------------------------------------------------- #
class BinariesError(Exception):
    """Raised on a malformed binaries zip upload."""


class BinariesStore:
    """Manages the on-disk pool of headless-* binaries.

    Admin uploads a zip (e.g. headless-wbstream-creator, headless-dion-joiner,
    headless-vk-bot, ...). We extract every top-level regular file that matches
    the ``headless-*`` or ``whitelist-bypass*`` naming convention into
    BINARIES_DIR and expose them as a dropdown when creating/editing a Service.
    """

    # Only extract files matching these name prefixes (defensive — don't let
    # an uploaded zip sprinkle arbitrary files into BINARIES_DIR).
    _ALLOWED_PREFIXES = ("headless-", "whitelist-bypass", "wb-manager")

    def list(self) -> list[dict]:
        """All binaries currently on disk, newest first.

        Skips non-executable placeholders like .gitkeep.
        """
        if not BINARIES_DIR.exists():
            return []
        files = sorted(
            (p for p in BINARIES_DIR.iterdir()
             if p.is_file() and p.name != ".gitkeep"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out = []
        for p in files:
            st = p.stat()
            out.append({
                "name": p.name,
                "path": str(p.resolve()),
                "size": st.st_size,
                "executable": os.access(p, os.X_OK),
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                    .strftime("%Y-%m-%d %H:%M:%S"),
            })
        return out

    def save_zip(self, data: bytes, replace: bool = True) -> list[str]:
        """Extract allowed binary members from an in-memory zip.

        Only top-level files whose basename starts with one of the allowed
        prefixes are extracted. Directories are skipped. Returns the list of
        extracted filenames. Raises BinariesError on anything suspicious.
        """
        ensure_dirs()
        import io, os
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise BinariesError(f"not a valid zip file: {e}") from e

        members = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # flatten: basename only
            if not any(name.startswith(prefix) for prefix in self._ALLOWED_PREFIXES):
                continue
            # Reject absolute / parent-traversal entries.
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise BinariesError(f"unsafe path in zip: {info.filename}")
            members.append((name, info))

        if not members:
            raise BinariesError(
                "zip contained no matching binaries "
                f"(expected names starting with: {', '.join(self._ALLOWED_PREFIXES)})"
            )

        extracted: list[str] = []
        for name, info in members:
            target = BINARIES_DIR / name
            if target.exists() and not replace:
                continue
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            # Make executable.
            target.chmod(0o755)
            extracted.append(name)
        return extracted

    def delete(self, name: str) -> None:
        """Remove a single binary by filename."""
        if "/" in name or "\\" in name or ".." in name:
            raise BinariesError("invalid binary filename")
        if not any(name.startswith(prefix) for prefix in self._ALLOWED_PREFIXES):
            raise BinariesError("invalid binary filename")
        target = (BINARIES_DIR / name).resolve()
        try:
            target.relative_to(BINARIES_DIR.resolve())
        except ValueError as e:
            raise BinariesError("invalid binary filename") from e
        if not target.exists():
            raise NotFoundError("binary not found")
        target.unlink()


binaries_store = BinariesStore()


# --------------------------------------------------------------------------- #
# Instances — enforces the max-3 concurrency rule
# --------------------------------------------------------------------------- #
class InstanceService:
    async def _active_count(self, user_id: int) -> int:
        row = await db.fetchone(
            "SELECT COUNT(*) c FROM instances WHERE user_id=? AND status IN ('pending','running','stopping')",
            (user_id,),
        )
        return row["c"] if row else 0

    async def list_for_user(self, user_id: int, include_history: bool = False) -> list[dict]:
        sql = """
            SELECT i.id, i.user_id, i.service_id, s.name AS service_name,
                   i.pid, i.status, i.started_at, i.ended_at, i.exit_code, i.error,
                   i.output_link
              FROM instances i
              JOIN services s ON s.id = i.service_id
             WHERE i.user_id = ?
        """
        if not include_history:
            sql += " AND i.status IN ('pending','running','stopping')"
        sql += " ORDER BY i.id DESC"
        rows = await db.fetchall(sql, (user_id,))
        return [dict(r) for r in rows]

    async def utilization(self, user_id: int) -> dict:
        user = await user_service.get(user_id)
        active = await self._active_count(user_id)
        return {
            "active": active,
            "max": user["max_concurrent"],
            "remaining": max(0, user["max_concurrent"] - active),
        }

    async def start(self, user_id: int, service_id: int,
                    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
        # 1. concurrency check (the core business rule)
        user = await user_service.get(user_id)
        active = await self._active_count(user_id)
        if active >= user["max_concurrent"]:
            raise ConcurrencyLimitError(
                f"Concurrent limit reached ({active}/{user['max_concurrent']})"
            )

        # 2. resolve service config
        svc = await service_registry.get(service_id)
        if not svc["enabled"]:
            raise ValueError("selected service is disabled")

        # 3. create instance row (pending), then spawn
        timeout_at = (
            datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        ).isoformat()
        cur = await db.execute(
            """INSERT INTO instances (user_id, service_id, status, timeout_at)
               VALUES (?, ?, 'pending', ?)""",
            (user_id, service_id, timeout_at),
        )
        instance_id = cur.lastrowid

        try:
            pid = await process_manager.spawn(
                instance_id=instance_id,
                binary_path=svc["binary_path"],
                credentials=svc["credentials"],
                extra_args=svc["extra_args"] or "",
            )
        except ProcessError as e:
            # spawn failed; row already marked crashed inside process_manager
            row = await db.fetchone("SELECT * FROM instances WHERE id=?", (instance_id,))
            return dict(row)

        # 4. schedule timeout enforcement
        await process_manager.schedule_timeout(instance_id, float(timeout_seconds))
        return await self._get(instance_id)

    async def stop(self, user_id: int, instance_id: int) -> dict:
        row = await db.fetchone(
            "SELECT * FROM instances WHERE id=? AND user_id=?",
            (instance_id, user_id),
        )
        if row is None:
            raise NotFoundError("instance not found")
        if row["status"] in ("stopped", "exited", "crashed", "timeout"):
            return dict(row)  # already done
        await process_manager.stop(instance_id)
        return await self._get(instance_id)

    async def stop_all_for_user(self, user_id: int) -> int:
        rows = await db.fetchall(
            """SELECT id FROM instances WHERE user_id=? AND status IN ('pending','running','stopping')""",
            (user_id,),
        )
        for r in rows:
            await process_manager.stop(r["id"])
        return len(rows)

    async def _get(self, instance_id: int) -> dict:
        row = await db.fetchone(
            """SELECT i.id, i.user_id, i.service_id, s.name AS service_name,
                      i.pid, i.status, i.started_at, i.ended_at, i.exit_code, i.error,
                      i.output_link
                 FROM instances i JOIN services s ON s.id = i.service_id
                WHERE i.id=?""",
            (instance_id,),
        )
        return dict(row) if row else {}


instance_service = InstanceService()
