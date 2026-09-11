"""VK relay fallback for the Android app's whitelist-bypass flow.

Some carrier whitelists that let this server's own domain and even the Yandex
Function proxy through still block them outright, while ``api.vk.com`` stays
reachable (it's on nearly every provider's whitelist). This module keeps a
fresh 5-minute anonymous instance link flowing into a private VK dialog
(this community -> the admin's VK account, see ``VK_RELAY_PEER_ID`` in
config.py) via ``messages.send``, so the Android app can read it back via
``messages.getHistory`` as a last-resort fallback when it can't reach the
server directly at all.

The dialog is private (a 1:1 conversation between the community and one
admin account) — never posted to the community wall, so it never surfaces to
regular members or search. Disabled by default; set WB_VK_COMMUNITY_TOKEN and
WB_VK_RELAY_PEER_ID to enable.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

from config import (
    APP_TEMP_TIMEOUT,
    VK_API_VERSION,
    VK_COMMUNITY_TOKEN,
    VK_RELAY_INTERVAL_SECONDS,
    VK_RELAY_PEER_ID,
)
from db import db
from services import instance_service, service_registry

log = logging.getLogger("vk_relay")

VK_API_BASE = "https://api.vk.com/method"
_LINK_WAIT_SECONDS = 30.0
_LINK_POLL_INTERVAL = 0.5


def is_configured() -> bool:
    return bool(VK_COMMUNITY_TOKEN) and VK_RELAY_PEER_ID > 0


async def _resolve_service():
    services = await service_registry.list()
    return next((s for s in services if s["enabled"]), None)


async def _wait_for_output_link(instance_id: int, timeout: float) -> str | None:
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
            return None
        await asyncio.sleep(_LINK_POLL_INTERVAL)
    return None


async def _vk_send_link(link: str) -> bool:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{VK_API_BASE}/messages.send",
            data={
                "user_id": VK_RELAY_PEER_ID,
                "random_id": random.randint(1, 2**31 - 1),
                "message": link,
                "access_token": VK_COMMUNITY_TOKEN,
                "v": VK_API_VERSION,
            },
        )
        data = resp.json()
    if "error" in data:
        log.warning("VK relay: messages.send failed: %s", data["error"].get("error_msg"))
        return False
    return True


class VkRelayService:
    """Background loop: spin up a fresh temp instance, push its link to VK,
    stop the previous one. Mirrors RemnawaveSyncService's start/shutdown shape.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._current_instance_id: int | None = None

    async def start(self) -> None:
        if not is_configured():
            log.info("VK relay disabled (WB_VK_COMMUNITY_TOKEN / WB_VK_RELAY_PEER_ID not set)")
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="vk-relay")
            log.info("VK relay service started (interval=%ss)", VK_RELAY_INTERVAL_SECONDS)

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort stop
                pass
        self._task = None
        await self._stop_current()
        log.info("VK relay service stopped")

    async def _stop_current(self) -> None:
        iid = self._current_instance_id
        self._current_instance_id = None
        if iid is None:
            return
        try:
            await instance_service.stop(user_id=1, instance_id=iid)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log.exception("VK relay: failed stopping previous instance %s", iid)

    async def _cycle(self) -> None:
        svc = await _resolve_service()
        if svc is None:
            log.warning("VK relay: no enabled service to launch")
            return
        instance = await instance_service.start(
            user_id=1,
            service_id=svc["id"],
            timeout_seconds=APP_TEMP_TIMEOUT,
            is_quick=True,
        )
        instance_id = instance["id"]
        link = instance.get("output_link") or await _wait_for_output_link(
            instance_id, _LINK_WAIT_SECONDS
        )
        if not link:
            log.warning("VK relay: instance %s never produced a link", instance_id)
            try:
                await instance_service.stop(user_id=1, instance_id=instance_id)
            except Exception:  # noqa: BLE001
                pass
            return
        if not await _vk_send_link(link):
            # Delivery failed — don't strand a running instance nobody can read.
            try:
                await instance_service.stop(user_id=1, instance_id=instance_id)
            except Exception:  # noqa: BLE001
                pass
            return
        # New link delivered — now retire the previous one.
        await self._stop_current()
        self._current_instance_id = instance_id
        log.info("VK relay: pushed fresh link (instance %s)", instance_id)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._cycle()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("VK relay cycle crashed (will retry next interval)")
            waited = 0.0
            while waited < VK_RELAY_INTERVAL_SECONDS and not self._stop.is_set():
                await asyncio.sleep(min(5.0, VK_RELAY_INTERVAL_SECONDS - waited))
                waited += 5.0


vk_relay = VkRelayService()
