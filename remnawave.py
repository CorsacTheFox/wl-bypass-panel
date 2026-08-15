"""Remnawave integration — import users from panel squads into this panel.

Read-only against the Remnawave panel (we never modify users there). Three
pieces live here:

  * :class:`RemnawaveClient`    — thin httpx wrapper for the panel's /api
  * config helpers              — panel URL / API key / auto-sync settings
                                  (settings table + env fallbacks)
  * :class:`RemnawaveMigrationService` — fetch squad users, classify them
                                  against local accounts, create the missing
                                  ones through ``user_service.create_client``
                                  (the extensibility seam: ``external_ref``
                                  stores the Remnawave UUID, so repeated runs
                                  are idempotent).
  * :class:`RemnawaveSyncService` — optional background loop that re-runs the
                                  import on an interval (Admin → Remnawave tab).

API shapes (Remnawave API spec v3.2.x):
    GET /api/system/metadata     -> {response: {version, ...}}
    GET /api/internal-squads     -> {response: {total, squads: [{uuid, name}]}}
    GET /api/users?start&size&filters=<json>
      filters = [{"id": "activeInternalSquads", "value": "<squad-uuid>"}]
      -> {response: {users: [...], total}}
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from config import (
    DEFAULT_MAX_CONCURRENT,
    REMNAWAVE_API_KEY,
    REMNAWAVE_PANEL_URL,
    REMNAWAVE_SYNC_INTERVAL_MIN,
    REMNAWAVE_TIMEOUT_SECONDS,
)
from db import db
from services import user_service

log = logging.getLogger("remnawave")

# Safety cap: refuse to fetch more than this many users per squad in one run
# (guards against a runaway pagination loop on a misbehaving panel).
MAX_USERS_PER_SQUAD = 10_000
# Page size for GET /api/users (panel hard limit is 1000; 100 keeps responses small).
USERS_PAGE_SIZE = 100
# How many preview rows to return to the UI (counts always cover everything).
PREVIEW_LIMIT = 200
# Auto-sync: delay after startup before the first background run.
SYNC_INITIAL_DELAY_SECONDS = 15.0


class RemnawaveError(Exception):
    """Any failure talking to the Remnawave panel (network, auth, bad payload)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
class RemnawaveClient:
    """Minimal async client for the Remnawave panel's admin API.

    One instance per call context (cheap); httpx clients are created lazily
    and closed via the async context manager.
    """

    def __init__(self, panel_url: str, api_key: str):
        if not panel_url or not api_key:
            raise RemnawaveError("Remnawave panel URL and API key are not configured")
        self._base = self._normalize_base_url(panel_url)
        self._headers = {
            "Authorization": api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}",
            "Accept": "application/json",
        }

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        # Accept "https://panel.example.com", ".../api", trailing slashes — we
        # always talk to "<base>/api/...".
        base = url.strip().rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        if not base:
            raise RemnawaveError("invalid Remnawave panel URL")
        return base + "/api"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                headers=self._headers,
                timeout=REMNAWAVE_TIMEOUT_SECONDS,
                follow_redirects=True,
            ) as client:
                resp = await client.get(path, params=params)
        except httpx.TimeoutException as e:
            raise RemnawaveError(f"timeout talking to Remnawave panel ({REMNAWAVE_TIMEOUT_SECONDS:g}s)") from e
        except httpx.HTTPError as e:
            raise RemnawaveError(f"cannot reach Remnawave panel: {e}") from e

        if resp.status_code in (401, 403):
            raise RemnawaveError("Remnawave panel rejected the API key (401/403)", resp.status_code)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("message") or body.get("detail") or body)
            except Exception:
                detail = resp.text[:300]
            raise RemnawaveError(f"Remnawave panel returned {resp.status_code}: {detail}", resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise RemnawaveError("Remnawave panel returned a non-JSON response (is this the panel URL?)") from e

    # -- endpoints ---------------------------------------------------------- #
    async def get_metadata(self) -> dict:
        data = await self._get("/system/metadata")
        return data.get("response") or {}

    async def get_internal_squads(self) -> list[dict]:
        data = await self._get("/internal-squads")
        response = data.get("response") or {}
        return response.get("squads") or []

    async def iter_users(self, squad_uuid: str):
        """Yield every user of a squad, paginating through GET /api/users."""
        start = 0
        served = 0
        while True:
            # The panel expects TanStack-table style filters as a JSON string.
            filters = json.dumps([{"id": "activeInternalSquads", "value": squad_uuid}])
            data = await self._get(
                "/users",
                params={"start": start, "size": USERS_PAGE_SIZE, "filters": filters},
            )
            response = data.get("response") or {}
            users = response.get("users") or []
            total = int(response.get("total") or 0)
            if not users:
                return
            for u in users:
                served += 1
                if served > MAX_USERS_PER_SQUAD:
                    raise RemnawaveError(
                        f"squad has more than {MAX_USERS_PER_SQUAD} users — refusing to import"
                    )
                yield u
            if total and served >= total:
                return
            if len(users) < USERS_PAGE_SIZE:  # defensive: panel lied about total
                return
            start += len(users)


# --------------------------------------------------------------------------- #
# Runtime configuration (settings table + env fallbacks)
# --------------------------------------------------------------------------- #
# The API key is a secret: it is stored in the local settings table (like the
# services' credentials) but never returned in clear by the admin API.
CONF_PANEL_URL = "remnawave_panel_url"
CONF_API_KEY = "remnawave_api_key"
CONF_AUTO_SYNC_ENABLED = "remnawave_auto_sync_enabled"
CONF_AUTO_SYNC_INTERVAL = "remnawave_auto_sync_interval_min"
CONF_AUTO_SYNC_SQUADS = "remnawave_auto_sync_squads"      # JSON array of uuids
CONF_SYNC_ONLY_ACTIVE = "remnawave_sync_only_active"
CONF_SYNC_GRANT_CREATE = "remnawave_sync_grant_create"
CONF_SYNC_MAX_CONCURRENT = "remnawave_sync_max_concurrent"
CONF_LAST_SYNC = "remnawave_last_sync"                    # JSON run summary


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:5]}{'*' * 6}{key[-4:]}"


