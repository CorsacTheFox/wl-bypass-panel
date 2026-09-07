#!/usr/bin/env bash
# =============================================================================
# install.sh — one-shot installer for the Whitelist-Bypass Instance Manager.
#
# Idempotent: safe to re-run. Designed for Ubuntu 22.04 / 24.04 (and Debian 12).
#
# What it does:
#   1. Asks configuration questions (each overridable via env var)
#   2. Installs system packages (python, sqlite3, ufw; nginx OR caddy)
#   3. Creates a dedicated, unprivileged service user + install dir
#   4. Copies the app, builds the venv, installs Python deps
#   5. Seeds .env (from .env.example), inits the DB
#   6. Forces the admin password to match .env (every run — see note below)
#   7. Installs + enables + starts the systemd service (wb-manager.service)
#   8. Installs the reverse-proxy config (nginx OR caddy), reloads it
#   9. Opens firewall ports (80/443 with a domain, else PUBLIC_PORT)
#  10. Optionally issues a Let's Encrypt TLS certificate (nginx path only;
#      caddy obtains TLS automatically)
#
# Usage (as root, or with sudo):
#   sudo bash deploy/install.sh                          # interactive
#   sudo PROXY=caddy DOMAIN=wb.example.com bash deploy/install.sh   # env-driven
#   sudo PROXY=nginx DOMAIN=wb.example.com EMAIL=you@x.com bash deploy/install.sh
#
# All prompts have an env-var default, so the install is fully scriptable:
#   PROXY         nginx | caddy          (default nginx)
#   DOMAIN        FQDN for TLS           (empty = no TLS, HTTP on PUBLIC_PORT)
#   EMAIL         Let's Encrypt email    (required if DOMAIN set and PROXY=nginx)
#   APP_HOST      uvicorn bind host      (default 127.0.0.1)
#   APP_PORT      uvicorn bind port      (default 8000)  — internal, behind proxy
#   PUBLIC_PORT   external HTTP port     (default 80)    — only when no DOMAIN
#   ADMIN_USERNAME                        (default admin)
#   ADMIN_PASSWORD                        (default: auto-generated, alphanumeric)
#   APP_DIR       install path           (default /opt/whitelist-manager)
#   SERVICE_USER  unprivileged user      (default wb-manager)
#   RECONFIGURE=1 on an update, force the config prompts (default: keep as-is)
#   DEBUG=1       enable set -x tracing
#
# On an UPDATE the installer asks a single yes/no question ("Reconfigure
# settings?"). Answer N (default) and nothing in .env / the proxy config / the
# admin password is touched — only new .env.example keys are appended and the
# manager process is restarted. Answer y (or pass RECONFIGURE=1) to walk the
# full prompt list; even then only values you actually change are written.
#
# NOTE on updates (re-run over an existing $APP_DIR) — the update is now
# NON-DESTRUCTIVE:
#   * SQLite database (data/) is PRESERVED and backed up to /var/backups/ first.
#     Schema changes are applied in-process by the app's own migrations.
#   * .env is PRESERVED. Existing keys are NOT touched unless the operator
#     explicitly passes a new value for that key (env var, or a changed answer
#     to an interactive prompt). Brand-new keys shipped in .env.example by this
#     release are ADDED with their template default; nothing is ever removed.
#   * The admin password is KEPT. It is only re-synced to the DB when the
#     operator explicitly supplies a new ADMIN_PASSWORD.
#   * RUNNING PROXY INSTANCES SURVIVE THE UPDATE. Child binaries are not killed;
#     only the uvicorn main process is restarted (KillMode=process) and the app
#     re-adopts the still-alive PIDs on startup (see main.py reattach()).
#   * Uploaded binaries/ are PRESERVED.
#
# A fresh install (no $APP_DIR) still creates the bootstrap admin and, if no
# ADMIN_PASSWORD was given, auto-generates one.
# =============================================================================
set -euo pipefail

# ---- logging helpers ----
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# Optional debug tracing.
[[ "${DEBUG:-0}" == "1" ]] && set -x

# #############################################################################
# Preflight
# #############################################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../deploy
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                       # the project root

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
[[ -f "$SRC_DIR/main.py" ]] || die "Could not find app at $SRC_DIR/main.py"

# Core paths (needed early for update detection).
APP_DIR="${APP_DIR:-/opt/whitelist-manager}"
SERVICE_USER="${SERVICE_USER:-wb-manager}"
SERVICE_NAME="wb-manager"

# Detect whether we can prompt interactively.
INTERACTIVE=0
if [[ -t 0 ]] && [[ -t 1 ]]; then INTERACTIVE=1; fi

# Detect existing installation (update vs fresh install).
IS_UPDATE=false
if [[ -f "$APP_DIR/main.py" ]]; then
    IS_UPDATE=true
fi
MODE_LABEL="INSTALL"

