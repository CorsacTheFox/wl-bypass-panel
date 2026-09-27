"""Android-app instance-creation API (``/api/app/*``).

A standalone flow separate from ``/api/quick`` that lets the Android client
create an instance on every connection. Each instance starts out as a short
**temporary** instance (5-minute window by default) so an unauthenticated user
can authorize inside the spawned service. Once the user completes Telegram
authentication (or immediately, if they were already authenticated), the app
*claims* the instance: ownership transfers to the real user, the running
process is kept, and the timeout is reset to a 5-minute default. The instance is
explicitly stopped by the app on disconnect.

Instance-control policy (the whole point of this router):
  * **Unauthorized users** (no Telegram auth, or ``can_create_instances`` off)
    are strictly limited to a 5-minute lifetime with **no extension** — the
    heartbeat endpoint rejects them with 403. This prevents the long-lived /
    "1-hour" instances that previously accumulated.
  * **Authorized users** (admin, or ``can_create_instances`` on) keep a session
    alive via the heartbeat endpoint, which slides the 5-minute window forward,
    capped at ``WB_HEARTBEAT_MAX`` (default 1h).
  * **Reconnect reuse:** when the app connects with valid Telegram initData and
    the user already has a still-live instance, that instance is returned as-is
    instead of spawning a duplicate (see ``POST /instances`` and
    ``InstanceService.find_active_app_session`` / the ``app_session`` column).

Authorization model:
  * Every request carries the shared static app token in the ``X-App-Token``
    header (see ``WB_APP_TOKEN`` / :func:`security.verify_app_token`). When that
    token is unset the whole router returns 404.
  * User identity is established either by a **Remnawave subscription link**
    (``POST /api/app/auth/link`` — the primary flow for current APKs) or, as a
    legacy fallback for old APKs, via Telegram WebApp ``initData`` at claim
    time; on success the app receives a normal bearer session token to use
    afterwards. ``POST /instances`` and ``/claim`` accept either credential.

Temporary instances are spawned under ``user_id=1`` (admin) with ``is_quick=1``
so the existing quick-session cap, reaper and restart reconciliation all apply
unchanged. The claim step flips ``is_quick=0`` and reassigns ``user_id``.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
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
from fastapi.security import HTTPAuthorizationCredentials
from process_manager import process_manager
from vk_relay import vk_relay
from remnawave import (
    RemnawaveError,
    get_link_auth_squads,
    get_sync_options,
    is_configured,
    remnawave_service,
)
from routers.quick import _quick_active_count, _resolve_quick_service
from security import (
    TelegramAuthError,
    bearer_scheme,
    create_session,
    get_current_user,
    get_user_by_token,
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
# their APP_TEMP_TIMEOUT (300s) timeout_at anyway, since rescheduling happens
# only on claim).
_claim_tokens: dict[str, int] = {}


async def optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict | None:
    """The bearer-identified user when the app presents a session token,
    otherwise None.

    A present-but-invalid bearer still yields 401 (the client believes it is
    signed in — silently downgrading it to the anonymous temp flow would be
    wrong). Used by the link-auth identity path of ``POST /instances`` and
    ``/claim``.
    """
    if creds is None or not creds.credentials:
        return None
    user = await get_user_by_token(creds.credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    return user


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
    # Optional Telegram WebApp initData (legacy auth path, old APKs). When the
    # app already has a valid Telegram session it sends this on connect so the
    # server can REUSE the user's still-live instance (instead of spawning a
    # duplicate) or, if none exists, create + claim on their behalf. Omitted ->
    # the unauthenticated 5-minute temp flow (no extension possible), unless a
    # valid bearer session token is supplied (link-auth path).
    telegram_init_data: str | None = None


class ClaimIn(BaseModel):
    # Raw Telegram WebApp initData string (legacy auth path, old APKs).
    # Newer clients authenticate with a bearer session token instead
    # (Authorization header) and omit this.
    telegram_init_data: str | None = None
    # The claim_token returned when the temp instance was created.
    claim_token: str


class ByLinkIn(BaseModel):
    # The call link the app read from the VK relay dialog.
    output_link: str


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
                  i.error, i.timeout_at, i.output_link, i.is_quick, i.app_session
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


def _authenticate_telegram(telegram_init_data: str) -> dict:
    """Validate Telegram WebApp initData and return the parsed user fields.

    Raises HTTPException(401) on a bad/expired signature. Centralized here so
    both the create (reuse) path and the claim path share identical validation.
    """
    try:
        return validate_telegram_init_data(
            telegram_init_data, TELEGRAM_BOT_TOKEN, TELEGRAM_INIT_DATA_MAX_AGE
        )
    except TelegramAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


class LinkAuthIn(BaseModel):
    # The user's Remnawave subscription link or its bare token, e.g.
    # https://<panel>/sub/<token>  or just <token>.
    subscription: str


def _extract_subscription_token(subscription: str) -> str:
    """Pull the token out of a subscription URL (or pass a bare token through).

    Accepts the shapes Remnawave hands out: ``https://<panel>/sub/<token>``,
    ``https://<panel>/c/<token>`` (clash) and a bare ``<token>``.
    """
    value = (subscription or "").strip()
    if "://" in value:
        from urllib.parse import urlparse

        path = urlparse(value).path.rstrip("/")
        segments = [s for s in path.split("/") if s]
        if segments:
            return segments[-1]
        return ""
    return value


async def _resolve_link_user(subscription: str) -> dict:
    """Resolve a subscription link to a local user, creating one on the fly.

    The token is the subscription-link ``shortUuid``; it is resolved against the
    panel directly (``GET /api/users/by-short-uuid`` in remnawave.py). A panel
    user not yet imported is auto-created locally with the same parameters the
    sync would use, so sign-in never waits for the next sync cycle.
    """
    if not await is_configured():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription sign-in is not configured (no Remnawave panel)",
        )

    token = _extract_subscription_token(subscription)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subscription is empty or has no token",
        )

    try:
        panel_user = await remnawave_service.find_user_by_subscription(token)
    except RemnawaveError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    if panel_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Subscription not found — check the link or contact support",
        )
    # Squad ALLOW-list (Admin → Remnawave → App sign-in squads): only the
    # checked squads may sign in via the app; empty = all squads allowed.
    # A user in several squads passes when ANY of their squads is checked.
    allowed_squads = await get_link_auth_squads()
    if allowed_squads:
        user_squads = set(panel_user.get("squads") or [panel_user.get("squad")])
        if not (user_squads & set(allowed_squads)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your squad is not allowed to sign in via the app",
            )
    if str(panel_user.get("status") or "").upper() != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Subscription is expired or disabled",
        )

    ref = panel_user["ref"]

    # 1. already imported
    existing = await user_service.find_by_external_ref(ref)
    if existing:
        return await user_service.get(existing["id"])

    # 1b. imported before Remnawave 3.0, when external_ref held the old panel
    #     UUID: re-bind that same account to the v3 shortUuid instead of
    #     creating a duplicate. Only touches panel-sourced rows.
    rebound = await user_service.rebind_external_ref(
        ref,
        telegram_id=panel_user.get("telegram_id"),
        username=panel_user.get("username") or None,
    )
    if rebound:
        return rebound

    # 2. create on the fly, mirroring the sync's parameters (grant flag,
    #    concurrency cap) so an auto-created user matches an imported one.
    opts = await get_sync_options()
    username = panel_user.get("username") or (
        f"user_{panel_user['telegram_id']}" if panel_user.get("telegram_id") else ""
    )
    for candidate in (username, f"remna_{ref[:8]}"):
        if not candidate:
            continue
        try:
            return await user_service.create_client(
                username=candidate,
                password=None,
                max_concurrent=opts["max_concurrent"],
                external_ref=ref,
                telegram_id=panel_user.get("telegram_id"),
                can_create_instances=bool(opts["grant_create_instances"]),
                must_change_password=False,
            )
        except ValueError:
            continue  # username clash (locally-managed account) — next candidate
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not create a local account for this subscription — contact support",
    )


@router.post("/auth/link")
async def link_auth(body: LinkAuthIn):
    """Sign the Android app in with a Remnawave subscription link.

    Replaces the Telegram-bot auth for the app: the subscription token is the
    user's existing secret (it already grants their VPN config), so proving
    possession of it is sufficient identity. Returns the same shape as
    POST /api/auth/login so the client reuses its login plumbing.
    """
    user = await _resolve_link_user(body.subscription)
    token = await create_session(user["id"])
    return {
        "token": token,
        "role": user["role"],
        "username": user["username"],
        "must_change_password": False,
    }


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
@router.get("/health")
async def app_health():
    """Readiness probe for the Android app.

    Reports which auth backends are usable (Telegram initData and/or the
    Remnawave subscription link-auth) and whether any enabled service exists.
    Always 200 when the router itself is enabled (the ``X-App-Token`` gate
    already returned 404 otherwise).
    """
    services = await _resolve_quick_service()
    return {
        "ok": True,
        "telegram_enabled": bool(TELEGRAM_BOT_TOKEN),
        "link_auth": await is_configured(),
        "service_available": services is not None,
    }


@router.post("/instances", status_code=status.HTTP_201_CREATED)
async def create_temp_instance(
    body: CreateInstanceIn, session_user: dict | None = Depends(optional_user)
):
    """Create an instance for the Android app, reusing an existing one if possible.

    Three modes, by how the caller authenticates:

    * **Legacy initData (old APKs):** the user is resolved via Telegram. If
      they already have a still-live app instance, it is **returned as-is**
      (HTTP 200, ``reused=true``) — no new process is spawned, preventing the
      duplicate-instance pile-up on reconnect. Otherwise a new temp instance is
      created and *immediately claimed* on the user's behalf (HTTP 201,
      ``reused=false``), stamped with their Telegram id so the next reconnect
      reuses it. The 5-minute default lifetime applies; authorized users extend
      it via the heartbeat endpoint.

    * **Bearer session (link-auth, new APKs):** same reuse/claim behaviour for
      the user identified by ``Authorization: Bearer`` (see
      ``POST /api/app/auth/link``).

    * **Unauthenticated (neither):** a 5-minute temp instance is created under
      ``user_id=1`` with a one-time ``claim_token``. The caller later completes
      auth and calls ``/claim`` to transfer it. Unauthorized users (no
      ``can_create_instances``) can never extend beyond these 5 min.

    Returns 429 if the global quick-session cap is reached, 404 if no service is
    available, 401 on bad initData / bad bearer.
    """
    # --- Identity: legacy initData or bearer session -------------------------
    user: dict | None = None
    tag: str | None = None  # app_session tag; None disables instance reuse
    if body.telegram_init_data and TELEGRAM_BOT_TOKEN:
        tg_user = _authenticate_telegram(body.telegram_init_data)
        user = await user_service.get_or_create_for_telegram(
            tg_user["id"], tg_user.get("username")
        )
        tag = str(tg_user["id"])
    elif session_user is not None:
        user = session_user
        tag = str(session_user["telegram_id"]) if session_user.get("telegram_id") else None

    # --- Authenticated path: reuse an existing live instance if there is one --
    if user is not None:
        existing = None
        if tag is not None:
            existing = await instance_service.find_active_app_session(int(tag))
        if existing is not None:
            # Reuse: no new process, no new row. Issue a fresh session token so
            # the client can keep heartbeating. The instance keeps its current
            # timeout_at (heartbeats slide it forward as the client stays active).
            token = await create_session(user["id"])
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "instance_id": existing["id"],
                    "claim_token": None,
                    "status": existing["status"],
                    "output_link": existing.get("output_link"),
                    "service_name": None,
                    "temp_expires_at": existing.get("timeout_at"),
                    "temp_ttl_seconds": None,
                    "reused": True,
                    "token": token,
                    "user_id": user["id"],
                    "username": user["username"],
                },
            )
        # No live instance — fall through to create one, then claim it below.

    # --- Shared cap + create -------------------------------------------------
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

    instance_id = instance["id"]

    # --- Authenticated create: claim immediately on the caller's behalf ------
    if user is not None:
        if user["role"] != "admin" and not user.get("can_create_instances"):
            # Guest: keep the 5-min temp running so they can still try the
            # manual claim flow / use it briefly, but don't pre-claim. Their
            # instance is strictly limited to 5 min (heartbeat will 403).
            claim_token = _new_claim_token()
            _claim_tokens[claim_token] = instance_id
            return {
                "instance_id": instance_id,
                "claim_token": claim_token,
                "status": instance["status"],
                "output_link": instance.get("output_link"),
                "service_name": instance.get("service_name"),
                "temp_expires_at": instance.get("timeout_at"),
                "temp_ttl_seconds": APP_TEMP_TIMEOUT,
                "reused": False,
            }
        # Authorized: transfer ownership + reset timeout to the default
        # lifetime, and stamp app_session so reconnects reuse this row.
        new_timeout_at = (_now_utc() + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)).isoformat()
        await instance_service.claim_to_user(instance_id, user["id"], new_timeout_at, tag)
        await process_manager.reschedule_timeout(instance_id, float(DEFAULT_TIMEOUT_SECONDS))
        token = await create_session(user["id"])
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "instance_id": instance_id,
                "claim_token": None,
                "status": instance["status"],
                "output_link": instance.get("output_link"),
                "service_name": instance.get("service_name"),
                "temp_expires_at": new_timeout_at,
                "temp_ttl_seconds": DEFAULT_TIMEOUT_SECONDS,
                "reused": False,
                "token": token,
                "user_id": user["id"],
                "username": user["username"],
            },
        )

    # --- Unauthenticated create: classic temp + claim_token -----------------
    claim_token = _new_claim_token()
    _claim_tokens[claim_token] = instance_id

    return {
        "instance_id": instance_id,
        "claim_token": claim_token,
        "status": instance["status"],
        "output_link": instance.get("output_link"),
        "service_name": instance.get("service_name"),
        "temp_expires_at": instance.get("timeout_at"),
        "temp_ttl_seconds": APP_TEMP_TIMEOUT,
        "reused": False,
    }


@router.post("/instances/by-link")
async def instance_by_link(body: ByLinkIn):
    """Resolve a VK-relayed link to its instance so the app can claim it.

    In VK-relay mode the app only has the bare call link. Once it is connected
    through that call it reaches this API over the tunnel, looks the instance
    up here, then uses the ordinary ``/claim`` + ``/heartbeat`` flow. Only
    still-unclaimed relay instances resolve; everything else is 404, so this
    can't be used to grab a user's already-claimed call.

    Returns ``instance_id`` and a fresh one-time ``claim_token``.
    """
    link = body.output_link.strip()
    row = await db.fetchone(
        """SELECT id, status, timeout_at FROM instances
            WHERE output_link=? AND user_id=1 AND is_quick=1
              AND status IN ('pending','running')
            ORDER BY id DESC LIMIT 1""",
        (link,),
    )
    if row is None or not vk_relay.is_relayed(row["id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="instance not found")
    instance_id = row["id"]
    # Drop any token minted earlier for this instance: one live claim token.
    for tok, iid in list(_claim_tokens.items()):
        if iid == instance_id:
            _claim_tokens.pop(tok, None)
    claim_token = _new_claim_token()
    _claim_tokens[claim_token] = instance_id
    return {
        "instance_id": instance_id,
        "claim_token": claim_token,
        # Required by the app's CreateInstanceOut parser (getString).
        "status": row["status"],
        "output_link": link,
        "temp_expires_at": row["timeout_at"],
    }


@router.post("/instances/{instance_id}/claim")
async def claim_instance(
    instance_id: int, body: ClaimIn, session_user: dict | None = Depends(optional_user)
):
    """Transfer a temp instance to the authenticated user.

    Authenticates via a Telegram WebApp ``initData`` (legacy, old APKs) OR a
    bearer session token (link-auth, ``POST /api/app/auth/link``); when both
    are present the initData wins. Resolves (or creates) the local user,
    reassigns the instance to them, clears the ``is_quick`` flag and resets the
    timeout to the default lifetime (``WB_DEFAULT_TIMEOUT_SECONDS``, 5 min).
    The running process is *not* restarted, so any authorization the user did
    inside the spawned service is preserved. The caller then keeps the instance
    alive beyond 5 min via the heartbeat endpoint.

    Permission: anyone may spawn a short temp instance, but claiming it (which
    grants the ability to heartbeat/extend) requires the instance-creation
    privilege (``users.can_create_instances``); admins always pass. Guests with
    no privilege are refused here and their temp instance expires at 5 min.

    Returns the user's session token (use it as ``Authorization: Bearer`` for
    subsequent calls, including heartbeat) plus the ``output_link``.

    Errors: 401 bad initData / no credentials, 403 instance creation disabled,
    404 instance/token mismatch, 409 already claimed, 410 instance gone.
    """
    if not TELEGRAM_BOT_TOKEN and session_user is None:
        # No bot token AND no bearer: claim is only "disabled" when no auth
        # backend exists at all; with link-auth configured this is simply a
        # missing-credential client error.
        if not await is_configured():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="claim auth disabled"
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no credentials: supply telegram_init_data or a bearer token",
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

    # 2. authenticate the user: initData (legacy) or bearer session (link-auth).
    if body.telegram_init_data and TELEGRAM_BOT_TOKEN:
        tg_user = _authenticate_telegram(body.telegram_init_data)
        user = await user_service.get_or_create_for_telegram(
            tg_user["id"], tg_user.get("username")
        )
        tag: str | None = str(tg_user["id"])
    elif session_user is not None:
        user = session_user
        tag = str(session_user["telegram_id"]) if session_user.get("telegram_id") else None
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="no credentials: supply telegram_init_data or a bearer token",
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

    # 3b. dedup: if this user already owns a *different* live app instance
    # (e.g. they claimed a temp, then started another and are claiming this one
    # too), stop the older one so there is at most one live app instance per
    # user. The current instance (instance_id) is the keeper. Lookup is by the
    # app_session tag; users without a telegram id have no tag and skip this.
    if tag is not None:
        prior = await instance_service.find_active_app_session(int(tag))
        if prior is not None and prior["id"] != instance_id:
            await process_manager.stop(prior["id"])

    # 4. transfer ownership + reset the timeout to the default (5 min) lifetime.
    new_timeout_at = (_now_utc() + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)).isoformat()
    await instance_service.claim_to_user(instance_id, user["id"], new_timeout_at, tag)
    await process_manager.reschedule_timeout(instance_id, float(DEFAULT_TIMEOUT_SECONDS))
    # A claimed VK-relay call must stop being the link handed out via VK.
    vk_relay.on_claimed(instance_id)

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
    """Extend a claimed instance's lifetime (authorized clients only).

    Each heartbeat adds ``WB_HEARTBEAT_EXT`` seconds (default 300) to the
    instance's remaining lifetime, capped at ``WB_HEARTBEAT_MAX`` (default 1h)
    from now. This is the mechanism by which an authorized user keeps a session
    alive beyond the 5-minute default: as long as the app keeps pinging, the
    instance is repeatedly extended.

    Authorization is two-layered:
      * Bearer-based (``Authorization: Bearer <token>``) — so only reachable
        *after* a successful claim.
      * **Privilege gate:** only admins and users with ``can_create_instances``
        may extend. Unauthorized users get 403 and their instance is strictly
        limited to the 5-minute TTL set at creation/claim — they cannot extend.

    Guests (pre-claim temp instances owned by the admin account) also fail the
    ownership check below.

    Errors: 401 bad/missing bearer, 403 not the owner / not allowed to extend,
    404 unknown instance, 410 instance already ended.
    """
    # Privilege gate: only authorized users may extend a session. Unauthorized
    # users are hard-capped at the 5-minute TTL — no extension path exists.
    if user["role"] != "admin" and not user.get("can_create_instances"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Instance limited to 5 minutes; extension not allowed",
        )

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
