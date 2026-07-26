"""Security primitives: password hashing, token generation, auth dependencies.

Uses only stdlib (hashlib.pbkdf2_hmac + secrets) so there are no extra
binary dependencies to compile on a hardened server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import PBKDF2_ITERATIONS, SESSION_TTL_SECONDS
from db import db

bearer_scheme = HTTPBearer(auto_error=False)

SALT_BYTES = 16
TOKEN_BYTES = 32


class TelegramAuthError(Exception):
    """Raised when a Telegram WebApp initData string fails validation."""


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(expected, bytes.fromhex(hash_hex))
    except (ValueError, AttributeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


# --------------------------------------------------------------------------- #
# Session storage
# --------------------------------------------------------------------------- #
async def create_session(user_id: int) -> str:
    token = new_token()
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    await db.execute(
        "INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)",
        (token, user_id, expires.isoformat()),
    )
    return token


async def revoke_session(token: str) -> None:
    await db.execute("DELETE FROM sessions WHERE token=?", (token,))


async def get_user_by_token(token: str) -> dict | None:
    row = await db.fetchone(
        """
        SELECT u.id, u.username, u.role, u.max_concurrent, u.enabled,
               u.password_must_change, u.can_create_instances, s.expires_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token = ?
        """,
        (token,),
    )
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if expires < datetime.now(timezone.utc):
        await db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    if not row["enabled"]:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "max_concurrent": row["max_concurrent"],
        "must_change_password": bool(row["password_must_change"]),
        "can_create_instances": bool(row["can_create_instances"]),
    }


# --------------------------------------------------------------------------- #
# Telegram WebApp initData validation
# --------------------------------------------------------------------------- #
def validate_telegram_init_data(init_data: str, bot_token: str, max_age: int) -> dict:
    """Validate a Telegram Mini App ``initData`` string and return the user.

    Implements the official validation algorithm
    (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):

      1. parse ``init_data`` as form-urlencoded fields;
      2. pop ``hash`` (the HMAC we recompute);
      3. reject if ``auth_date`` is missing or older than ``max_age`` seconds
         (replay guard);
      4. build the data-check-string by sorting ``key=value`` pairs (values
         URL-decoded) alphabetically and joining with newlines;
      5. ``secret_key = HMAC_SHA256(b"WebAppData", bot_token)``;
      6. ``expected   = HMAC_SHA256(secret_key, data_check_string).hexdigest()``;
      7. constant-time compare expected vs the received ``hash``;
      8. parse the JSON ``user`` field and return it.

    Raises :class:`TelegramAuthError` on any failure. Uses only the standard
    library (hmac/hashlib/urllib) — no new dependency.
    """
    if not init_data or not bot_token:
        raise TelegramAuthError("missing initData or bot token")

    # parse_qsl decodes URL-encoded values and keeps the last value on dup keys.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise TelegramAuthError("initData missing hash")

    # --- replay guard on auth_date ---
    auth_date_raw = fields.get("auth_date")
    if not auth_date_raw:
        raise TelegramAuthError("initData missing auth_date")
    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError):
        raise TelegramAuthError("initData auth_date is not an integer")
    age = datetime.now(timezone.utc).timestamp() - auth_date
    if age < 0:
        raise TelegramAuthError("initData auth_date is in the future")
    if age > max_age:
        raise TelegramAuthError("initData expired")

    # --- build data-check-string (values must be URL-decoded) ---
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(fields.items())
    )

    # --- recompute the hash and compare ---
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise TelegramAuthError("initData hash mismatch")

    # --- extract the Telegram user object ---
    user_raw = fields.get("user")
    if not user_raw:
        raise TelegramAuthError("initData missing user")
    try:
        tg_user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise TelegramAuthError("initData user is not valid JSON")
    if not isinstance(tg_user, dict) or "id" not in tg_user:
        raise TelegramAuthError("initData user has no id")
    return tg_user


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
async def _resolve_user(
    creds: HTTPAuthorizationCredentials | None,
) -> dict:
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await get_user_by_token(creds.credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    """Any authenticated user (admin or client)."""
    return await _resolve_user(creds)


async def require_admin(
    user: dict = Depends(get_current_user),
) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


async def require_client(
    user: dict = Depends(get_current_user),
) -> dict:
    """Any authenticated, enabled user (clients & admin)."""
    return user


def request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None