# prompt <var> <message> <default>
# Sets the var to user input or the default (the default already reflects any
# env-var override the caller set, so this single helper covers both modes).
prompt() {
    local var="$1" msg="$2" def="${3:-}"
    local val
    if [[ "$INTERACTIVE" -eq 1 ]]; then
        if [[ -n "$def" ]]; then
            read -r -p "$msg [$def]: " val
            val="${val:-$def}"
        else
            read -r -p "$msg: " val
        fi
    else
        val="$def"
        [[ -n "$val" ]] || die "Non-interactive shell: $var must be set via env."
    fi
    printf -v "$var" '%s' "$val"
}

# #############################################################################
# 1. Configuration questions (every value has an env default)
# #############################################################################
# On an update, default PROXY to whatever is already serving this install so a
# press-Enter update does not switch nginx <-> caddy underneath the operator.
_DETECTED_PROXY=""
_DETECTED_DOMAIN=""
_DETECTED_PUBLIC_PORT=""
if $IS_UPDATE; then
    NGINX_SITE_FILE="/etc/nginx/sites-available/${SERVICE_NAME}"
    if [[ -f /etc/nginx/sites-enabled/${SERVICE_NAME} ]]; then
        _DETECTED_PROXY="nginx"
        # server_name (skip the "_" catch-all) and the first listen port.
        _DETECTED_DOMAIN="$(grep -hoE 'server_name[[:space:]]+[^;]+' "$NGINX_SITE_FILE" 2>/dev/null \
            | awk '{print $2}' | grep -v '^_$' | head -1 || true)"
        _DETECTED_PUBLIC_PORT="$(grep -hoE 'listen[[:space:]]+[0-9]+' "$NGINX_SITE_FILE" 2>/dev/null \
            | awk '{print $2}' | head -1 || true)"
    elif [[ -f /etc/caddy/Caddyfile ]] && grep -q 'whitelist-manager install.sh' /etc/caddy/Caddyfile 2>/dev/null; then
        _DETECTED_PROXY="caddy"
        # First site label: "example.com {" => domain, ":80 {" => bare port.
        _CADDY_LABEL="$(grep -oE '^[^#[:space:]]+[[:space:]]*\{' /etc/caddy/Caddyfile 2>/dev/null | head -1 | sed 's/[[:space:]]*{.*//' || true)"
        if [[ "$_CADDY_LABEL" == :* ]]; then
            _DETECTED_PUBLIC_PORT="${_CADDY_LABEL#:}"
        elif [[ -n "$_CADDY_LABEL" ]]; then
            _DETECTED_DOMAIN="$_CADDY_LABEL"
        fi
    fi
fi
PROXY="${PROXY:-${_DETECTED_PROXY:-nginx}}"
DOMAIN="${DOMAIN:-${_DETECTED_DOMAIN:-}}"
EMAIL="${EMAIL:-}"
PUBLIC_PORT="${PUBLIC_PORT:-${_DETECTED_PUBLIC_PORT:-80}}"
QUICK_TOKEN="${QUICK_TOKEN:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
APP_TOKEN="${APP_TOKEN:-}"

# For host/port/username/password the precedence is:
#   explicit env var  >  existing .env value (update only)  >  built-in default
# We must consult .env BEFORE applying the built-in defaults, otherwise a plain
# `press-Enter` update would silently rewrite WB_PORT/WB_HOST back to the
# defaults. (That was the old bug.)
_env_get() { grep -E "^${1}=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }
if $IS_UPDATE && [[ -f "$APP_DIR/.env" ]]; then
    APP_HOST="${APP_HOST:-$(_env_get WB_HOST)}"
    APP_PORT="${APP_PORT:-$(_env_get WB_PORT)}"
    ADMIN_USERNAME="${ADMIN_USERNAME:-$(_env_get WB_ADMIN_USERNAME)}"
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(_env_get WB_ADMIN_PASSWORD)}"
fi
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

# Remaining update-only prompt seeds (tokens): existing .env value as default.
if $IS_UPDATE && [[ -f "$APP_DIR/.env" ]]; then
    QUICK_TOKEN="${QUICK_TOKEN:-$(_env_get WB_QUICK_TOKEN)}"
    TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(_env_get WB_TELEGRAM_BOT_TOKEN)}"
    APP_TOKEN="${APP_TOKEN:-$(_env_get WB_APP_TOKEN)}"
fi

# On an update we keep the whole configuration as-is by default (values come
# from the existing .env + detected proxy config). The installer asks ONE
# yes/no question — whether to reconfigure — and only then walks the prompts.
# Non-interactive: pass RECONFIGURE=1 to force the prompts, or override any
# single value with its env var (PROXY=, DOMAIN=, APP_PORT=, ADMIN_PASSWORD=...).
ASK_QUESTIONS=1
if $IS_UPDATE; then
    ASK_QUESTIONS=0
    if [[ "${RECONFIGURE:-0}" == "1" ]]; then
        ASK_QUESTIONS=1
    fi
