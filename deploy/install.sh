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
#   PROXYCHAINS_ENABLED  1                (default: disabled)
#   PROXYCHAINS_TYPE     socks5           (socks5 | socks4 | http)
#   PROXYCHAINS_HOST     127.0.0.1
#   PROXYCHAINS_PORT     1080
#   APP_DIR       install path           (default /opt/whitelist-manager)
#   SERVICE_USER  unprivileged user      (default wb-manager)
#   DEBUG=1       enable set -x tracing
#
# NOTE on admin password: the bootstrap admin is created on first run only, so
# editing WB_ADMIN_PASSWORD in .env later would NOT change it. To avoid the
# "I changed the password but still can't log in" trap, this installer ALWAYS
# re-syncs the admin row's password hash to whatever ends up in .env. Re-running
# the installer therefore resets the admin password — by design.
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

# Detect whether we can prompt interactively.
INTERACTIVE=0
if [[ -t 0 ]] && [[ -t 1 ]]; then INTERACTIVE=1; fi

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
PROXY="${PROXY:-nginx}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"
PUBLIC_PORT="${PUBLIC_PORT:-80}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
QUICK_TOKEN="${QUICK_TOKEN:-}"
PROXYCHAINS_ENABLED="${PROXYCHAINS_ENABLED:-}"
PROXYCHAINS_TYPE="${PROXYCHAINS_TYPE:-socks5}"
PROXYCHAINS_HOST="${PROXYCHAINS_HOST:-}"
PROXYCHAINS_PORT="${PROXYCHAINS_PORT:-}"
APP_DIR="${APP_DIR:-/opt/whitelist-manager}"
SERVICE_USER="${SERVICE_USER:-wb-manager}"
SERVICE_NAME="wb-manager"

echo
echo "=============================================================="
echo "  Whitelist-Bypass Instance Manager — installer"
echo "  (press Enter to accept the [default] for each question)"
echo "=============================================================="

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
# Password: don't echo. Allow blank -> auto-generate later.
if [[ "$INTERACTIVE" -eq 1 ]]; then
    read -r -s -p "Admin password (blank = auto-generate): " ADMIN_PASSWORD; echo
else
    : "${ADMIN_PASSWORD:?Non-interactive: set ADMIN_PASSWORD (or ADMIN_PASSWORD= to auto-generate is unsupported in CI)}"
fi

prompt QUICK_TOKEN "Quick-launch token (blank = disabled)" "$QUICK_TOKEN"

# Proxychains4
prompt PROXYCHAINS_ENABLED "Enable proxychains4 for all instances? (y/n)" "$PROXYCHAINS_ENABLED"
PROXYCHAINS_ENABLED="${PROXYCHAINS_ENABLED,,}"
if [[ "$PROXYCHAINS_ENABLED" == "y" || "$PROXYCHAINS_ENABLED" == "yes" || "$PROXYCHAINS_ENABLED" == "1" ]]; then
    PROXYCHAINS_ENABLED="1"
    prompt PROXYCHAINS_TYPE "Proxy type (socks5 | socks4 | http)" "$PROXYCHAINS_TYPE"
    prompt PROXYCHAINS_HOST "Proxy host (e.g. 127.0.0.1)" "$PROXYCHAINS_HOST"
    [[ -n "$PROXYCHAINS_HOST" ]] || die "PROXYCHAINS_HOST is required when proxychains is enabled"
    prompt PROXYCHAINS_PORT "Proxy port (e.g. 1080)" "$PROXYCHAINS_PORT"
    [[ -n "$PROXYCHAINS_PORT" ]] || die "PROXYCHAINS_PORT is required when proxychains is enabled"
    log "Proxychains4 enabled: $PROXYCHAINS_TYPE://$PROXYCHAINS_HOST:$PROXYCHAINS_PORT"
else
    PROXYCHAINS_ENABLED=""
    log "Proxychains4 disabled"
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
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
        --exclude '.git' --exclude '*.pyc' --exclude '.DS_Store' \
        "$SRC_DIR"/ "$APP_DIR"/
