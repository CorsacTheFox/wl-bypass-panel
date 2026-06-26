"""Admin routes: CRUD for clients, CRUD for services, cookies & binaries uploads, global view."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from config import DEFAULT_MAX_CONCURRENT
from security import require_admin
from services import (
    BinariesError,
    CookiesError,
    NotFoundError,
    binaries_store,
    cookies_store,
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
    password: str | None = Field(default=None, min_length=6, max_length=128)
    max_concurrent: int = Field(default=DEFAULT_MAX_CONCURRENT, ge=0, le=10)


class BulkCreateIn(BaseModel):
    usernames: str  # comma-separated list
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


@router.post("/clients/bulk", status_code=status.HTTP_201_CREATED)
async def create_clients_bulk(body: BulkCreateIn):
    usernames = [u.strip() for u in body.usernames.split(",") if u.strip()]
    if not usernames:
        raise HTTPException(status_code=400, detail="no usernames provided")
    result = await user_service.create_clients_bulk(usernames, body.max_concurrent)
    return result


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
# Cookies files (uploaded as a zip of cookies-<binary>.json)
# --------------------------------------------------------------------------- #
# Hard cap on a single cookies zip. Cookies JSONs are tiny (a few KB), so a
# megabyte is plenty and keeps memory bounded when reading into RAM.
COOKIES_ZIP_MAX_BYTES = 1 * 1024 * 1024


@router.get("/cookies")
async def list_cookies():
    """List all cookies-*.json available for use as a service's credentials."""
    return cookies_store.list()


@router.post("/cookies", status_code=status.HTTP_201_CREATED)
async def upload_cookies(file: UploadFile = File(...)):
    """Upload a zip of cookies-*.json files. Top-level members matching
    `cookies-*.json` are extracted into the cookies directory.

    Example zip contents:
        cookies-dion.json
        cookies-wbstream.json
        cookies-vk.json
        cookies-yandex.json
    """
    data = await file.read()
    if len(data) > COOKIES_ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zip too large: {len(data)} bytes "
                   f"(max {COOKIES_ZIP_MAX_BYTES})",
        )
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        extracted = cookies_store.save_zip(data)
    except CookiesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "extracted": extracted, "all": cookies_store.list()}


@router.delete("/cookies/{name}")
async def delete_cookies(name: str):
    """Delete a single cookies-<name>.json by filename."""
    try:
        cookies_store.delete(name)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="cookies file not found")
    except CookiesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Binaries (uploaded as a zip of headless-*-creator, headless-*-joiner, etc.)
# --------------------------------------------------------------------------- #
# Hard cap on a single binaries zip. Go binaries are typically 5-15 MB each,
# so 200 MB allows for a generous archive (up to ~20 binaries).
BINARIES_ZIP_MAX_BYTES = 200 * 1024 * 1024


@router.get("/binaries")
async def list_binaries():
    """List all binaries available for use as a service's binary_path."""
    return binaries_store.list()


@router.post("/binaries", status_code=status.HTTP_201_CREATED)
async def upload_binaries(file: UploadFile = File(...)):
    """Upload a zip of headless-* and whitelist-bypass binaries.

    Example zip contents:
        headless-wbstream-creator
        headless-wbstream-joiner
        headless-dion-creator
        headless-vk-bot
        ...
    """
    data = await file.read()
    if len(data) > BINARIES_ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"zip too large: {len(data)} bytes "
                   f"(max {BINARIES_ZIP_MAX_BYTES})",
        )
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        extracted = binaries_store.save_zip(data)
    except BinariesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "extracted": extracted, "all": binaries_store.list()}


@router.delete("/binaries/{name}")
async def delete_binary(name: str):
    """Delete a single binary by filename."""
    try:
        binaries_store.delete(name)
    except NotFoundError:
        raise HTTPException(status_code=404, detail="binary not found")
    except BinariesError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Global overview (live instances across all users)
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def overview():
    from db import db
    rows = await db.fetchall(
        """SELECT i.id, i.user_id, u.username, s.name AS service_name,
                  i.pid, i.status, i.started_at, i.output_link
             FROM instances i
             JOIN users u ON u.id = i.user_id
             JOIN services s ON s.id = i.service_id
            WHERE i.status IN ('pending','running','stopping')
            ORDER BY i.started_at DESC"""
    )
    return {"live": [dict(r) for r in rows], "count": len(rows)}
