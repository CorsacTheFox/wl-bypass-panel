"""Admin routes: CRUD for clients, CRUD for services, global view."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from config import DEFAULT_MAX_CONCURRENT
from security import require_admin
from services import (
    NotFoundError,
    instance_service,
    service_registry,
    user_service,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
class ClientCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    max_concurrent: int = Field(default=DEFAULT_MAX_CONCURRENT, ge=0, le=10)


class ClientUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=128)
    max_concurrent: int | None = Field(default=None, ge=0, le=10)
    enabled: bool | None = None


@router.get("/clients")
async def list_clients():
    return await user_service.list_clients()


@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate):
    try:
        return await user_service.create_client(
            body.username, body.password, body.max_concurrent
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch("/clients/{client_id}")
async def update_client(client_id: int, body: ClientUpdate):
    try:
        return await user_service.update(
            client_id,
            password=body.password,
            max_concurrent=body.max_concurrent,
            enabled=body.enabled,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="client not found")


@router.delete("/clients/{client_id}")
async def delete_client(client_id: int):
    # Stop the client's live instances first, then remove the account.
    await instance_service.stop_all_for_user(client_id)
    try:
        await user_service.delete(client_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="client not found")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
class ServiceCreate(BaseModel):
    name: str
    binary_path: str
    credentials: str
    extra_args: str = ""
    enabled: bool = True


class ServiceUpdate(BaseModel):
    name: str | None = None
    binary_path: str | None = None
    credentials: str | None = None
    extra_args: str | None = None
    enabled: bool | None = None


@router.get("/services")
async def list_services():
    return await service_registry.list()


@router.post("/services", status_code=status.HTTP_201_CREATED)
async def create_service(body: ServiceCreate):
    try:
        return await service_registry.create(
            body.name, body.binary_path, body.credentials, body.extra_args, body.enabled
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/services/{service_id}")
async def update_service(service_id: int, body: ServiceUpdate):
    try:
        return await service_registry.update(
            service_id,
            **body.model_dump(exclude_none=True),
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail="service not found")


@router.delete("/services/{service_id}")
async def delete_service(service_id: int):
    try:
        await service_registry.delete(service_id)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="service not found")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Global overview (live instances across all users)
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def overview():
    from db import db
    rows = await db.fetchall(
        """SELECT i.id, i.user_id, u.username, s.name AS service_name,
                  i.pid, i.status, i.started_at
             FROM instances i
             JOIN users u ON u.id = i.user_id
             JOIN services s ON s.id = i.service_id
            WHERE i.status IN ('pending','running','stopping')
            ORDER BY i.started_at DESC"""
    )
    return {"live": [dict(r) for r in rows], "count": len(rows)}
