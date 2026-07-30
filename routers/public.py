"""Public discovery endpoints (no auth).

  GET /api/config  — advertises how to authenticate so the Android app can
                     configure its Telegram login screen at runtime. Only ever
                     returns the bot *username*, never the token.
"""
from __future__ import annotations

from fastapi import APIRouter

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME

router = APIRouter(prefix="/api", tags=["public"])


@router.get("/config")
async def config():
    """What the app needs to know before it can offer Telegram sign-in."""
    return {
        "telegram_login_enabled": bool(TELEGRAM_BOT_TOKEN),
        "telegram_bot_username": TELEGRAM_BOT_USERNAME,
    }