fi

echo
echo "=============================================================="
echo "  Whitelist-Bypass Instance Manager — installer"
if $IS_UPDATE; then
    echo "  MODE: UPDATE (existing installation at $APP_DIR)"
else
    echo "  (press Enter to accept the [default] for each question)"
fi
echo "=============================================================="

# The single update question: change configuration, or keep everything?
if $IS_UPDATE && [[ "$ASK_QUESTIONS" -eq 0 && "$INTERACTIVE" -eq 1 ]]; then
    _reconf=""
    read -r -p "Reconfigure settings (.env / proxy / admin)? Everything is kept as-is otherwise [y/N]: " _reconf || true
    case "${_reconf,,}" in
        y|yes) ASK_QUESTIONS=1 ;;
        *)     log "Keeping the current configuration unchanged." ;;
    esac
fi

if [[ "$ASK_QUESTIONS" -eq 1 ]]; then
    prompt PROXY       "Reverse proxy (nginx | caddy)" "$PROXY"
    PROXY="${PROXY,,}"   # lowercase
    case "$PROXY" in
        nginx|caddy) ;;
        *) die "PROXY must be 'nginx' or 'caddy' (got: $PROXY)";;
    esac

    prompt DOMAIN      "Domain for HTTPS (blank = plain HTTP on PUBLIC_PORT)" "$DOMAIN"
    if [[ -n "$DOMAIN" ]]; then
        if [[ "$PROXY" == "nginx" ]]; then
            prompt EMAIL "Email for Let's Encrypt" "${EMAIL:-}"
            [[ -n "$EMAIL" ]] || die "EMAIL is required for nginx + DOMAIN (Let's Encrypt)."
        fi
        log "Target: https://$DOMAIN  (TLS via $PROXY)"
    else
        log "Target: http://<server-ip>:$PUBLIC_PORT  (no TLS — set DOMAIN to enable)"
    fi

    prompt APP_PORT    "Internal uvicorn port (behind the proxy)" "$APP_PORT"
    if [[ -z "$DOMAIN" ]]; then
        prompt PUBLIC_PORT "External HTTP port the proxy listens on" "$PUBLIC_PORT"
        [[ "$PUBLIC_PORT" != "$APP_PORT" ]] \
            || warn "PUBLIC_PORT == APP_PORT ($APP_PORT): the proxy and uvicorn will both bind it. Set a different APP_PORT."
    fi

    prompt ADMIN_USERNAME "Admin username" "$ADMIN_USERNAME"
    # Password: don't echo. Blank = auto-generate (fresh) / keep current (update).
    if [[ "$INTERACTIVE" -eq 1 ]]; then
        if $IS_UPDATE; then
            read -r -s -p "Admin password (blank = keep current): " ADMIN_PASSWORD; echo
        else
            read -r -s -p "Admin password (blank = auto-generate): " ADMIN_PASSWORD; echo
        fi
    else
        if ! $IS_UPDATE; then
            : "${ADMIN_PASSWORD:?Non-interactive: set ADMIN_PASSWORD (or ADMIN_PASSWORD= to auto-generate is unsupported in CI)}"
        fi
    fi

    prompt QUICK_TOKEN "Quick-launch token (blank = disabled)" "$QUICK_TOKEN"
    prompt TELEGRAM_BOT_TOKEN "Telegram Mini App bot token (blank = keep current on update / disabled on fresh install)" "$TELEGRAM_BOT_TOKEN"
    prompt APP_TOKEN "Android app API token, X-App-Token (blank = keep current on update / disabled on fresh install)" "$APP_TOKEN"
else
    PROXY="${PROXY,,}"
    case "$PROXY" in nginx|caddy) ;; *) PROXY="nginx";; esac
    log "[UPDATE] proxy=$PROXY domain=${DOMAIN:-<none>} public_port=$PUBLIC_PORT app_port=$APP_PORT admin=$ADMIN_USERNAME"
fi

