"""Native-app routes for Telegram-authorized clients.

These implement the "authorized client" link model from the Android app spec:

  GET /api/app/connect     — called by the app when the user connects. Starts
                             (or resumes) a long-lived instance attributed to
                             the authenticated user and returns the same
                             ``output_link`` for the whole connection.
  GET /api/app/disconnect  — called by the app when the user disconnects.
                             Stops that instance, freeing the server binary.

Both are GET (per the app contract) and idempotent: a repeated connect while a
live link exists returns the same link; a repeated disconnect is a no-op.
Contrast with the UNAUTHORIZED client, which simply hits the public
``GET /api/quick/link`` and gets a short, unattributed, shared temporary link.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status

from db import db
from security import require_client
from services import (
    ConcurrencyLimitError,
    ForbiddenError,
    NotFoundError,
    instance_service,
    service_registry,
    settings_store,
)

router = APIRouter(prefix="/api/app", tags=["app"], dependencies=[Depends(require_client)])

# How long /connect blocks waiting for the binary to print its join_link, and
# the poll cadence. Mirrors routers/quick.py so behavior is consistent.
_LINK_WAIT_SECONDS = 30
_LINK_POLL_INTERVAL = 0.5


async def _resolve_app_service(service_id: int | None):
    """Pick the service the authorized app should connect through.

    Preference: an explicit ``service_id`` from the query string; else the
    admin-configured quick service; else the first enabled service. Mirrors
    routers/quick._resolve_quick_service so the app and the public quick flow
    target the same default service unless told otherwise.
    """
    if service_id:
        try:
            svc = await service_registry.get(service_id)
            if svc["enabled"]:
                return svc
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="selected service is disabled")
        except NotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service not found")

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


async def _wait_for_output_link(instance_id: int, timeout: float) -> dict | None:
    """Poll the DB until the instance has an output_link or terminates.

    Returns the instance row (with status/output_link/timeout_at/error) when a
    link is ready, ``None`` if the instance died or the deadline elapsed first.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        row = await db.fetchone(
            "SELECT status, output_link, timeout_at, error FROM instances WHERE id=?",
            (instance_id,),
        )
        if row is None:
            return None
        if row["output_link"]:
            return dict(row)
        if row["status"] in ("stopped", "exited", "crashed", "timeout"):
            return dict(row)  # ended — caller reports the terminal status/error
        await asyncio.sleep(_LINK_POLL_INTERVAL)
    return None


@router.get("/connect")
async def connect(
    user=Depends(require_client),
    service_id: int | None = Query(default=None, ge=1),
):
    """Authorized connect: return the connection link for this session.

    If the user already has a live app-session instance with a ready link, the
    same link is returned (idempotent reconnect). Otherwise a long-lived
    instance is started under the authenticated user and this call blocks
    (server-side, up to ~30s) until the binary prints its ``join_link``.
    """
    svc = await _resolve_app_service(service_id)
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no enabled services available")

    timeout_seconds = await settings_store.get("app_default_timeout_seconds")
    try:
        instance = await instance_service.start_or_resume_active(
            user["id"], svc["id"], timeout_seconds
        )
    except ForbiddenError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(e))
    except ConcurrencyLimitError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e))
    except NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service not found")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))

    # If we resumed an already-ready link (reconnect) or the instance died
    # before spawning, short-circuit without the wait loop.
    if instance.get("output_link"):
        return _link_payload(instance)
    if instance.get("status") in ("stopped", "exited", "crashed", "timeout"):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=instance.get("error") or "instance ended without producing a link",
        )

    waited = await _wait_for_output_link(instance["id"], _LINK_WAIT_SECONDS)
    if waited is None:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail="instance started but produced no link within the timeout",
        )
    if not waited.get("output_link"):
        # instance terminated during the wait
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=waited.get("error") or "instance ended without producing a link",
        )
    waited["id"] = instance["id"]
    return _link_payload(waited)


def _link_payload(row: dict) -> dict:
    return {
        "instance_id": row.get("id"),
        "output_link": row["output_link"],
        "status": row.get("status"),
        "expires_at": row.get("timeout_at"),
    }


@router.get("/disconnect")
async def disconnect(user=Depends(require_client)):
    """Authorized disconnect: stop the user's live app-session instance(s).

    Idempotent — returns the list of instance ids that were actually stopped
    (empty if nothing was live, e.g. a duplicate or late disconnect).
    """
    stopped = await instance_service.stop_app_session(user["id"])
    return {"stopped": stopped, "instance_id": stopped[-1] if stopped else None}
