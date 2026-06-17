"""FastAPI application entrypoint.

Lifespan handles:
  * DB connect + schema
  * bootstrap admin (first run only)
  * reconcile any stale 'running' rows from a previous crash
  * start the process manager (reaper loop)
  * on shutdown: stop all live processes + close DB

The SPA is served from /static and the root path.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import ADMIN_PASSWORD, ADMIN_USERNAME, BASE_DIR, ensure_dirs
from db import db
from process_manager import process_manager
from routers import admin as admin_router
from routers import auth as auth_router
from routers import client as client_router
from services import user_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("app")

STATIC_DIR = BASE_DIR / "static"


async def _reconcile_stale_instances() -> None:
    """After a crash/restart, any rows still marked running are orphans (their
    PIDs are no longer our children). Mark them crashed so the UI is honest."""
    rows = await db.fetchall(
        """SELECT id FROM instances WHERE status IN ('pending','running','stopping')"""
    )
    for r in rows:
        await db.execute(
            """UPDATE instances
                  SET status='crashed', ended_at=datetime('now'),
                      error='process orphaned by server restart'
                WHERE id=?""",
            (r["id"],),
        )
    if rows:
        log.warning("Reconciled %d orphaned instance(s) after restart", len(rows))


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    await db.connect()
    await user_service.ensure_bootstrap_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
    await _reconcile_stale_instances()
    await process_manager.start()
    log.info("Started — admin=%s, listening on config HOST/PORT", ADMIN_USERNAME)
    try:
        yield
    finally:
        log.info("Shutting down: stopping live processes")
        await process_manager.shutdown()
        await db.close()


app = FastAPI(title="Whitelist-Bypass Instance Manager", lifespan=lifespan)

# Routers
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(client_router.router)

# Static assets (CSS/JS)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health():
    return {"ok": True, "live_processes": process_manager.live_count()}


# SPA fallback: any non-API, non-static GET -> index.html
INDEX = STATIC_DIR / "index.html"


@app.get("/")
async def index():
    if INDEX.exists():
        return FileResponse(INDEX)
    return {"message": "UI not built. Place static/index.html."}


@app.get("/{path:path}")
async def spa_fallback(path: str):
    # Don't shadow API or static routes.
    if path.startswith("api/") or path.startswith("static/"):
        return {"detail": "Not found"}
    candidate = STATIC_DIR / path
    if candidate.is_file():
        return FileResponse(candidate)
    if INDEX.exists():
        return FileResponse(INDEX)
    return {"detail": "Not found"}
