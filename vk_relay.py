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
import os
import random
from datetime import datetime, timedelta, timezone

import httpx

from config import (
    APP_TEMP_TIMEOUT,
    VK_API_VERSION,
    VK_COMMUNITY_TOKEN,
    VK_RELAY_ACTIVE_BYTES,
    VK_RELAY_CHECK_SECONDS,
    VK_RELAY_EXTEND_SECONDS,
    VK_RELAY_INTERVAL_SECONDS,
    VK_RELAY_MAX_LIFETIME_SECONDS,
    VK_RELAY_PEER_ID,
)
from db import db
from process_manager import process_manager
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


def _process_group_io(pgid: int) -> int | None:
    """Sum of rchar+wchar over every process in *pgid* (the binary is spawned
    with start_new_session, so its whole tree shares the group). rchar/wchar
    count socket I/O too, which is what we use as the "tunnel is in use"
    signal. Returns None where /proc isn't available (non-Linux)."""
    if not os.path.isdir("/proc"):
        return None
    total = 0
    found = False
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat = f.read()
            # pgrp is the 3rd field after the parenthesised comm.
            if int(stat.rsplit(")", 1)[1].split()[2]) != pgid:
                continue
            with open(f"/proc/{entry}/io") as f:
                for line in f:
                    if line.startswith(("rchar:", "wchar:")):
                        total += int(line.split()[1])
            found = True
        except (OSError, ValueError, IndexError):
            continue
    return total if found else None


async def _extend(instance_id: int, seconds: int) -> None:
    """Same mechanism as the app heartbeat endpoint: push timeout_at out and
    re-arm the in-process timeout killer."""
    new_timeout_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    await db.execute(
        "UPDATE instances SET timeout_at=? WHERE id=? AND status IN ('pending','running','stopping')",
        (new_timeout_at, instance_id),
    )
    await process_manager.reschedule_timeout(instance_id, float(seconds))


class VkRelayService:
    """Background loop: spin up a fresh temp instance, push its link to VK,
    stop the previous one. Mirrors RemnawaveSyncService's start/shutdown shape.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._current_instance_id: int | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Instances whose link went out via VK and may still carry a live
        # tunnel: iid -> [pid, last io counter, started monotonic].
        self._watched: dict[int, list] = {}

    async def start(self) -> None:
        if not is_configured():
            log.info("VK relay disabled (WB_VK_COMMUNITY_TOKEN / WB_VK_RELAY_PEER_ID not set)")
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="vk-relay")
            self._keepalive_task = asyncio.create_task(self._keepalive(), name="vk-relay-keepalive")
            log.info("VK relay service started (interval=%ss)", VK_RELAY_INTERVAL_SECONDS)

    async def shutdown(self) -> None:
        self._stop.set()
        for task in (self._task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort stop
                    pass
        self._task = None
        self._keepalive_task = None
        await self._stop_current()
        for iid in list(self._watched):
            await self._stop_instance(iid)
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

    async def _stop_instance(self, iid: int) -> None:
        self._watched.pop(iid, None)
        try:
            await instance_service.stop(user_id=1, instance_id=iid)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log.exception("VK relay: failed stopping instance %s", iid)

    async def _keepalive(self) -> None:
        """Server-side heartbeat for VK-relayed instances. The app can't
        heartbeat them (in VK mode it only has the bare link, no instance id
        or bearer, and usually can't reach this server at all), so we extend
        them ourselves while their tunnel carries traffic."""
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            await asyncio.sleep(VK_RELAY_CHECK_SECONDS)
            for iid, entry in list(self._watched.items()):
                try:
                    pid, last_io, started = entry
                    row = await db.fetchone("SELECT status FROM instances WHERE id=?", (iid,))
                    if row is None or row["status"] in ("stopped", "exited", "crashed", "timeout"):
                        self._watched.pop(iid, None)
                        continue
                    io = _process_group_io(pid)
                    delta = (io - last_io) if (io is not None and last_io is not None) else 0
                    entry[1] = io
                    active = delta >= VK_RELAY_ACTIVE_BYTES
                    log.debug("VK relay: instance %s io delta=%s active=%s", iid, delta, active)
                    too_old = (
                        VK_RELAY_MAX_LIFETIME_SECONDS > 0
                        and loop.time() - started > VK_RELAY_MAX_LIFETIME_SECONDS
                    )
                    if active and not too_old:
                        await _extend(iid, VK_RELAY_EXTEND_SECONDS)
                    elif iid != self._current_instance_id:
                        # Retired link and nobody on it anymore — free it now.
                        log.info("VK relay: instance %s idle, stopping", iid)
                        await self._stop_instance(iid)
                    # Idle current link: leave it on its normal 5-min timer.
                except Exception:  # noqa: BLE001 — the loop must survive anything
                    log.exception("VK relay keepalive failed for instance %s", iid)

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
        # New link delivered. The previous one is NOT killed here: a user may
        # still be tunnelling through it. The keepalive loop stops it once it
        # goes idle (or lets it hit its own 5-min timeout).
        self._current_instance_id = instance_id
        row = await db.fetchone("SELECT pid FROM instances WHERE id=?", (instance_id,))
        pid = row["pid"] if row else None
        if pid:
            self._watched[instance_id] = [pid, _process_group_io(pid), asyncio.get_event_loop().time()]
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
