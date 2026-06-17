"""Centralized configuration. All values are overridable via environment variables."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WB_DATA_DIR", BASE_DIR / "data"))
BINARIES_DIR = Path(os.getenv("WB_BINARIES_DIR", BASE_DIR / "binaries"))
DATABASE_PATH = Path(os.getenv("WB_DATABASE_PATH", DATA_DIR / "app.db"))

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


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
