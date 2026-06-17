#!/usr/bin/env bash
# =============================================================================
# install.sh — one-shot installer for the Whitelist-Bypass Instance Manager.
#
# Idempotent: safe to re-run. Designed for Ubuntu 22.04 / 24.04 (and Debian 12).
#
# What it does:
#   1. Installs system packages (python, venv, nginx, sqlite3, certbot, ufw)
#   2. Creates a dedicated, unprivileged service user + install dir
#   3. Copies the app, builds the venv, installs Python deps
#   4. Seeds .env (from .env.example) if missing, inits the DB
#   5. Installs + enables the systemd service (wb-manager.service)
#   6. Installs the nginx site (+ shared proxy snippet), reloads nginx
#   7. Opens firewall ports (80/443, or 8000 if no domain)
#   8. Optionally issues a Let's Encrypt TLS certificate
#
# Usage (as root, or with sudo):
#   sudo bash deploy/install.sh                        # no TLS (port 8000 direct)
#   sudo DOMAIN=wb.example.com EMAIL=you@x.com bash deploy/install.sh
#   sudo DOMAIN=wb.example.com EMAIL=you@x.com bash deploy/install.sh --reinstall
#
# Env knobs:
#   DOMAIN   FQDN to serve on (enables TLS via Let's Encrypt). Optional.
#   EMAIL    Email for Let's Encrypt. Required if DOMAIN is set.
#   APP_DIR  Install path (default /opt/whitelist-manager).
#   SERVICE_USER  Unprivileged user (default wb-manager).
# =============================================================================
set -euo pipefail

# ---- logging helpers ----
log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- config / preflight ----
APP_DIR="${APP_DIR:-/opt/whitelist-manager}"
SERVICE_USER="${SERVICE_USER:-wb-manager}"
SERVICE_NAME="wb-manager"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../deploy
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                       # the project root

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
[[ -f "$SRC_DIR/main.py" ]] || die "Could not find app at $SRC_DIR/main.py"
if [[ -n "$DOMAIN" && -z "$EMAIL" ]]; then die "EMAIL is required when DOMAIN is set (for Let's Encrypt)."; fi

if [[ -n "$DOMAIN" ]]; then
    log "Target: https://$DOMAIN  (TLS via Let's Encrypt)"
else
    log "Target: http://<server-ip>:8000  (no TLS — pass DOMAIN=... to enable)"
fi

# #############################################################################
# 1. System packages
# #############################################################################
log "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    nginx sqlite3 \
    ufw curl ca-certificates \
    > /dev/null
# certbot + nginx plugin only if we have a domain
if [[ -n "$DOMAIN" ]]; then
    apt-get install -y -qq certbot python3-certbot-nginx > /dev/null || true
fi
ok "System packages installed"

