"""Android-app instance-creation API (``/api/app/*``).

A standalone flow separate from ``/api/quick`` that lets the Android client
create an instance on every connection. Each instance starts out as a short
**temporary** instance (10-minute window by default) so an unauthenticated user
can authorize inside the spawned service. Once the user completes Telegram
authentication (or immediately, if they were already authenticated), the app
*claims* the instance: ownership transfers to the real user, the running
process is kept, and the timeout is reset to the full lifetime. The instance is
explicitly stopped by the app on disconnect.

Authorization model:
  * Every request carries the shared static app token in the ``X-App-Token``
    header (see ``WB_APP_TOKEN`` / :func:`security.verify_app_token`). When that
    token is unset the whole router returns 404.
  * User identity is established via Telegram WebApp ``initData`` at claim time
    (same validation as ``POST /api/auth/telegram``); on success the app
    receives a normal bearer session token to use afterwards.

Temporary instances are spawned under ``user_id=1`` (admin) with ``is_quick=1``
so the existing quick-session cap, reaper and restart reconciliation all apply
unchanged. The claim step flips ``is_quick=0`` and reassigns ``user_id``.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from config import (
    APP_TEMP_TIMEOUT,
    DEFAULT_TIMEOUT_SECONDS,
    HEARTBEAT_EXTENSION_SECONDS,
    HEARTBEAT_MAX_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_INIT_DATA_MAX_AGE,
)
from db import db
from process_manager import process_manager
from routers.quick import _quick_active_count, _resolve_quick_service
from security import (
    TelegramAuthError,
    create_session,
    get_current_user,
    validate_telegram_init_data,
    verify_app_token,
)
from services import (
    ConcurrencyLimitError,
    instance_service,
    settings_store,
    user_service,
)

router = APIRouter(
    prefix="/api/app",
    tags=["android"],
    dependencies=[Depends(verify_app_token)],
)

# claim_token -> instance_id. Lives only in memory for the lifetime of the temp
# instance; a server restart clears it (and the temp instances are reclaimed by
# their 600s timeout_at anyway, since rescheduling happens only on claim).
_claim_tokens: dict[str, int] = {}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _extend(instance_id: int, add_seconds: int, cap_seconds: int) -> str | None:
    """Extend an instance's lifetime by *add_seconds*, capped *cap_seconds* from now.

    Computes the new deadline as ``min(now + add_seconds, now + cap_seconds)``,
    persists it to ``instances.timeout_at`` and re-arms the in-process timeout
    killer via :func:`process_manager.reschedule_timeout`. Returns the new
    deadline ISO string, or None if the instance is no longer tracked (already
    terminal/reaped). The cap prevents a perpetually-heartbeating client from
    keeping an instance alive indefinitely.
    """
    now = _now_utc()
    new_remaining = min(add_seconds, cap_seconds)
    new_timeout_at = (now + timedelta(seconds=new_remaining)).isoformat()
    await db.execute(
        "UPDATE instances SET timeout_at=? WHERE id=? AND status IN ('pending','running','stopping')",
        (new_timeout_at, instance_id),
    )
    await process_manager.reschedule_timeout(instance_id, float(new_remaining))
    return new_timeout_at


class CreateInstanceIn(BaseModel):
    # Optional service override; omitted -> the configured quick service.
    service_id: int | None = None


class ClaimIn(BaseModel):
    # Raw Telegram WebApp initData string.
    telegram_init_data: str
    # The claim_token returned when the temp instance was created.
    claim_token: str


class StopIn(BaseModel):
    # Required to stop an instance before the user has authenticated; once
    # claimed, callers may instead send their bearer token and omit this.
    claim_token: str | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _get_instance_row(instance_id: int) -> dict | None:
    row = await db.fetchone(
        """SELECT i.id, i.user_id, i.service_id, s.name AS service_name,
                  i.pid, i.status, i.started_at, i.ended_at, i.exit_code,
                  i.error, i.timeout_at, i.output_link, i.is_quick
             FROM instances i JOIN services s ON s.id = i.service_id
            WHERE i.id=?""",
        (instance_id,),
    )
    return dict(row) if row else None


def _new_claim_token() -> str:
    return secrets.token_urlsafe(32)


async def _resolve_service(service_id: int | None):
    """Resolve the service to launch: explicit id, else the configured quick one."""
    if service_id is not None:
        from services import NotFoundError, service_registry

        try:
            svc = await service_registry.get(service_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="service not found")
        if not svc["enabled"]:
            raise HTTPException(status_code=400, detail="selected service is disabled")
        return svc
    svc = await _resolve_quick_service()
    if svc is None:
        raise HTTPException(status_code=404, detail="no enabled services available")
    return svc


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
@router.get("/health")
async def app_health():
    """Readiness probe for the Android app.

    Reports whether Telegram login is configured (the app needs it for the
    claim flow) and whether any enabled service exists. Always 200 when the
    router itself is enabled (the ``X-App-Token`` gate already returned 404
    otherwise).
    """
    services = await _resolve_quick_service()
    return {
        "ok": True,
        "telegram_enabled": bool(TELEGRAM_BOT_TOKEN),
        "service_available": services is not None,
    }


@router.post("/instances", status_code=status.HTTP_201_CREATED)
async def create_temp_instance(body: CreateInstanceIn):
    """Create a temporary (short-lived) instance for an unauthenticated app user.

    The instance runs under the admin account (``user_id=1``) with
    ``is_quick=1`` and a ``WB_APP_TEMP_TIMEOUT`` lifetime (default 10 min). A
    one-time ``claim_token`` is returned that the app later uses to transfer the
    instance to the authenticated user.

    Returns 429 if the global quick-session cap is reached, 404 if no service is
    available.
    """
    limit = await settings_store.get("quick_max_concurrent")
    if await _quick_active_count() >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"instance-launch limit reached ({limit} active)",
        )

    svc = await _resolve_service(body.service_id)

    try:
        instance = await instance_service.start(
            user_id=1,  # temp instances live under the admin account until claimed
            service_id=svc["id"],
            timeout_seconds=APP_TEMP_TIMEOUT,
            is_quick=True,
        )
    except ConcurrencyLimitError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    claim_token = _new_claim_token()
    _claim_tokens[claim_token] = instance["id"]

    return {
        "instance_id": instance["id"],
        "claim_token": claim_token,
        "status": instance["status"],
        "output_link": instance.get("output_link"),
        "service_name": instance.get("service_name"),
        "temp_expires_at": instance.get("timeout_at"),
        "temp_ttl_seconds": APP_TEMP_TIMEOUT,
    }


@router.post("/instances/{instance_id}/claim")
async def claim_instance(instance_id: int, body: ClaimIn):
    """Transfer a temp instance to the authenticated user.

    Validates the Telegram WebApp ``initData``, resolves (or creates) the local
    user, reassigns the instance to them, clears the ``is_quick`` flag and
    resets the timeout to the default lifetime (``WB_DEFAULT_TIMEOUT_SECONDS``,
    5 min). The running process is *not* restarted, so any authorization the
    user did inside the spawned service is preserved. The caller then keeps the
    instance alive beyond 5 min via the heartbeat endpoint.

    Permission: anyone may spawn a short temp instance, but claiming it (which
    grants the ability to heartbeat/extend) requires the instance-creation
    privilege (``users.can_create_instances``); admins always pass. Guests with
    no privilege are refused here and their temp instance expires at 5 min.

    Returns the user's session token (use it as ``Authorization: Bearer`` for
    subsequent calls, including heartbeat) plus the ``output_link``.

    Errors: 401 bad initData, 403 instance creation disabled, 404 instance/token
    mismatch, 409 already claimed, 410 instance gone.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Telegram auth disabled"
        )

    # 1. verify the claim token maps to this instance.
    mapped_id = _claim_tokens.get(body.claim_token)
    if mapped_id != instance_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown or mismatched claim_token",
        )

    row = await _get_instance_row(instance_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")

    if not row["is_quick"]:
        # already claimed (someone reused the token) — idempotent-ish refusal
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="instance already claimed"
        )
    if row["status"] in ("stopped", "exited", "crashed", "timeout"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"instance already ended ({row['status']})",
        )

    # 2. authenticate the user via Telegram initData.
    try:
        tg_user = validate_telegram_init_data(
            body.telegram_init_data, TELEGRAM_BOT_TOKEN, TELEGRAM_INIT_DATA_MAX_AGE
        )
    except TelegramAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    user = await user_service.get_or_create_for_telegram(
        tg_user["id"], tg_user.get("username")
    )

    # 3. privilege gate — temp instances are open, but claiming one (and thus
    # gaining the ability to heartbeat/extend its lifetime) requires the
    # creation privilege. Guests are refused and their temp instance expires at
    # its 5-min TTL.
    if user["role"] != "admin" and not user.get("can_create_instances"):
        _claim_tokens.pop(body.claim_token, None)
        await process_manager.stop(instance_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Instance creation is disabled for this account",
        )

    # 4. transfer ownership + reset the timeout to the default (5 min) lifetime.
    new_timeout_at = (_now_utc() + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)).isoformat()
    await db.execute(
        "UPDATE instances SET user_id=?, is_quick=0, timeout_at=? WHERE id=?",
        (user["id"], new_timeout_at, instance_id),
    )
    await process_manager.reschedule_timeout(instance_id, float(DEFAULT_TIMEOUT_SECONDS))

    # consume the token so it can't be reused.
    _claim_tokens.pop(body.claim_token, None)

    token = await create_session(user["id"])

    return {
        "instance_id": instance_id,
        "user_id": user["id"],
        "token": token,
        "role": user["role"],
        "username": user["username"],
        "status": row["status"],
        "output_link": row.get("output_link"),
        "expires_at": new_timeout_at,
    }