# #############################################################################
# 1b. Pre-flight: backup database (update only)
#
# NOTE: we deliberately DO NOT stop the service or kill child processes here.
#   * Child proxy binaries must keep running across the update. They are spawned
#     in their own sessions (start_new_session=True) and the systemd unit uses
#     KillMode=process, so the restart in step 7 signals ONLY the uvicorn main
#     process. The app re-adopts the surviving PIDs on startup (main.py).
#   * The Python source is swapped in place by rsync (inode replacement) while
#     uvicorn runs; the new code is picked up by the single restart in step 7.
# #############################################################################
if $IS_UPDATE; then
    MODE_LABEL="UPDATE"
    echo
    log "[UPDATE] Detected existing installation at $APP_DIR"
    log "[UPDATE] Running proxy instances will be preserved (no kill, main-process-only restart)"

    # Backup SQLite database (keep last 3 backups). Stored OUTSIDE the app
    #    dir (/var/backups) so that no failure inside $APP_DIR — wipe bugs,
    #    bad deploys — can ever take the backups down with it.
    DB_PATH="$APP_DIR/data/app.db"
    if [[ -f "$DB_PATH" ]]; then
        BACKUP_DIR="/var/backups/${SERVICE_NAME}"
        mkdir -p "$BACKUP_DIR"
        TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
        BACKUP_FILE="$BACKUP_DIR/app.db.${TIMESTAMP}.bak"
        log "[UPDATE] Backing up database ..."
        cp -a "$DB_PATH" "$BACKUP_FILE"
        chmod 600 "$BACKUP_FILE"
        ok "[UPDATE] Database backed up to $BACKUP_FILE"

        # Rotate: keep only the 3 most recent backups.
        ls -1t "$BACKUP_DIR"/app.db.*.bak 2>/dev/null | tail -n +4 | xargs -r rm -f
        BACKUP_COUNT="$(ls -1 "$BACKUP_DIR"/app.db.*.bak 2>/dev/null | wc -l)"
        log "[UPDATE] $BACKUP_COUNT backup(s) retained in $BACKUP_DIR/"
    else
        ok "[UPDATE] No database found — skipping backup"
    fi

    echo
fi

# #############################################################################
# 2. System packages
# #############################################################################
log "Installing base system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    sqlite3 \
    ufw curl ca-certificates gnupg \
    proxychains4 \
    rsync \
    > /dev/null

if [[ "$PROXY" == "nginx" ]]; then
    log "Installing nginx..."
    apt-get install -y -qq nginx > /dev/null
    # certbot + nginx plugin only needed for nginx + a domain.
    if [[ -n "$DOMAIN" ]]; then
        apt-get install -y -qq certbot python3-certbot-nginx > /dev/null || warn "certbot install failed — TLS step will be skipped."
    fi
