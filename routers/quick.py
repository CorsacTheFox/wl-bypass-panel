"""Quick-launch routes: unauthenticated instance creation via token.

POST /api/quick/start   — creates a 15-min instance for the first enabled service
GET  /api/quick/status/{id} — polls instance status & output_link

Authorised by a shared token (WB_QUICK_TOKEN env var). If the token is empty
the routes are disabled (404).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from config import BASE_DIR, QUICK_TOKEN
from db import db
from services import (
    ConcurrencyLimitError,
    instance_service,
    service_registry,
)

router = APIRouter(prefix="/api/quick", tags=["quick"])

QUICK_HTML = BASE_DIR / "quick.html"  # noqa: F841 — referenced only for documentation
_QUICK_TIMEOUT = 900  # 15 minutes in seconds


def _check_token(request: Request) -> None:
    """Validate the shared secret token from the query string or header."""
    if not QUICK_TOKEN:
        raise HTTPException(status_code=404, detail="quick launch is disabled")
    token = request.query_params.get("token") or request.headers.get("X-Quick-Token", "")
    if token != QUICK_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def quick_start(request: Request):
    """Create a 15-minute instance using the first enabled service."""
    _check_token(request)

    # Find the first enabled service
    services = await service_registry.list()
    svc = next((s for s in services if s["enabled"]), None)
    if svc is None:
        raise HTTPException(status_code=404, detail="no enabled services available")

    try:
        return await instance_service.start(
            user_id=1,  # quick-launch always uses user 1 (admin)
            service_id=svc["id"],
            timeout_seconds=_QUICK_TIMEOUT,
        )
    except ConcurrencyLimitError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/status/{instance_id}")
async def quick_status(instance_id: int, request: Request):
    """Poll instance status and output_link."""
    _check_token(request)

    row = await db.fetchone(
        """SELECT i.id, i.status, i.output_link, i.error
           FROM instances i WHERE i.id=?""",
        (instance_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="instance not found")
    return dict(row)