async def _get_setting(key: str) -> str | None:
    row = await db.fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


async def _set_setting(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


async def get_panel_url() -> str:
    return (await _get_setting(CONF_PANEL_URL)) or REMNAWAVE_PANEL_URL


async def get_api_key() -> str:
    return (await _get_setting(CONF_API_KEY)) or REMNAWAVE_API_KEY


async def is_configured() -> bool:
    return bool(await get_panel_url() and await get_api_key())


async def get_client() -> RemnawaveClient:
    return RemnawaveClient(await get_panel_url(), await get_api_key())


async def get_sync_options() -> dict:
    """The persisted auto-sync options (with sensible defaults)."""
    squads_raw = await _get_setting(CONF_AUTO_SYNC_SQUADS)
    try:
        squads = json.loads(squads_raw) if squads_raw else []
    except ValueError:
        squads = []
    return {
        "enabled": (await _get_setting(CONF_AUTO_SYNC_ENABLED)) == "1",
        "interval_min": int((await _get_setting(CONF_AUTO_SYNC_INTERVAL)) or REMNAWAVE_SYNC_INTERVAL_MIN),
        "squads": [s for s in squads if isinstance(s, str)],
        "only_active": (await _get_setting(CONF_SYNC_ONLY_ACTIVE) or "1") == "1",
        "grant_create_instances": (await _get_setting(CONF_SYNC_GRANT_CREATE)) == "1",
        "max_concurrent": int((await _get_setting(CONF_SYNC_MAX_CONCURRENT)) or DEFAULT_MAX_CONCURRENT),
    }


async def get_last_sync() -> dict | None:
    raw = await _get_setting(CONF_LAST_SYNC)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _record_last_sync(summary: dict, trigger: str) -> None:
    summary = dict(summary)
    summary["trigger"] = trigger
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    await _set_setting(CONF_LAST_SYNC, json.dumps(summary))


# --------------------------------------------------------------------------- #
# Migration service
# --------------------------------------------------------------------------- #
class RemnawaveMigrationService:
    """Fetches users from selected Remnawave squads and creates local clients.

    Classification against local accounts:
      * new        — no local user with this Remnawave UUID or username
      * already    — local user exists with the same ``external_ref``
      * conflict   — username taken by a locally-managed account (skipped;
                     the admin resolves these by hand)
    """

    async def list_squads(self) -> list[dict]:
        client = await get_client()
        squads = await client.get_internal_squads()
        return [{"uuid": s.get("uuid", ""), "name": s.get("name", "")} for s in squads]

    async def test_connection(self) -> dict:
        client = await get_client()
        meta = await client.get_metadata()
        return {
            "version": meta.get("version") or meta.get("appVersion") or "unknown",
            "metadata": meta,
        }

    async def fetch_users(self, squad_uuids: list[str]) -> list[dict]:
        """All users of the given squads, deduped by uuid (first squad wins)."""
        client = await get_client()
        seen: dict[str, dict] = {}
        for squad_uuid in squad_uuids:
            async for u in client.iter_users(squad_uuid):
                uuid = str(u.get("uuid") or "")
                if not uuid or uuid in seen:
                    continue
                entry = {
                    "uuid": uuid,
                    "username": str(u.get("username") or ""),
                    "status": u.get("status") or "",
                    "expire_at": u.get("expireAt"),
                    "telegram_id": u.get("telegramId"),
                    "squad": squad_uuid,
                }
                seen[uuid] = entry
        return list(seen.values())

    async def _classify(self, users: list[dict], only_active: bool) -> tuple[list[dict], dict]:
        """Split fetched users into (importable, counts)."""
        counts = {"total": len(users), "new": 0, "already": 0, "conflict": 0, "inactive": 0}
        importable: list[dict] = []
        for u in users:
            if only_active and u["status"] and u["status"].upper() != "ACTIVE":
                counts["inactive"] += 1
                u["category"] = "inactive"
                continue
            by_ref = await db.fetchone(
                "SELECT id FROM users WHERE external_ref=?", (u["uuid"],)
            )
            if by_ref:
                counts["already"] += 1
                u["category"] = "already"
                continue
            by_name = await db.fetchone(
                "SELECT id FROM users WHERE username=?", (u["username"],)
            )
            if by_name:
                counts["conflict"] += 1
                u["category"] = "conflict"
                continue
            counts["new"] += 1
            u["category"] = "new"
            importable.append(u)
        return importable, counts

    async def preview(self, squad_uuids: list[str], only_active: bool = True) -> dict:
        users = await self.fetch_users(squad_uuids)
        _, counts = await self._classify(users, only_active)
        return {
            "counts": counts,
            "users": users[:PREVIEW_LIMIT],
            "truncated": len(users) > PREVIEW_LIMIT,
        }

    async def migrate(
        self,
        squad_uuids: list[str],
        only_active: bool = True,
        grant_create_instances: bool = False,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> dict:
        users = await self.fetch_users(squad_uuids)
        importable, counts = await self._classify(users, only_active)

        errors: list[dict] = []
        created = 0
        for u in importable:
            try:
                await user_service.create_client(
                    username=u["username"],
                    password=None,  # must set one on first login (bulk-create pattern)
                    max_concurrent=max_concurrent,
                    external_ref=u["uuid"],
                    telegram_id=u["telegram_id"],
                    can_create_instances=grant_create_instances,
                )
                created += 1
            except ValueError as e:
                # e.g. telegram_id already linked to another local account
                errors.append({"username": u["username"], "error": str(e)})

        report = {
            "counts": counts,
            "created": created,
            "errors": errors,
            # squash usernames for compactness in the UI
            "conflict_usernames": [u["username"] for u in users if u["category"] == "conflict"],
        }
        return report


remnawave_service = RemnawaveMigrationService()


# --------------------------------------------------------------------------- #
# Background auto-sync
# --------------------------------------------------------------------------- #
class RemnawaveSyncService:
    """Periodically re-imports users from the configured squads.

    The loop re-reads settings every cycle, so admins can enable/disable it or
    change the interval from the UI without a restart. Runs the exact same
    idempotent import as the manual button (already-migrated users are skipped
    by ``external_ref``).
    """

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="remnawave-autosync")
            log.info("Remnawave auto-sync service started")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort stop
                pass
        self._task = None
        log.info("Remnawave auto-sync service stopped")

    async def _run(self) -> None:
        await asyncio.sleep(SYNC_INITIAL_DELAY_SECONDS)
        while not self._stop.is_set():
            opts = await get_sync_options()
            if opts["enabled"] and opts["squads"] and await is_configured():
                try:
                    report = await remnawave_service.migrate(
                        squad_uuids=opts["squads"],
                        only_active=opts["only_active"],
                        grant_create_instances=opts["grant_create_instances"],
                        max_concurrent=opts["max_concurrent"],
                    )
                    await _record_last_sync(report, trigger="auto")
                    log.info(
                        "Remnawave auto-sync: created=%s already=%s conflicts=%s errors=%s",
                        report["created"], report["counts"]["already"],
                        report["counts"]["conflict"], len(report["errors"]),
                    )
                except RemnawaveError as e:
                    await _record_last_sync({"error": str(e)}, trigger="auto")
                    log.warning("Remnawave auto-sync failed: %s", e)
                except Exception:  # noqa: BLE001 — the loop must survive anything
                    log.exception("Remnawave auto-sync crashed (will retry next cycle)")
                    await _record_last_sync({"error": "unexpected error — see server logs"}, trigger="auto")
                # Re-read options after the run so an interval change made
                # during the import applies immediately.
                opts = await get_sync_options()
            # Sleep in small slices so shutdown stays responsive and interval
            # changes still take effect without waiting a full cycle.
            interval_s = max(30.0, opts["interval_min"] * 60)
            waited = 0.0
            while waited < interval_s and not self._stop.is_set():
                step = min(5.0, interval_s - waited)
                await asyncio.sleep(step)
                waited += step
                fresh = await get_sync_options()
                interval_s = max(30.0, fresh["interval_min"] * 60)


remnawave_sync = RemnawaveSyncService()