else
    log "Installing caddy (official apt repo)..."
    # Per the official Caddy docs for Debian/Ubuntu.
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https >/dev/null
    if [[ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    fi
    if [[ ! -f /etc/apt/sources.list.d/caddy-stable.list ]]; then
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq
    fi
    apt-get install -y -qq caddy > /dev/null || die "caddy install failed (need network access to dl.cloudsmith.io)."
fi
ok "System packages installed"

# #############################################################################
# 3. Dedicated service user + directories
# #############################################################################
log "Ensuring service user '$SERVICE_USER'..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Created user $SERVICE_USER"
else
    ok "User $SERVICE_USER already exists"
fi

log "Installing app into $APP_DIR ..."
mkdir -p "$APP_DIR"
# Copy app code (rsync keeps the dir; --delete mirrors source minus ignores).
# NOTE the excludes that keep SERVER-ONLY state intact on updates:
#   data/     — SQLite DB, cookies, logs, backups
#   .env      — live config incl. WB_APP_TOKEN (deleting it would disable the
#               whole /api/app router and break the Android flow)
#   binaries/ — uploaded service binaries
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
        --exclude '.git' --exclude '*.pyc' --exclude '.DS_Store' \
        --exclude '.env' --exclude 'binaries' \
        "$SRC_DIR"/ "$APP_DIR"/
else
    # rsync absent: mirror source by hand, but PRESERVE data/, .env, binaries/
    # and .venv. CRITICAL: the staging dir lives OUTSIDE $APP_DIR (a sibling),
    # because an earlier version staged the preserve-copies INSIDE the app dir
    # as dot-files and the `rm -rf ... /.*` wipe below deleted them together
    # with everything else. The wipe itself uses `find` (no shell glob), so
    # nothing outside $APP_DIR can ever be touched.
    STAGE="$(mktemp -d "${APP_DIR%/}.preserve.XXXXXX")"
    declare -a PRESERVE=()
    for item in data .env binaries .venv; do
        if [[ -e "$APP_DIR/$item" ]]; then
            mv "$APP_DIR/$item" "$STAGE/$item"
            PRESERVE+=("$item")
        fi
    done
    find "$APP_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    cp -a "$SRC_DIR"/. "$APP_DIR"/
    find "$APP_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
    for item in "${PRESERVE[@]}"; do
        rm -rf "$APP_DIR/$item"
        mv "$STAGE/$item" "$APP_DIR/$item" \
            || die "FATAL: could not restore '$item' from $STAGE — it is still safe there, resolve manually"
    done
    rmdir "$STAGE" 2>/dev/null || true
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/binaries"
ok "App files copied"

# #############################################################################
# 4. Virtualenv + Python deps
# #############################################################################
log "Building virtualenv and installing Python deps..."
if [[ ! -d "$APP_DIR/.venv" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Python deps installed"

# #############################################################################
# 5. .env  (fresh install: seed from template + apply prompts.
#           update:        touch NOTHING the operator did not explicitly change,
#                          but pull in brand-new keys from the shipped template.)
# #############################################################################
ENV_EXISTED=true
if [[ ! -f "$APP_DIR/.env" ]]; then
    ENV_EXISTED=false
    TEMPLATE=""
    for cand in "$APP_DIR/.env.example" "$APP_DIR/env.example"; do
        if [[ -f "$cand" ]]; then TEMPLATE="$cand"; break; fi
    done
    if [[ -z "$TEMPLATE" ]]; then
        die "No env template found: need .env.example or env.example in $APP_DIR"
    fi
    log "Creating $APP_DIR/.env from $(basename "$TEMPLATE") ..."
    cp "$TEMPLATE" "$APP_DIR/.env"
fi

# Generate a strong password if none was provided (fresh install only).
# On update, blank means "keep the existing one from .env".
if [[ -z "$ADMIN_PASSWORD" ]]; then
    if [[ -f "$APP_DIR/.env" ]]; then
        ADMIN_PASSWORD="$(grep -E '^WB_ADMIN_PASSWORD=' "$APP_DIR/.env" | head -1 | cut -d= -f2-)"
    fi
    if [[ -z "$ADMIN_PASSWORD" ]]; then
        ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
        ok "Auto-generated admin password (alphanumeric, no special chars)"
    fi
fi

# _cur_env <KEY> — current value of KEY in the live .env ('' if absent).
_cur_env() { grep -E "^${1}=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# upsert_key <KEY> <VALUE> — sets KEY=VALUE, replacing any existing line.
upsert_key() {
    local key="$1" val="$2" file="$APP_DIR/.env"
    # Escape | & / for the sed replacement; values here are simple (paths,
    # ports, passwords) so escaping | is enough as we use | as the delimiter.
    local esc
    esc="${val//\\/\\\\}"
    esc="${esc//|/\\|}"
    if grep -qE "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${esc}|" "$file"
    else
        printf '%s=%s\n' "$key" "$val" >> "$file"
    fi
}

# add_if_missing <KEY> <VALUE> — append KEY=VALUE only when the key is absent.
# Used for keys that have safe defaults and may be hand-tuned by the operator:
# an update must never clobber them.
add_if_missing() {
    local key="$1" val="$2" file="$APP_DIR/.env"
    [[ -n "$val" ]] || return 0
    grep -qE "^${key}=" "$file" || printf '%s=%s\n' "$key" "$val" >> "$file"
}

# Values for App Links verification come from the shipped .env.example so the
# fingerprint lives in exactly one place. Env overrides win, then existing
# .env, then the template default.
_env_def() { grep -E "^${1}=" "$APP_DIR/.env.example" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# set_key <KEY> <DESIRED>
#   fresh install       -> write DESIRED
#   update, key absent   -> write DESIRED (a new key this release introduced)
#   update, key present  -> write ONLY if DESIRED is non-empty AND differs from
#                           the current value (i.e. the operator typed/passed a
#                           new value); otherwise the on-disk value is kept.
set_key() {
    local key="$1" desired="$2" cur
    if ! $ENV_EXISTED; then
        upsert_key "$key" "$desired"; return
    fi
    if ! grep -qE "^${key}=" "$APP_DIR/.env"; then
        [[ -n "$desired" ]] || return 0
        add_if_missing "$key" "$desired"
        log "[UPDATE] .env: added new key $key"
        return
    fi
    cur="$(_cur_env "$key")"
    if [[ -n "$desired" && "$desired" != "$cur" ]]; then
        upsert_key "$key" "$desired"
        log "[UPDATE] .env: $key updated (operator-supplied value)"
    fi
}

# Admin password: on an update it is re-synced to the DB ONLY when a new value
# was supplied (env var or a changed interactive answer). Blank / unchanged =>
# keep the current password, in both .env and the DB.
SYNC_ADMIN=1
if $ENV_EXISTED; then
    CUR_ADMIN_PW="$(_cur_env WB_ADMIN_PASSWORD)"
    if [[ -n "$ADMIN_PASSWORD" && "$ADMIN_PASSWORD" != "$CUR_ADMIN_PW" ]]; then
        SYNC_ADMIN=1
    else
        SYNC_ADMIN=0
        ADMIN_PASSWORD="$CUR_ADMIN_PW"
    fi
fi

set_key "WB_HOST"           "$APP_HOST"
set_key "WB_PORT"           "$APP_PORT"
set_key "WB_ADMIN_USERNAME" "$ADMIN_USERNAME"
if ! $ENV_EXISTED || [[ "$SYNC_ADMIN" == "1" ]]; then
    upsert_key "WB_ADMIN_PASSWORD" "$ADMIN_PASSWORD"
fi
# Tokens: only ever written, never deleted on update (wiping them silently
# disabled Telegram login / the Android /api/app router on old re-installs).
if [[ -n "$QUICK_TOKEN" ]]; then
    set_key "WB_QUICK_TOKEN" "$QUICK_TOKEN"
fi
if [[ -n "$TELEGRAM_BOT_TOKEN" ]]; then
    set_key "WB_TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
elif ! $ENV_EXISTED; then
    sed -i '/^WB_TELEGRAM_BOT_TOKEN=/d' "$APP_DIR/.env" 2>/dev/null || true
fi
if [[ -n "$APP_TOKEN" ]]; then
    set_key "WB_APP_TOKEN" "$APP_TOKEN"
fi
# Android App Links verification (mandatory for Android 15+/16 to open the
# /tginit https callback in the app). add-if-missing keeps a manually tuned
# fingerprint intact.
add_if_missing "WB_APP_PACKAGE"     "${APP_PACKAGE:-$(_env_def WB_APP_PACKAGE)}"
add_if_missing "WB_APP_CERT_SHA256" "${APP_CERT_SHA256:-$(_env_def WB_APP_CERT_SHA256)}"
# Storage paths: pin on a fresh install; on an update only fill them in if the
# running .env somehow lacks them (never repoint an existing install).
if ! $ENV_EXISTED; then
    upsert_key "WB_DATA_DIR"      "$APP_DIR/data"
    upsert_key "WB_BINARIES_DIR"  "$APP_DIR/binaries"
    upsert_key "WB_DATABASE_PATH" "$APP_DIR/data/app.db"
else
    add_if_missing "WB_DATA_DIR"      "$APP_DIR/data"
    add_if_missing "WB_BINARIES_DIR"  "$APP_DIR/binaries"
    add_if_missing "WB_DATABASE_PATH" "$APP_DIR/data/app.db"
fi

# Pull in any brand-new keys shipped in this release's .env.example that the
# live .env does not have yet — with the template default, commented context
# skipped. Existing keys are never touched here.
if [[ -f "$APP_DIR/.env.example" ]]; then
    NEW_KEYS=0
    while IFS= read -r line; do
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
        k="${line%%=*}"
        grep -qE "^${k}=" "$APP_DIR/.env" && continue
        printf '%s\n' "$line" >> "$APP_DIR/.env"
        log "[UPDATE] .env: added new key $k (from .env.example)"
        NEW_KEYS=$((NEW_KEYS+1))
    done < "$APP_DIR/.env.example"
    [[ "$NEW_KEYS" -gt 0 ]] && ok "Added $NEW_KEYS new key(s) from .env.example"
fi

chmod 600 "$APP_DIR/.env"   # contains the admin password — protect it.
if $ENV_EXISTED; then
    ok ".env reconciled (existing values kept; only new keys / operator-set values written)"
else
    ok ".env written at $APP_DIR/.env (mode 600)"
fi

# #############################################################################
# 6. DB init + (conditional) admin password sync
#
# Schema creation is idempotent (CREATE TABLE IF NOT EXISTS) and the app applies
# its own column migrations on connect, so this never destroys data.
# The admin row is only re-synced when SYNC_ADMIN=1 (fresh install, or the
# operator explicitly passed a new ADMIN_PASSWORD). Otherwise the existing admin
# credentials are left exactly as they are.
# #############################################################################
log "Initializing database (schema)..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# argv[1] = APP_DIR, argv[2] = SYNC_ADMIN ("1" resets the admin password hash to
# WB_ADMIN_PASSWORD; "0" only creates the admin if it does not exist yet).
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" - "$APP_DIR" "$SYNC_ADMIN" <<'PY'
import asyncio, os, sys
APP_DIR = sys.argv[1]
SYNC_ADMIN = (len(sys.argv) > 2 and sys.argv[2] == "1")
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

# Load .env manually so WB_* settings resolve even outside systemd.
from pathlib import Path
envf = Path(APP_DIR) / ".env"
for line in envf.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import config
config.ensure_dirs()
from db import db
from security import hash_password

async def main():
    await db.connect()
    uname = config.ADMIN_USERNAME
    phash = hash_password(config.ADMIN_PASSWORD)
    if SYNC_ADMIN:
        # Create on first run, otherwise overwrite the password hash + role so
        # the DB matches .env. Only reached on a fresh install or when the
        # operator explicitly passed a new ADMIN_PASSWORD.
        await db.execute(
            """
            INSERT INTO users (username, password_hash, role, max_concurrent)
            VALUES (?, ?, 'admin', 3)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                role          = 'admin',
                enabled       = 1
            """,
            (uname, phash),
        )
        print("  schema ready + admin '%s' password synced" % uname)
    else:
        # Update with no explicit password change: never touch an existing
        # admin row. Only seed one if the DB somehow has none.
        await db.execute(
            """
            INSERT INTO users (username, password_hash, role, max_concurrent)
            VALUES (?, ?, 'admin', 3)
            ON CONFLICT(username) DO NOTHING
            """,
            (uname, phash),
        )
        print("  schema ready + admin '%s' left untouched" % uname)
    await db.close()

asyncio.run(main())
PY
if [[ "$SYNC_ADMIN" == "1" ]]; then
    ok "Database ready, admin password synced to .env"
else
    ok "Database ready, existing admin credentials preserved"
fi

# #############################################################################
# 7. systemd service
# #############################################################################
log "Installing systemd unit..."
UNIT_SRC="$SCRIPT_DIR/wb-manager.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
UNIT_NEW="$(mktemp)"
sed -e "s|{{APP_DIR}}|$APP_DIR|g" \
    -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
    -e "s|{{APP_HOST}}|$APP_HOST|g" \
    -e "s|{{APP_PORT}}|$APP_PORT|g" \
    "$UNIT_SRC" > "$UNIT_NEW"
if ! cmp -s "$UNIT_NEW" "$UNIT_DST" 2>/dev/null; then
    install -m0644 "$UNIT_NEW" "$UNIT_DST"
    systemctl daemon-reload
    ok "systemd unit updated"
else
    ok "systemd unit unchanged"
fi
rm -f "$UNIT_NEW"
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
# Plain restart: the unit uses KillMode=process, so ONLY the uvicorn main
# process is signalled. Running proxy child binaries keep going and the app
# re-adopts their PIDs on startup (main.py reattach()).
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service $SERVICE_NAME is running on ${APP_HOST}:${APP_PORT}"
else
    die "Service failed to start. Inspect: journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

# Smoke: the App Links statement must be served by the backend — without it
# Android 15+/16 refuses to open the /tginit https link in the app (the
# "works on Android 12, dead on 16" symptom).
ASSETLINKS="$(curl -fsS "http://${APP_HOST}:${APP_PORT}/.well-known/assetlinks.json" 2>/dev/null || true)"
if [[ "$ASSETLINKS" == *'"android_app"'* ]]; then
    ok "App Links statement served (/ .well-known/assetlinks.json)"
else
    warn "assetlinks.json NOT served by the backend — Android 15+/16 will not"
    warn "open the app link. Check WB_APP_PACKAGE / WB_APP_CERT_SHA256 and: journalctl -u $SERVICE_NAME -n 30"
fi

# #############################################################################
# 8. Reverse proxy (nginx OR caddy)
#
# On a keep-as-is update (no reconfigure) we do NOT touch an existing proxy
# config — rewriting it would drop certbot's TLS edits. We only (re)generate it
# on a fresh install, when reconfiguring, or when the config is missing.
# #############################################################################
PROXY_RECONF=1
if $IS_UPDATE && [[ "$ASK_QUESTIONS" -eq 0 ]]; then
    if { [[ "$PROXY" == "nginx" ]] && [[ -f "/etc/nginx/sites-available/${SERVICE_NAME}" ]]; } \
    || { [[ "$PROXY" == "caddy" ]] && [[ -f /etc/caddy/Caddyfile ]]; }; then
        PROXY_RECONF=0
    fi
fi

if [[ "$PROXY_RECONF" -eq 0 ]]; then
    log "Reverse proxy ($PROXY) left unchanged; reloading only."
    if [[ "$PROXY" == "nginx" ]]; then
        nginx -t >/dev/null 2>&1 && systemctl reload nginx || warn "nginx -t failed; config left as-is"
    else
        systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || true
    fi
elif [[ "$PROXY" == "nginx" ]]; then
    _install_nginx() { :; }   # keep shellcheck happy; real work below
    log "Installing nginx config..."

    # Define $connection_upgrade inside the http{} context (idempotent).
    if [[ ! -f /etc/nginx/conf.d/connection_upgrade.conf ]]; then
        cat >/etc/nginx/conf.d/connection_upgrade.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
EOF
        ok "Created /etc/nginx/conf.d/connection_upgrade.conf"
    else
        ok "connection_upgrade map already configured"
    fi

    # Shared proxy snippet.
    install -m0644 "$SCRIPT_DIR/wb-proxy.snippet.conf" /etc/nginx/snippets/wb-proxy.conf

    NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
    NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"

    if [[ -n "$DOMAIN" ]]; then
        # Full TLS site config; certbot will fill in the cert paths.
        sed -e "s|wb.example.com|$DOMAIN|g" \
            -e "s|/opt/whitelist-manager/static|$APP_DIR/static|g" \
            -e "s|{{APP_PORT}}|$APP_PORT|g" \
            "$SCRIPT_DIR/nginx.sample.conf" > "$NGINX_SITE"
    else
        # No domain: minimal plain-HTTP reverse proxy on PUBLIC_PORT.
        cat > "$NGINX_SITE" <<EOF
server {
    listen ${PUBLIC_PORT} default_server;
    listen [::]:${PUBLIC_PORT};
    server_name _;
    client_max_body_size 1m;
    location /static/ { alias ${APP_DIR}/static/; expires 1h; }
    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        include /etc/nginx/snippets/wb-proxy.conf;
    }
}
EOF
    fi

    ln -sfn "$NGINX_SITE" "$NGINX_LINK"
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

    if ! nginx -t >/dev/null 2>&1; then
        die "nginx config test failed. Inspect: nginx -t"
    fi
    systemctl reload nginx
    ok "nginx configured and reloaded"

else
    # ----- Caddy -----
    log "Installing Caddyfile..."
    CADDYFILE="/etc/caddy/Caddyfile"
    mkdir -p /etc/caddy
    # Caddy obtains TLS automatically when a domain is given; with no domain we
    # serve plain HTTP on PUBLIC_PORT.
    if [[ -n "$DOMAIN" ]]; then
        cat > "$CADDYFILE" <<EOF
# Managed by whitelist-manager install.sh
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF
    else
        cat > "$CADDYFILE" <<EOF
# Managed by whitelist-manager install.sh (no TLS — no domain)
:${PUBLIC_PORT} {
    encode gzip
    reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF
    fi
    systemctl enable caddy >/dev/null 2>&1 || true
    if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
        warn "Caddyfile validation had warnings — continuing anyway."
    fi
    systemctl restart caddy || warn "caddy restart failed; check: journalctl -u caddy -n 50"
    ok "Caddy configured and restarted"
fi

# #############################################################################
# 9. Firewall (ufw)
# #############################################################################
log "Configuring firewall (ufw)..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH         >/dev/null 2>&1 || true
    if [[ -n "$DOMAIN" ]]; then
        ufw allow 80/tcp   >/dev/null 2>&1 || true
        ufw allow 443/tcp  >/dev/null 2>&1 || true
    else
        ufw allow "${PUBLIC_PORT}/tcp" >/dev/null 2>&1 || true
    fi
    yes | ufw enable >/dev/null 2>&1 || true
    ok "Firewall rules set"
else
    warn "ufw not available — skipping firewall"
fi

# #############################################################################
# 10. Let's Encrypt (nginx path only; caddy does its own TLS)
# #############################################################################
if [[ -n "$DOMAIN" && "$PROXY" == "nginx" ]] \
   && [[ -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    ok "TLS certificate for $DOMAIN already present — leaving it untouched (renewal is automatic)"
elif [[ -n "$DOMAIN" && "$PROXY" == "nginx" && -z "$EMAIL" ]]; then
    warn "DOMAIN set but no EMAIL and no existing cert — skipping Let's Encrypt."
    warn "Run: sudo certbot --nginx -d $DOMAIN   (or re-run with RECONFIGURE=1 EMAIL=you@x.com)"
elif [[ -n "$DOMAIN" && "$PROXY" == "nginx" ]]; then
    log "Requesting TLS certificate for $DOMAIN ..."
    if certbot --nginx -n --redirect \
         --agree-tos -m "$EMAIL" --no-eff-email \
         -d "$DOMAIN"; then
        ok "TLS certificate issued; nginx configured for https"
    else
        warn "Certbot failed — nginx is serving on port 80 for now."
        warn "Once DNS for $DOMAIN points here, run: sudo certbot --nginx -d $DOMAIN"
    fi
fi

# #############################################################################
# Done — show next steps
# #############################################################################
echo
ok "================ ${MODE_LABEL:-INSTALL} COMPLETE ================"
echo "  App dir:     $APP_DIR"
echo "  Backend:     ${APP_HOST}:${APP_PORT}  (systemd: systemctl status $SERVICE_NAME)"
echo "  Logs:        journalctl -u $SERVICE_NAME -f"
echo "  Admin user:  $ADMIN_USERNAME"
echo "  Admin pass:  $ADMIN_PASSWORD   (also in $APP_DIR/.env)"
if [[ -n "$DOMAIN" ]]; then
    echo "  URL:         https://$DOMAIN"
    if [[ -n "$QUICK_TOKEN" ]]; then
        echo "  Quick launch: https://$DOMAIN/quick?token=$QUICK_TOKEN"
    fi
else
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "  URL:         http://${IP}:${PUBLIC_PORT}"
    if [[ -n "$QUICK_TOKEN" ]]; then
        echo "  Quick launch: http://${IP}:${PUBLIC_PORT}/quick?token=$QUICK_TOKEN"
    fi
fi
echo "  Proxy:       $PROXY"
echo
if $IS_UPDATE; then
    echo "  NOTE: UPDATE mode — non-destructive:"
    echo "    * database preserved (backup in /var/backups/${SERVICE_NAME}/)"
    echo "    * .env preserved (only new keys / values you changed were written)"
    if [[ "$SYNC_ADMIN" == "1" ]]; then
        echo "    * admin password re-synced (you supplied a new one)"
    else
        echo "    * admin password left unchanged"
    fi
    echo "    * running proxy instances kept alive across the restart"
else
    echo "  NOTE: on a FRESH install the admin password is the one shown above /"
    echo "  stored in $APP_DIR/.env. Later updates never reset it unless you pass"
    echo "  a new ADMIN_PASSWORD."
fi
echo "==================================================="
