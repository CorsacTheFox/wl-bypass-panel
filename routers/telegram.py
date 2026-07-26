"""Telegram WebApp (Mini App) authorization.

Mirrors routers/auth.py:login but authenticates via the Telegram initData
string that the Mini App SDK injects. On success it issues the same opaque
bearer session token as the username/password path, so the SPA can use the
exact same bootstrap() flow for both.

Disabled (returns 404) when WB_TELEGRAM_BOT_TOKEN is unset — mirroring the
QUICK_TOKEN gating in routers/quick.py.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_INIT_DATA_MAX_AGE
from security import TelegramAuthError, create_session, validate_telegram_init_data
from services import user_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramLoginIn(BaseModel):
    initData: str


@router.post("/telegram")
async def telegram_login(body: TelegramLoginIn):
    if not TELEGRAM_BOT_TOKEN:
        # TMA auth is disabled — indistinguishable from a missing route.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Telegram auth disabled")

    try:
        tg_user = validate_telegram_init_data(
            body.initData, TELEGRAM_BOT_TOKEN, TELEGRAM_INIT_DATA_MAX_AGE
        )
    except TelegramAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    user = await user_service.get_or_create_for_telegram(
        tg_user["id"], tg_user.get("username")
    )
    token = await create_session(user["id"])
    # Same response shape as /api/auth/login so the SPA needs no special path.
    return {
        "token": token,
        "role": user["role"],
        "username": user["username"],
        "must_change_password": False,
    }
