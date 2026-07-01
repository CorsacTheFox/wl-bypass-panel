"""Process Manager — the core of the application.

Responsibilities:
  * spawn a compiled `whitelist-bypass` binary as a tracked child process,
  * keep an in-memory map of live processes keyed by instance id,
  * reap exited children (no zombies), updating the DB with exit status,
  * enforce per-instance timeouts,
  * gracefully terminate (SIGTERM -> grace -> SIGKILL).

Design notes
------------
We use `asyncio.create_subprocess_exec`. Each spawned process is wrapped in
a `TrackedProcess` that owns a background "waiter" task. The waiter awaits
`proc.wait()`, so the child is reaped promptly by asyncio the moment it
exits — there is no `waitpid` needed and no zombies accumulate.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from config import (
    COOKIE_FLAG,
    ERROR_TAIL_BYTES,
    LOGS_DIR,
    PROCESS_KILL_GRACE_SECONDS,
    REAPER_INTERVAL_SECONDS,
    generate_proxychains_conf,
)
from db import db

log = logging.getLogger("process_manager")

# Regex to extract the join_link from binary output.
# All creator binaries emit:
#   CALL CREATED
#   join_link: <scheme>://<payload>
# The scheme may be wbstream://, dion.vc/..., https://vk.com/call/..., tm://, etc.
_RE_JOIN_LINK = re.compile(r"join_link:\s*(\S+)")

# Statuses (must match the DB CHECK constraint)
S_PENDING = "pending"
S_RUNNING = "running"
S_STOPPING = "stopping"
S_STOPPED = "stopped"     # killed by us (graceful)
S_EXITED = "exited"       # exited on its own, code 0
S_CRASHED = "crashed"     # exited on its own, non-zero
S_TIMEOUT = "timeout"     # killed by us due to timeout

TERMINAL = {S_STOPPED, S_EXITED, S_CRASHED, S_TIMEOUT}
LIVE = {S_PENDING, S_RUNNING, S_STOPPING}


class ProcessError(Exception):
    """Raised when a binary fails to spawn or is not found."""


class TrackedProcess:
    """A single live child process and its lifecycle bookkeeping."""

    def __init__(self, instance_id: int, proc: asyncio.subprocess.Process,
                 log_path: Optional[Path] = None):
        self.instance_id = instance_id
        self.proc = proc
        self.log_path = log_path
        self.waiter: asyncio.Task | None = None
        self.tailer: asyncio.Task | None = None   # log scanner for output_link
        self.kill_task: asyncio.Task | None = None  # timeout enforcer

    @property
    def pid(self) -> int | None:
        return self.proc.pid

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None


class ProcessManager:
    """Singleton manager. Lives for the whole process lifetime."""

    def __init__(self) -> None:
        # instance_id -> TrackedProcess (only LIVE ones are kept here)
        self._tracked: dict[int, TrackedProcess] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        """Stop all tracked processes on shutdown."""
        if self._reaper_task:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        # Cancel all tailers and kill anything still alive.
        ids = list(self._tracked.keys())
        for iid in ids:
            t = self._tracked[iid]
            if t.tailer and not t.tailer.done():
                t.tailer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t.tailer
            await self._kill(iid, reason="shutdown", timeout_status=False)

    # ------------------------------------------------------------------ #
    # spawn
    # ------------------------------------------------------------------ #
    async def spawn(
        self,
        instance_id: int,
        binary_path: str,
        credentials: str,
        extra_args: str = "",
        env: Optional[dict[str, str]] = None,
        proxy_type: str = "",
        proxy_host: str = "",
        proxy_port: str = "",
    ) -> int:
        """Spawn the binary for `instance_id`. Returns the child PID.

        ``credentials`` is treated as a **path** to a cookies JSON file. If it
        resolves to an existing file, the binary receives
        ``-cookies <path>``; if it is empty/blank, no cookies flag is emitted
        (some binaries — e.g. the *-joiner tools — take no cookies at all).
        Extra args configured per service are appended verbatim.

        If *proxy_type* is non-empty (socks5/socks4/http) and *proxy_host* /
        *proxy_port* are set, a per-instance proxychains4 config is generated
        and the command is wrapped through proxychains4.

        stdout+stderr of the binary are tee'd to ``LOGS_DIR/instance-<id>.log``
        so crashes are diagnosable; on non-zero exit the tail of that log is
        written to ``instances.error``.
        """
        args = self._build_command(binary_path, credentials, extra_args)

        # Wrap with proxychains4 if a proxy is configured for this service.
        if proxy_type and proxy_host and proxy_port:
            import shutil
            pc4 = shutil.which("proxychains4")
            if pc4:
                conf_path = LOGS_DIR / f"instance-{instance_id}-proxychains.conf"
                generate_proxychains_conf(conf_path, proxy_type, proxy_host, proxy_port)
                log.info("proxychains4 enabled for instance %d: %s://%s:%s",
                         instance_id, proxy_type, proxy_host, proxy_port)
                args = [pc4, "-f", str(conf_path)] + args
            else:
                log.warning("proxychains4 requested but binary not found in PATH — launching without proxy")

        # One log file per instance lifecycle, opened for the child to inherit.
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"instance-{instance_id}.log"
        log_file = open(log_path, "ab", buffering=0)
        log_file.write(
            f"==== instance {instance_id} @ {datetime.now(timezone.utc).isoformat()} ====\n"
            f"cmd: {shlex.join(args)}\n".encode()
        )

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,  # merge stderr into the log file
                    stdin=asyncio.subprocess.DEVNULL,
                    # New process group so we can signal the whole tree if needed.
                    start_new_session=True,
                    env=env,
                )
            except FileNotFoundError as e:
                await self._mark_terminal(instance_id, S_CRASHED, error=f"binary not found: {binary_path}")
                raise ProcessError(f"Binary not found: {binary_path}") from e
            except OSError as e:
                await self._mark_terminal(instance_id, S_CRASHED, error=f"spawn error: {e}")
                raise ProcessError(f"Failed to spawn binary: {e}") from e
        finally:
            # The child has its own dup'd fd; the parent can close its handle.
            log_file.close()

        tracked = TrackedProcess(instance_id, proc, log_path=log_path)
        async with self._lock:
            self._tracked[instance_id] = tracked

        await db.execute(
            "UPDATE instances SET pid=?, status=? WHERE id=?",
            (proc.pid, S_RUNNING, instance_id),
        )
        log.info("Spawned instance %d -> pid %d (%s) log=%s",
                 instance_id, proc.pid, binary_path, log_path)

        # Waiter: reaps the process the moment it exits.
        tracked.waiter = asyncio.create_task(self._waiter(tracked))
        # Tailer: scans the log for join_link and writes it to the DB.
        tracked.tailer = asyncio.create_task(self._tailer(tracked))
        return proc.pid

    def _build_command(self, binary_path: str, credentials: str, extra_args: str) -> list[str]:
        """Construct the argv for the binary.

        Cookie handling (see class docstring): a *file path* in `credentials`
        is passed via ``-cookies <path>``; empty credentials emit no flag, so
        binaries that take none (joiners) still work. Per-service `extra_args`
        are appended verbatim — set a different flag there if a binary needs
        e.g. ``-cookie-string`` instead.

        Proxy wrapping (if configured per-service) is handled in `spawn()`,
        not here.
        """
        cmd = [binary_path]
        cred = (credentials or "").strip()
        if cred:
            cmd.extend([COOKIE_FLAG, cred])
        if extra_args and extra_args.strip():
            cmd.extend(shlex.split(extra_args))
        return cmd

    # ------------------------------------------------------------------ #
    # wait / reap
    # ------------------------------------------------------------------ #
    async def _waiter(self, tracked: TrackedProcess) -> None:
        """Await process exit, then reconcile state. Prevents zombies."""
        proc = tracked.proc
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            # We were asked to stop tracking (shouldn't normally happen here).
            return

        # stdout/stderr are redirected to a file (no pipes to drain).
        if tracked.kill_task and not tracked.kill_task.done():
            tracked.kill_task.cancel()

        # If the process was in 'stopping', we already set the terminal
        # status via stop(); otherwise it exited on its own.
        async with self._lock:
            current = self._tracked.get(tracked.instance_id)
            # Only auto-classify if it is still the same tracked object.
            if current is not tracked:
                return
            self._tracked.pop(tracked.instance_id, None)

        # Read current DB status to decide final classification.
        row = await db.fetchone(
            "SELECT status FROM instances WHERE id=?", (tracked.instance_id,)
        )
        if row is None:
            return
        if row["status"] in TERMINAL:
            return  # already terminal (e.g. user stopped it)

        status = S_EXITED if code == 0 else S_CRASHED
        # On crash, capture the tail of the log so the cause is visible in
        # the UI without SSH'ing to read the full file.
        error = None
        if status == S_CRASHED:
            error = self._read_log_tail(tracked.log_path)
        await self._mark_terminal(
            tracked.instance_id, status, exit_code=code, error=error
        )
        log.info("Instance %d exited code=%s -> %s", tracked.instance_id, code, status)

    @staticmethod
    def _read_log_tail(log_path: Optional[Path]) -> Optional[str]:
        """Return the last ERROR_TAIL_BYTES of a binary log, or None."""
        if log_path is None or not log_path.exists():
            return None
        try:
            size = log_path.stat().st_size
            with open(log_path, "rb") as f:
                if size > ERROR_TAIL_BYTES:
                    f.seek(-ERROR_TAIL_BYTES, os.SEEK_END)
                data = f.read()
            text = data.decode("utf-8", errors="replace").strip()
            return text or None
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    # log tailer — extracts join_link from binary output
    # ------------------------------------------------------------------ #
    async def _tailer(self, tracked: TrackedProcess) -> None:
        """Periodically scan the instance log for a ``join_link:`` line.

        All creator binaries emit something like::

            CALL CREATED
            join_link: wbstream://019ed925-...

        Once found, the link is persisted to ``instances.output_link`` in the
        DB and the tailer stops (no point rescanning). If the process exits
        before a link appears the tailer simply ends — no error.
        """
        iid = tracked.instance_id
        log_path = tracked.log_path
        if log_path is None:
            return
        last_pos = 0  # byte offset into the log file
        try:
            while tracked.alive:
                await asyncio.sleep(0.5)
                try:
                    size = log_path.stat().st_size
                except OSError:
                    continue
                if size <= last_pos:
                    continue  # nothing new
                try:
                    with open(log_path, "r", errors="replace") as f:
                        f.seek(last_pos)
                        new_data = f.read()
                except OSError:
                    continue
                last_pos = size
                # Scan for join_link: in newly-read text.
                m = _RE_JOIN_LINK.search(new_data)
                if m:
                    link = m.group(1).strip()
                    await db.execute(
                        "UPDATE instances SET output_link=? WHERE id=?",
                        (link, iid),
                    )
                    log.info("Instance %d: join_link found -> %s", iid, link)
                    return  # done — link captured
        except asyncio.CancelledError:
            return  # process killed or shutdown — nothing to do

    # ------------------------------------------------------------------ #
    # stop / kill
    # ------------------------------------------------------------------ #
    async def stop(self, instance_id: int) -> bool:
        """Gracefully stop: SIGTERM, wait grace, then SIGKILL. Returns True if
        the instance was live and we acted on it."""
        return await self._kill(instance_id, reason="user_stop", timeout_status=False)

    async def _kill(
        self, instance_id: int, *, reason: str, timeout_status: bool
    ) -> bool:
        async with self._lock:
            tracked = self._tracked.get(instance_id)
        if tracked is None or not tracked.alive:
            # Not live in memory — make sure DB reflects that.
            await self._mark_terminal(
                instance_id,
                S_TIMEOUT if timeout_status else S_STOPPED,
                error=reason if timeout_status else None,
            )
            return False

        await db.execute(
            "UPDATE instances SET status=? WHERE id=?", (S_STOPPING, instance_id)
        )

        final_status = S_TIMEOUT if timeout_status else S_STOPPED
        proc = tracked.proc
        try:
            # start_new_session=True => process group id == pid.
            # Signal the whole group so child subprocesses (if any) also die.
            try:
                os_kill_group = True
                import os
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                os_kill_group = False
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGTERM)

            try:
                await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_SECONDS)
                # exited gracefully within grace
            except asyncio.TimeoutError:
                # escalate to SIGKILL
                if os_kill_group:
                    try:
                        import os
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                else:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=PROCESS_KILL_GRACE_SECONDS)
        finally:
            await self._mark_terminal(instance_id, final_status, error=reason if timeout_status else None)
            # Let the waiter finish; remove from tracked.
            async with self._lock:
                self._tracked.pop(instance_id, None)
            if tracked.tailer and not tracked.tailer.done():
                tracked.tailer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracked.tailer
            if tracked.waiter and not tracked.waiter.done():
                tracked.waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tracked.waiter
            if tracked.kill_task and not tracked.kill_task.done():
                tracked.kill_task.cancel()

        log.info("Instance %d stopped (%s)", instance_id, reason)
        return True

    # ------------------------------------------------------------------ #
    # timeout enforcement
    # ------------------------------------------------------------------ #
    async def schedule_timeout(self, instance_id: int, delay: float) -> None:
        task = asyncio.create_task(self._timeout_killer(instance_id, delay))
        async with self._lock:
            tracked = self._tracked.get(instance_id)
            if tracked:
                tracked.kill_task = task

    async def _timeout_killer(self, instance_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        log.warning("Instance %d timed out, killing", instance_id)
        await self._kill(instance_id, reason="timeout", timeout_status=True)

    # ------------------------------------------------------------------ #
    # reaper loop (defensive: reconcile live set with reality)
    # ------------------------------------------------------------------ #
    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the reaper die silently
                log.exception("Reaper error")

    async def _reconcile(self) -> None:
        """Cross-check in-memory tracked set vs DB; clean stragglers.

        Handles the case where a process died between waiter scheduling and
        now, or where the app restarted and stale 'running' rows exist.
        """
        async with self._lock:
            stale_tracked = [
                (iid, t) for iid, t in self._tracked.items() if not t.alive
            ]
        for iid, t in stale_tracked:
            # Let its own waiter finalize it; just drop our reference.
            async with self._lock:
                self._tracked.pop(iid, None)

    # ------------------------------------------------------------------ #
    # DB helpers
    # ------------------------------------------------------------------ #
    async def _mark_terminal(
        self,
        instance_id: int,
        status: str,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> None:
        await db.execute(
            """
            UPDATE instances
               SET status=?, ended_at=datetime('now'),
                   exit_code=COALESCE(?, exit_code),
                   error=?
             WHERE id=?
            """,
            (status, exit_code, error, instance_id),
        )

    # ------------------------------------------------------------------ #
    # introspection (used by admin dashboard)
    # ------------------------------------------------------------------ #
    def live_count(self) -> int:
        return len(self._tracked)

    def live_pids(self) -> list[int]:
        return [t.pid for t in self._tracked.values() if t.pid is not None]


# Singleton
process_manager = ProcessManager()
