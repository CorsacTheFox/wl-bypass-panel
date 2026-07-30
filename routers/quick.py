"""Quick-launch routes: unauthenticated one-click instance creation.

GET  /api/quick/link  — NON-INTERACTIVE: starts an instance and returns the
                        ready output_link as plain text once it appears
                        (waits up to ~30s server-side). Intended for bots and
                        external integrations: a single GET, no JSON, no page.
POST /api/quick/start  — creates a 15-min instance, returns JSON (instance row).
GET  /api/quick/status/{id} — polls instance status & output_link.

Fully public (no auth, no token). The service to launch and the global cap on
simultaneous quick sessions are configured at runtime from the Admin panel
(stored in the `settings` table), with env/config fallbacks.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from config import BASE_DIR, QUICK_TOKEN
from db import db
from services import (
    ConcurrencyLimitError,
    NotFoundError,
    instance_service,
    service_registry,
    settings_store,
)

async def _require_quick_token(request: Request) -> None:
    """Guard the public quick flow with an API key when one is configured.

    When ``WB_QUICK_TOKEN`` is unset (the default) the quick flow stays open —
    convenient for local/dev use. When it IS set, callers must present it via
    the ``X-Api-Key`` header or a ``?token=`` query param. This stops anonymous
    drive-by abuse of the unauthenticated client link on a deployed server.
    """
    if not QUICK_TOKEN:
        return
    presented = request.headers.get("x-api-key") or request.query_params.get("token") or ""
    if presented != QUICK_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


router = APIRouter(
    prefix="/api/quick",
    tags=["quick"],
    dependencies=[Depends(_require_quick_token)],
)

QUICK_HTML = BASE_DIR / "quick.html"  # noqa: F841 — referenced only for documentation
_QUICK_TIMEOUT = 900  # 15 minutes in seconds


async def _quick_active_count() -> int:
    row = await db.fetchone(
        "SELECT COUNT(*) c FROM instances "
        "WHERE is_quick = 1 AND status IN ('pending','running','stopping')"
    )
    return row["c"] if row else 0


async def _resolve_quick_service():
    """Resolve the service the quick flow should launch.

    Preference: the service_id configured in Admin settings; if unset (0) or
    if that service no longer exists / is disabled, fall back to the first
    enabled service.
    """
    configured_id = await settings_store.get("quick_service_id")
    if configured_id:
        try:
            svc = await service_registry.get(configured_id)
            if svc["enabled"]:
                return svc
        except NotFoundError:
            pass  # configured service vanished — fall back
    services = await service_registry.list()
    return next((s for s in services if s["enabled"]), None)


_LINK_WAIT_SECONDS = 30   # how long /link blocks waiting for the output_link
_LINK_POLL_INTERVAL = 0.5


async def _wait_for_output_link(instance_id: int, timeout: float) -> str | None:
    """Block (polling the DB) until output_link appears or instance dies."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await db.fetchone(
            "SELECT status, output_link FROM instances WHERE id=?", (instance_id,)
        )
        if row is None:
            return None
        if row["output_link"]:
            return row["output_link"]
        if row["status"] in ("stopped", "exited", "crashed", "timeout"):
            return None  # instance ended without a link
        await asyncio.sleep(_LINK_POLL_INTERVAL)
    return None


@router.get("/link", response_class=PlainTextResponse)
async def quick_link():
    """NON-INTERACTIVE quick launch: returns the ready link as plain text.

    Starts a 15-minute instance for the configured service, waits (server-side)
    up to ~30s for the binary to print its join link, and returns that link as
    ``text/plain``. Designed for bots / external callers that just want the link
    in a single GET. Returns:
      200 + link            on success,
      404                   if no enabled service,
      429                   if the quick-session cap is reached,
      504                   if the link did not appear in time.
    """
    return PlainTextResponse(await produce_link())


async def produce_link() -> str:
    """Shared non-interactive quick-launch logic (used by /quick and /api/quick/link).

    Starts an instance for the configured service, blocks until the join link
    appears (or the instance dies), and returns the link string. Raises
    HTTPException on any failure.
    """
    limit = await settings_store.get("quick_max_concurrent")
    if await _quick_active_count() >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"quick-launch limit reached ({limit} active)",
        )

    svc = await _resolve_quick_service()
    if svc is None:
        raise HTTPException(status_code=404, detail="no enabled services available")

    try:
        instance = await instance_service.start(
            user_id=1,
            service_id=svc["id"],
            timeout_seconds=_QUICK_TIMEOUT,
            is_quick=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    link = await _wait_for_output_link(instance["id"], _LINK_WAIT_SECONDS)
    if not link:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="instance started but produced no link within the timeout",
        )
    return link


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def quick_start():
    """Create a 15-minute instance using the configured service (public)."""
    # Global cap on simultaneous quick sessions.
    limit = await settings_store.get("quick_max_concurrent")
    if await _quick_active_count() >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"quick-launch limit reached ({limit} active)",
        )

    svc = await _resolve_quick_service()
    if svc is None:
        raise HTTPException(status_code=404, detail="no enabled services available")

    try:
        return await instance_service.start(
            user_id=1,  # quick-launch always uses user 1 (admin)
            service_id=svc["id"],
            timeout_seconds=_QUICK_TIMEOUT,
            is_quick=True,
        )
    except ConcurrencyLimitError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/status/{instance_id}")
async def quick_status(instance_id: int):
    """Poll instance status and output_link."""
    row = await db.fetchone(
        """SELECT i.id, i.status, i.output_link, i.error
           FROM instances i WHERE i.id=?""",
        (instance_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return dict(row)