else
    rm -rf "$APP_DIR"/* "$APP_DIR"/.* 2>/dev/null || true
    cp -a "$SRC_DIR"/. "$APP_DIR"/
    find "$APP_DIR" -type d -name '__pycache__' -prune -exec rm -rf {} +
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
# 5. .env (create or reconcile; do NOT clobber an existing one wholesale)
# #############################################################################
# If .env already exists we still force-update the values we care about so the
# running config matches the prompts above. That is the whole point: previously
# editing WB_ADMIN_PASSWORD had no effect.
if [[ ! -f "$APP_DIR/.env" ]]; then
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

# Generate a strong password if none was provided. Use only [A-Za-z0-9] so it
# survives copy/paste and shell/JSON quoting (base64's +/= caused login bugs).
if [[ -z "$ADMIN_PASSWORD" ]]; then
    ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    ok "Auto-generated admin password (alphanumeric, no special chars)"
fi

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

upsert_key "WB_HOST"            "$APP_HOST"
upsert_key "WB_PORT"            "$APP_PORT"
upsert_key "WB_ADMIN_USERNAME"  "$ADMIN_USERNAME"
upsert_key "WB_ADMIN_PASSWORD"  "$ADMIN_PASSWORD"
if [[ -n "$QUICK_TOKEN" ]]; then
    upsert_key "WB_QUICK_TOKEN" "$QUICK_TOKEN"
fi
if [[ -n "$PROXYCHAINS_ENABLED" ]]; then
    upsert_key "WB_PROXYCHAINS_ENABLED" "$PROXYCHAINS_ENABLED"
    upsert_key "WB_PROXYCHAINS_TYPE" "$PROXYCHAINS_TYPE"
    upsert_key "WB_PROXYCHAINS_HOST" "$PROXYCHAINS_HOST"
    upsert_key "WB_PROXYCHAINS_PORT" "$PROXYCHAINS_PORT"
fi
upsert_key "WB_DATA_DIR"        "$APP_DIR/data"
upsert_key "WB_BINARIES_DIR"    "$APP_DIR/binaries"
upsert_key "WB_DATABASE_PATH"   "$APP_DIR/data/app.db"
chmod 600 "$APP_DIR/.env"   # contains the admin password — protect it.
ok ".env written at $APP_DIR/.env (mode 600)"

# #############################################################################
# 6. DB init + forced admin password sync
# #############################################################################
log "Initializing database (schema)..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# Load .env into the subprocess's environment, init the schema, then force the
# admin row to match WB_ADMIN_PASSWORD. We pass APP_DIR as argv[1] AND insert
# it on sys.path so `import config` works regardless of cwd.
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import asyncio, os, sys
APP_DIR = sys.argv[1]
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
    # UPSERT the admin: create on first run, otherwise overwrite the password
    # hash + role so the DB always matches .env. This is what makes a password
    # change in .env actually take effect.
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
    await db.close()
    print("  schema + admin '%s' synced" % uname)

asyncio.run(main())
PY
ok "Database ready, admin password synced to .env"

# #############################################################################
# 7. systemd service
# #############################################################################
log "Installing systemd unit..."
UNIT_SRC="$SCRIPT_DIR/wb-manager.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
sed -e "s|{{APP_DIR}}|$APP_DIR|g" \
    -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
    -e "s|{{APP_HOST}}|$APP_HOST|g" \
    -e "s|{{APP_PORT}}|$APP_PORT|g" \
    "$UNIT_SRC" > "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service $SERVICE_NAME is running on ${APP_HOST}:${APP_PORT}"
else
    die "Service failed to start. Inspect: journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

# #############################################################################
# 8. Reverse proxy (nginx OR caddy)
# #############################################################################
if [[ "$PROXY" == "nginx" ]]; then
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
if [[ -n "$DOMAIN" && "$PROXY" == "nginx" ]]; then
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
ok "================ INSTALL COMPLETE ================"
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
if [[ -n "$PROXYCHAINS_ENABLED" ]]; then
    echo "  Proxychains:  $PROXYCHAINS_TYPE://$PROXYCHAINS_HOST:$PROXYCHAINS_PORT"
fi
echo
echo "  NOTE: re-running this installer RESETS the admin password to the one"
echo "  shown above / stored in $APP_DIR/.env (by design — see README)."
echo "==================================================="