# #############################################################################
# 2. Dedicated service user + directories
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
# 3. Virtualenv + Python deps
# #############################################################################
log "Building virtualenv and installing Python deps..."
if [[ ! -d "$APP_DIR/.venv" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "Python deps installed"

# #############################################################################
# 4. .env + DB init
# #############################################################################
if [[ ! -f "$APP_DIR/.env" ]]; then
    # Find the template under whichever name shipped: .env.example or env.example
    TEMPLATE=""
    for cand in "$APP_DIR/.env.example" "$APP_DIR/env.example"; do
        if [[ -f "$cand" ]]; then TEMPLATE="$cand"; break; fi
    done
    if [[ -z "$TEMPLATE" ]]; then
        die "No env template found: need .env.example or env.example in $APP_DIR"
    fi
    log "Creating $APP_DIR/.env from $(basename "$TEMPLATE") ..."
    cp "$TEMPLATE" "$APP_DIR/.env"
    # Point env paths at the install dir.
    sed -i \
        -e "s|^WB_DATA_DIR=.*|WB_DATA_DIR=$APP_DIR/data|" \
        -e "s|^WB_BINARIES_DIR=.*|WB_BINARIES_DIR=$APP_DIR/binaries|" \
        -e "s|^WB_DATABASE_PATH=.*|WB_DATABASE_PATH=$APP_DIR/data/app.db|" \
        "$APP_DIR/.env"
    # Generate a strong random admin password on first install.
    NEWPW="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64)"
    sed -i "s|^WB_ADMIN_PASSWORD=.*|WB_ADMIN_PASSWORD=$NEWPW|" "$APP_DIR/.env"
    ok "Admin password generated (see $APP_DIR/.env) -> username: admin"
else
    ok ".env already exists — left untouched"
fi

# Create the SQLite DB + schema by importing the app (lifespan runs init).
log "Initializing database..."
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
# NOTE: pass APP_DIR as an arg AND sys.path.insert it, so imports resolve
# regardless of the cwd sudo leaves us in. (A quoted heredoc <<'PY' would
# NOT expand $APP_DIR — that was the original bug.)
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import asyncio, os, sys
APP_DIR = sys.argv[1]
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)        # <- guarantee `import config` works
# Load .env manually so paths resolve even outside systemd.
from pathlib import Path
envf = Path(APP_DIR) / ".env"
if envf.exists():
    for line in envf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
import config; config.ensure_dirs()
from db import db
asyncio.run(db.connect())
asyncio.run(db.close())
print("  schema created at", config.DATABASE_PATH)
PY
ok "Database initialized"

# #############################################################################
# 5. systemd service
# #############################################################################
log "Installing systemd unit..."
UNIT_SRC="$SCRIPT_DIR/wb-manager.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
# Render the template placeholders.
sed -e "s|{{APP_DIR}}|$APP_DIR|g" \
    -e "s|{{SERVICE_USER}}|$SERVICE_USER|g" \
    "$UNIT_SRC" > "$UNIT_DST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Service $SERVICE_NAME is running"
else
    die "Service failed to start. Inspect: journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

# #############################################################################
# 6. nginx
# #############################################################################
log "Installing nginx config..."
# Shared proxy snippet first.
install -m0644 "$SCRIPT_DIR/wb-proxy.snippet.conf" /etc/nginx/snippets/wb-proxy.conf

NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"

if [[ -n "$DOMAIN" ]]; then
    # Render the full TLS site config.
    sed -e "s|wb.example.com|$DOMAIN|g" \
        -e "s|/opt/whitelist-manager/static|$APP_DIR/static|g" \
        "$SCRIPT_DIR/nginx.sample.conf" > "$NGINX_SITE"
else
    # No domain: emit a minimal plain-HTTP reverse proxy on port 8000.
    cat > "$NGINX_SITE" <<EOF
server {
    listen 8000 default_server;
    listen [::]:8000;
    server_name _;
    client_max_body_size 1m;
    location /static/ { alias $APP_DIR/static/; expires 1h; }
    location / {
        proxy_pass http://127.0.0.1:8000;
        include /etc/nginx/snippets/wb-proxy.conf;
    }
}
EOF
    # If uvicorn also binds 8000, that collides. Tell the user to set WB_PORT.
fi

ln -sfn "$NGINX_SITE" "$NGINX_LINK"
# Remove the default site only if ours is the one taking its place.
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

if ! nginx -t >/dev/null 2>&1; then
    die "nginx config test failed. Inspect: nginx -t"
fi
systemctl reload nginx
ok "nginx configured and reloaded"

# #############################################################################
# 7. Firewall (ufw)
# #############################################################################
log "Configuring firewall (ufw)..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH         >/dev/null 2>&1 || true
    if [[ -n "$DOMAIN" ]]; then
        ufw allow 'Nginx Full' >/dev/null 2>&1 || true   # 80 + 443
    else
        ufw allow 8000/tcp    >/dev/null 2>&1 || true
    fi
    yes | ufw enable >/dev/null 2>&1 || true
    ok "Firewall rules set"
else
    log "ufw not available — skipping firewall"
fi

# #############################################################################
# 8. Let's Encrypt (optional, only with DOMAIN)
# #############################################################################
if [[ -n "$DOMAIN" ]]; then
    log "Requesting TLS certificate for $DOMAIN ..."
    if certbot --nginx -n --redirect \
         --agree-tos -m "$EMAIL" --no-eff-email \
         -d "$DOMAIN"; then
        ok "TLS certificate issued; nginx configured for https"
    else
        log "Certbot failed — nginx is serving on port 80 for now."
        log "Re-run once DNS for $DOMAIN points here, or run:"
        log "  sudo certbot --nginx -d $DOMAIN"
    fi
fi

# #############################################################################
# Done — show next steps
# #############################################################################
echo
ok "================ INSTALL COMPLETE ================"
echo "  App dir:     $APP_DIR"
echo "  Service:     systemctl status $SERVICE_NAME"
echo "  Logs:        journalctl -u $SERVICE_NAME -f"
if [[ -f "$APP_DIR/.env" ]]; then
    echo "  Admin user:  admin   (password: see $APP_DIR/.env)"
fi
if [[ -n "$DOMAIN" ]]; then
    echo "  URL:         https://$DOMAIN"
else
    echo "  URL:         http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
    echo "  (uvicorn still binds 127.0.0.1:8000; nginx fronts 8000. To run"
    echo "   uvicorn on a different port, set WB_PORT in $APP_DIR/.env and"
    echo "   adjust the nginx server{} port above.)"
fi
echo "==================================================="
