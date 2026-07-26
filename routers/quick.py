"""Quick-launch routes: unauthenticated one-click instance creation.

POST /api/quick/start   — creates a 15-min instance for the configured service
GET  /api/quick/status/{id} — polls instance status & output_link

Fully public (no auth, no token). The service to launch and the global cap on
simultaneous quick sessions are configured at runtime from the Admin panel
(stored in the `settings` table), with env/config fallbacks.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from config import BASE_DIR
from db import db
from services import (
    ConcurrencyLimitError,
    NotFoundError,
    instance_service,
    service_registry,
    settings_store,
)

router = APIRouter(prefix="/api/quick", tags=["quick"])

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
