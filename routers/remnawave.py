"""Admin routes for the Remnawave integration.

Configure the source panel, test the connection, list its squads, preview a
migration, run it, and manage the background auto-sync. Everything here is
read-only with respect to the Remnawave panel.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from config import DEFAULT_MAX_CONCURRENT
from remnawave import (
    CONF_API_KEY,
    CONF_AUTO_SYNC_ENABLED,
    CONF_AUTO_SYNC_INTERVAL,
    CONF_AUTO_SYNC_SQUADS,
    CONF_PANEL_URL,
    CONF_SYNC_GRANT_CREATE,
    CONF_SYNC_MAX_CONCURRENT,
    CONF_SYNC_ONLY_ACTIVE,
    RemnawaveError,
    get_api_key,
    get_last_sync,
    get_panel_url,
    get_sync_options,
    mask_key,
    remnawave_service,
    _set_setting,
)
from security import require_admin

router = APIRouter(prefix="/api/admin/remnawave", tags=["remnawave"],
                   dependencies=[Depends(require_admin)])


# --------------------------------------------------------------------------- #
# Configuration (panel URL + API key + auto-sync options)
# --------------------------------------------------------------------------- #
class RemnawaveConfigUpdate(BaseModel):
    """Partial config update. An empty ``api_key`` keeps the stored one."""

    panel_url: str | None = None
    api_key: str | None = Field(default=None, max_length=512)
    auto_sync_enabled: bool | None = None
    auto_sync_interval_min: int | None = Field(default=None, ge=1, le=1440)
    auto_sync_squads: list[str] | None = None
    sync_only_active: bool | None = None
    sync_grant_create: bool | None = None
    sync_max_concurrent: int | None = Field(default=None, ge=0, le=10)


@router.get("/config")
async def get_config():
    """Current Remnawave settings. The API key is only ever returned masked."""
    url = await get_panel_url()
    key = await get_api_key()
    return {
        "panel_url": url,
        "api_key_masked": mask_key(key),
        "configured": bool(url and key),
        "auto_sync": await get_sync_options(),
        "last_sync": await get_last_sync(),
    }


@router.put("/config")
async def put_config(body: RemnawaveConfigUpdate):
    if body.panel_url is not None:
        await _set_setting(CONF_PANEL_URL, body.panel_url.strip().rstrip("/"))
    if body.api_key:  # empty/None = keep the current key
        await _set_setting(CONF_API_KEY, body.api_key.strip())
    if body.auto_sync_enabled is not None:
        await _set_setting(CONF_AUTO_SYNC_ENABLED, "1" if body.auto_sync_enabled else "0")
    if body.auto_sync_interval_min is not None:
        await _set_setting(CONF_AUTO_SYNC_INTERVAL, str(body.auto_sync_interval_min))
    if body.auto_sync_squads is not None:
        await _set_setting(CONF_AUTO_SYNC_SQUADS, json.dumps(body.auto_sync_squads))
    if body.sync_only_active is not None:
        await _set_setting(CONF_SYNC_ONLY_ACTIVE, "1" if body.sync_only_active else "0")
    if body.sync_grant_create is not None:
        await _set_setting(CONF_SYNC_GRANT_CREATE, "1" if body.sync_grant_create else "0")
    if body.sync_max_concurrent is not None:
        await _set_setting(CONF_SYNC_MAX_CONCURRENT, str(body.sync_max_concurrent))
    return await get_config()


# --------------------------------------------------------------------------- #
# Panel interaction
# --------------------------------------------------------------------------- #
@router.post("/test")
async def test_connection():
    """Check the panel URL/API key by fetching the panel's metadata."""
    try:
        result = await remnawave_service.test_connection()
    except RemnawaveError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@router.get("/squads")
async def list_squads():
    try:
        squads = await remnawave_service.list_squads()
    except RemnawaveError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return squads


class MigrateRequest(BaseModel):
    squads: list[str] = Field(min_length=1)
    only_active: bool = True
    # ON by default: imported active users may create instances
    grant_create_instances: bool = True
    max_concurrent: int = Field(default=DEFAULT_MAX_CONCURRENT, ge=0, le=10)


@router.post("/preview")
async def preview(body: MigrateRequest):
    """Dry-run: what would happen if migrate ran now. Writes nothing."""
    try:
        return await remnawave_service.preview(body.squads, body.only_active)
    except RemnawaveError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/migrate")
async def migrate(body: MigrateRequest):
    """Import new users from the selected squads (idempotent — already
    imported users are skipped by their Remnawave UUID)."""
    try:
        return await remnawave_service.migrate(
            body.squads,
            only_active=body.only_active,
            grant_create_instances=body.grant_create_instances,
            max_concurrent=body.max_concurrent,
        )
    except RemnawaveError as e:
        raise HTTPException(status_code=400, detail=str(e))