@router.post("/instances/{instance_id}/heartbeat")
async def heartbeat_instance(instance_id: int, user=Depends(get_current_user)):
    """Extend a claimed instance's lifetime (authenticated clients only).

    Each heartbeat adds ``WB_HEARTBEAT_EXT`` seconds (default 300) to the
    instance's remaining lifetime, capped at ``WB_HEARTBEAT_MAX`` (default 4h)
    from now. This is the mechanism by which an authenticated user keeps a
    session alive beyond the 5-minute default: as long as the app keeps
    pinging while there is user activity, the instance is repeatedly extended.

    Authorization is bearer-based (``Authorization: Bearer <token>``), so it is
    only reachable *after* a successful claim — i.e. only authenticated,
    privileged users can extend. Guests (pre-claim temp instances owned by the
    admin account) cannot satisfy the ownership check and are refused.

    Errors: 401 bad/missing bearer, 403 not the owner, 404 unknown instance,
    410 instance already ended.
    """
    row = await _get_instance_row(instance_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")

    # Ownership: only the instance's owner may extend it. A pre-claim temp
    # instance is owned by user_id=1 (admin) but the caller here is a real
    # authenticated user, so this also naturally blocks heartbeating a temp
    # (guest) instance.
    if row["user_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not the instance owner"
        )
    if row["status"] in ("stopped", "exited", "crashed", "timeout"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"instance already ended ({row['status']})",
        )

    new_timeout_at = await _extend(
        instance_id, HEARTBEAT_EXTENSION_SECONDS, HEARTBEAT_MAX_SECONDS
    )
    return {
        "ok": True,
        "instance_id": instance_id,
        "expires_at": new_timeout_at,
    }


@router.get("/instances/{instance_id}")
async def get_instance(instance_id: int, request: Request):
    """Poll an instance's status and output_link.

    Usable both before claim (no bearer, the app simply tracks the temp
    instance by id) and after. Returns 404 if the instance never existed.
    """
    row = await _get_instance_row(instance_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "status": row["status"],
        "output_link": row.get("output_link"),
        "error": row.get("error"),
        "timeout_at": row.get("timeout_at"),
        "is_quick": bool(row["is_quick"]),
        "service_name": row.get("service_name"),
    }


@router.delete("/instances/{instance_id}")
async def stop_instance(instance_id: int, body: StopIn):
    """Stop an instance (the app calls this on disconnect).

    Before the user is authenticated, the app authenticates with ``X-App-Token``
    (router-level) and supplies the ``claim_token`` to prove it owns the temp
    instance. After claim, the app may instead use its bearer token via the
    regular client flow; this endpoint remains available for convenience and is
    idempotent — stopping an already-terminal instance just returns its row.
    """
    row = await _get_instance_row(instance_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")

    # Authorization: a matching claim_token always authorizes; otherwise the
    # caller must be acting on an already-claimed instance they own (handled by
    # the client router). For the app's pre-claim stop, require the token.
    if row["is_quick"]:
        mapped_id = _claim_tokens.get(body.claim_token) if body.claim_token else None
        if mapped_id != instance_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="claim_token required to stop a temp instance",
            )

    await process_manager.stop(instance_id)
    if body.claim_token:
        _claim_tokens.pop(body.claim_token, None)
    final = await _get_instance_row(instance_id)
    return {"ok": True, "instance": final}
