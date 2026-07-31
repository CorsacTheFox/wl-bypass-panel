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
from routers import app as app_router
from routers import auth as auth_router
from routers import client as client_router
from routers import quick as quick_router
from routers import telegram as telegram_router
from services import user_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("app")

STATIC_DIR = BASE_DIR / "static"


async def _reconcile_stale_instances() -> None:
    """After a restart, live instance rows point at binaries that may still be
    running as OS processes (a ``systemctl restart`` doesn't kill them — they
    live in their own session and the systemd unit uses ``KillMode=mixed``).

    For each live row we re-adopt the PID if it is still alive (reattach
    supervision: waiter, tailer, rescheduled timeout) and only mark ``crashed``
    the ones whose process is genuinely gone.
    """
    from process_manager import _pid_alive  # local import: avoid cycle at import time

    rows = await db.fetchall(
        """SELECT id, pid, timeout_at, output_link
             FROM instances
            WHERE status IN ('pending','running','stopping')"""
    )
    if not rows:
        return

    adopted = 0
    crashed = 0
    for r in rows:
        iid = r["id"]
        pid = r["pid"]
        if _pid_alive(pid):
            ok = await process_manager.reattach(
                iid,
                pid,
                timeout_at=r["timeout_at"],
                has_output_link=bool(r["output_link"]),
            )
            if ok:
                adopted += 1
                continue
        # PID is gone (or reattach reported it dead): be honest in the UI.
        await db.execute(
            """UPDATE instances
                  SET status='crashed', ended_at=datetime('now'),
                      error='process not found after server restart'
                WHERE id=?""",
            (iid,),
        )
        crashed += 1
    log.warning(
        "Reconciled after restart: re-adopted %d live instance(s), marked %d crashed",
        adopted, crashed,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    await db.connect()
    await user_service.ensure_bootstrap_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
    # Start the process manager (reaper loop) BEFORE re-adopting orphaned
    # instances so their restarted waiters/tailers run on a live event loop.
    await process_manager.start()
    await _reconcile_stale_instances()
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
app.include_router(telegram_router.router)
app.include_router(admin_router.router)
app.include_router(client_router.router)
app.include_router(quick_router.router)
app.include_router(app_router.router)

# Static assets (CSS/JS)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health():
    return {"ok": True, "live_processes": process_manager.live_count()}


# Quick-launch page: NON-INTERACTIVE. GET /quick starts an instance, waits for
# the join link, and renders a styled HTML page showing it (server-side, no JS).
# The raw plain-text link is available at /api/quick/link for bots/integrations.
from fastapi import HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402
from routers.quick import produce_link  # noqa: E402


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quick_html_page(link: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Quick Launch</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <style>
    *,*::before,*::after{{box-sizing:border-box;}}
    body{{font-family:'Inter',system-ui,-apple-system,sans-serif;margin:0;
          background:#08080a;color:#d1d5db;min-height:100vh;
          display:flex;align-items:center;justify-content:center;padding:1rem;}}
    .wrap{{width:100%;max-width:28rem;}}
    .card{{background:#14141a;border:1px solid rgba(255,255,255,0.06);
           border-radius:16px;padding:2rem;text-align:center;}}
    .logo{{width:3rem;height:3rem;border-radius:12px;background:#7c3aed;
           display:flex;align-items:center;justify-content:center;margin:0 auto 1.25rem;
           box-shadow:0 8px 24px rgba(124,58,237,0.25);}}
    h1{{color:#fff;font-size:1.25rem;font-weight:600;margin:0 0 .5rem;}}
    .sub{{color:#6b7280;font-size:.8rem;margin:0 0 1.5rem;}}
    .link-box{{background:#0e0e12;border:1px solid rgba(255,255,255,0.08);
               border-radius:10px;padding:.85rem 1rem;margin:0 0 1.25rem;
               word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
               font-size:.8rem;color:#67e8f9;}}
    .btn{{display:inline-flex;align-items:center;justify-content:center;gap:.4rem;
          padding:.7rem 1.5rem;border-radius:10px;font-size:.85rem;font-weight:500;
          cursor:pointer;border:none;outline:none;text-decoration:none;}}
    .btn-success{{background:#16a34a;color:#fff;}}
    .btn-success:hover{{background:#15803d;}}
    .hint{{color:#4b5563;font-size:.7rem;margin:1.25rem 0 0;}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="logo">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/>
        </svg>
      </div>
      <h1>Instance ready</h1>
      <p class="sub">Your link is below — tap to copy it.</p>
      <div class="link-box">{_escape(link)}</div>
      <a class="btn btn-success" href="{_escape(link)}">Open Link</a>
      <p class="hint">15-minute session &middot; auto-created</p>
    </div>
  </div>
</body>
</html>"""


@app.get("/quick")
async def quick_page():
    """Styled non-interactive page showing the ready link (server-rendered)."""
    try:
        link = await produce_link()
    except HTTPException:
        raise
    return HTMLResponse(_quick_html_page(link))


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
