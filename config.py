"""Centralized configuration. All values are overridable via environment variables."""
from __future__ import annotations

import os
from pathlib import Path

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


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
