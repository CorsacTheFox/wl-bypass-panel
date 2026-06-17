"""Client routes: utilization, list instances, start call, stop call.

Clients only ever see and act on their own resources (user_id is taken
from the auth dependency, never from the request body).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from security import require_client
from services import (
    ConcurrencyLimitError,
    NotFoundError,
    instance_service,
    service_registry,
)

router = APIRouter(prefix="/api/client", tags=["client"], dependencies=[Depends(require_client)])


class StartCallIn(BaseModel):
    service_id: int
    timeout_seconds: int | None = None  # falls back to service/config default


@router.get("/services")
async def list_available_services(user=Depends(require_client)):
    """Services a client may launch against (no credentials leaked)."""
    services = await service_registry.list()
    return [
        {"id": s["id"], "name": s["name"], "enabled": bool(s["enabled"])}
        for s in services
    ]


@router.get("/utilization")
async def utilization(user=Depends(require_client)):
    return await instance_service.utilization(user["id"])


@router.get("/instances")
async def list_instances(user=Depends(require_client)):
    return await instance_service.list_for_user(user["id"], include_history=False)


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def start_call(body: StartCallIn, user=Depends(require_client)):
    try:
        return await instance_service.start(
            user["id"], body.service_id, body.timeout_seconds or 3600
        )
    except ConcurrencyLimitError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except NotFoundError:
        raise HTTPException(status_code=404, detail="service not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop/{instance_id}")
async def stop_call(instance_id: int, user=Depends(require_client)):
    try:
        return await instance_service.stop(user["id"], instance_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="instance not found")
