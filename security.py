"""Security primitives: password hashing, token generation, auth dependencies.

Uses only stdlib (hashlib.pbkdf2_hmac + secrets) so there are no extra
binary dependencies to compile on a hardened server.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import PBKDF2_ITERATIONS, SESSION_TTL_SECONDS
from db import db

bearer_scheme = HTTPBearer(auto_error=False)

SALT_BYTES = 16
TOKEN_BYTES = 32


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
               u.password_must_change, s.expires_at
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
    }


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
