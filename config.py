"""Centralized configuration. All values are overridable via environment variables."""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("config")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WB_DATA_DIR", BASE_DIR / "data"))
BINARIES_DIR = Path(os.getenv("WB_BINARIES_DIR", BASE_DIR / "binaries"))
DATABASE_PATH = Path(os.getenv("WB_DATABASE_PATH", DATA_DIR / "app.db"))
# Uploaded cookies-*.json live here (extracted from admin-uploaded zip files).
# A service references one of them by path in its `credentials` field.
COOKIES_DIR = Path(os.getenv("WB_COOKIES_DIR", DATA_DIR / "cookies"))
# Per-instance binary stdout/stderr logs (one file per instance lifecycle).
LOGS_DIR = Path(os.getenv("WB_LOGS_DIR", DATA_DIR / "logs"))

# Flag passed to the binary to point it at its cookies file. All current
# creator binaries use `-cookies <path>`; if a future build differs, override
# per-service via extra_args (the builder never duplicates it).
COOKIE_FLAG = os.getenv("WB_COOKIE_FLAG", "-cookies")
# Tail of stderr stored in instances.error when a binary crashes — keeps the
# DB row readable while the full log lives on disk.
ERROR_TAIL_BYTES = int(os.getenv("WB_ERROR_TAIL_BYTES", "8192"))

# Bootstrap admin (only created on first run if no admin exists yet).
ADMIN_USERNAME = os.getenv("WB_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("WB_ADMIN_PASSWORD", "changeme-on-first-login")

HOST = os.getenv("WB_HOST", "127.0.0.1")
PORT = int(os.getenv("WB_PORT", "8000"))

# Process / business policy
DEFAULT_MAX_CONCURRENT = int(os.getenv("WB_DEFAULT_MAX_CONCURRENT", "3"))
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("WB_DEFAULT_TIMEOUT_SECONDS", "3600"))  # 1h
PROCESS_KILL_GRACE_SECONDS = float(os.getenv("WB_KILL_GRACE_SECONDS", "5"))
REAPER_INTERVAL_SECONDS = float(os.getenv("WB_REAPER_INTERVAL", "2"))

SESSION_TTL_SECONDS = int(os.getenv("WB_SESSION_TTL", str(12 * 3600)))
PBKDF2_ITERATIONS = 200_000

# Quick-launch page: token to authorize unauthenticated instance creation.
# When empty (default), the /quick route is disabled.
QUICK_TOKEN = os.getenv("WB_QUICK_TOKEN", "")
# Maximum number of simultaneous quick-launch instances (across all callers).
QUICK_MAX_CONCURRENT = int(os.getenv("WB_QUICK_MAX_CONCURRENT", "5"))
# Default service id used by the public /quick flow when no admin override is
# set. 0 = use the first enabled service.
QUICK_DEFAULT_SERVICE_ID = int(os.getenv("WB_QUICK_DEFAULT_SERVICE_ID", "0"))

# Telegram Mini App (WebApp) authorization.
# Set to the bot token obtained from BotFather to enable Telegram login.
# When empty (default), POST /api/auth/telegram is disabled (returns 404),
# mirroring the QUICK_TOKEN gating pattern.
TELEGRAM_BOT_TOKEN = os.getenv("WB_TELEGRAM_BOT_TOKEN", "")
# Maximum age (in seconds) of a Telegram initData before it is rejected as a
# replay. Telegram recommends keeping this short; 24h matches the session TTL.
TELEGRAM_INIT_DATA_MAX_AGE = int(os.getenv("WB_TG_INITDATA_MAX_AGE", str(24 * 3600)))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def generate_proxychains_conf(conf_path: Path, proxy_type: str,
                              proxy_host: str, proxy_port: str) -> None:
    """Write a proxychains4 config file to *conf_path*."""
    conf = (
        "strict_chain\n"
        "proxy_dns\n"
        "remote_dns_subnet 224\n"
        "tcp_read_time_out 15000\n"
        "tcp_connect_time_out 8000\n"
        "\n"
        "[ProxyList]\n"
        f"{proxy_type} {proxy_host} {proxy_port}\n"
    )
    conf_path.write_text(conf, encoding="utf-8")
