# Whitelist-Bypass Instance Manager

A lightweight web dashboard to manage instances of a compiled `whitelist-bypass`
binary (an anti-censorship relay tool — same category as Tor bridges / snowflake).
Operators create client accounts; clients log in, pick a pre-configured service,
and launch a backgrounded relay instance. The backend spawns the binary as a
tracked child process, enforces a per-client concurrency cap, and can stop it
gracefully.

**Stack:** Python 3.11+ · FastAPI · aiosqlite (SQLite) · asyncio subprocess ·
single-file vanilla JS + Tailwind SPA.

---

## Quick start

```bash
./run.sh                    # creates .venv, installs deps, runs uvicorn
# or manually:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --port 8000
```

Open http://127.0.0.1:8000 and log in with the bootstrap admin
(default `admin` / `changeme-on-first-login` — override via
`WB_ADMIN_USERNAME` / `WB_ADMIN_PASSWORD`).

### 1. Configure binaries & services (Admin → Services)
Drop the compiled binary into `./binaries/` and make it executable
(`chmod +x binaries/whitelist-bypass`). Then in the Admin UI create a
**Service** pointing at it, with the server-side cookies/session token in the
*Credentials* field. The Process Manager runs:

```
<binary> --cookies <credentials> <extra_args>
```

Adjust `ProcessManager._build_command` in `process_manager.py` if your build
expects different flag names (e.g. `--vk-link`, `--room`, `--write-file`).

### 2. Create a client (Admin → Clients → + New Client)
Give them a username, password, and slot cap (default 3).

### 3. Log in as the client → pick a service → **Start Call**.
The dashboard shows live slot utilization (`2/3`), PID, status, and a **Stop**
button.

---

## Deploy to a server (Ubuntu 22.04 / 24.04)

The `deploy/install.sh` script is **idempotent** (safe to re-run) and handles
the whole production setup: apt packages, dedicated unprivileged service user,
virtualenv, DB init, systemd service, a reverse proxy (your choice of **nginx**
or **Caddy**), firewall, and TLS.

It is **interactive by default** — it asks a handful of questions and accepts
the `[default]` if you just press Enter. Every question also has an **env-var
override**, so the install is fully scriptable for CI.

```bash
# 1. Get the code onto the server (git clone, scp, rsync, ...)
scp -r . ubuntu@your-server:/tmp/whitelist-manager
ssh ubuntu@your-server

# 2. Run the installer as root (interactive):
sudo bash /tmp/whitelist-manager/deploy/install.sh

# ...or drive it entirely from env vars (non-interactive):
# nginx + a domain (Let's Encrypt TLS):
sudo PROXY=nginx DOMAIN=wb.example.com EMAIL=you@example.com \
     bash /tmp/whitelist-manager/deploy/install.sh
# caddy + a domain (caddy obtains TLS automatically):
sudo PROXY=caddy DOMAIN=wb.example.com \
     bash /tmp/whitelist-manager/deploy/install.sh
# no domain — plain HTTP on the external port (LAN / quick test):
sudo PROXY=nginx bash /tmp/whitelist-manager/deploy/install.sh
```

### Questions it asks (and their env-var equivalents)

| Prompt | Env var | Default | Notes |
|--------|---------|---------|-------|
| Reverse proxy (`nginx` / `caddy`) | `PROXY` | `nginx` | caddy auto-obtains TLS |
| Domain for HTTPS | `DOMAIN` | *(blank = HTTP)* | blank → plain HTTP |
| Let's Encrypt email | `EMAIL` | — | required for `nginx` + `DOMAIN` |
| Internal uvicorn port | `APP_PORT` | `8000` | behind the proxy; `WB_PORT` in `.env` |
| External HTTP port | `PUBLIC_PORT` | `80` | only asked when there is no domain |
| Admin username | `ADMIN_USERNAME` | `admin` | |
| Admin password | `ADMIN_PASSWORD` | *auto-generated* | blank at the prompt = generated alphanumeric |

The generated password uses only `[A-Za-z0-9]` (no `+`/`/`/`=`), so it copies
and pastes cleanly.

What the installer does:
1. Installs `python3`, `sqlite3`, `ufw`, and either `nginx` (+ `certbot`) or
   `caddy` (from Caddy's apt repo)
2. Creates a system user `wb-manager` (no shell, no home — least privilege)
3. Installs the app to `/opt/whitelist-manager`, builds the venv, installs deps
4. Writes `/opt/whitelist-manager/.env` (mode 600) and initializes the SQLite DB
5. **Forces the admin password to match `.env`** (see note below)
6. Renders + enables + starts the `wb-manager` systemd service (host/port from
   `.env`, no longer hardcoded)
7. Installs the reverse-proxy config and reloads it
8. Opens firewall ports (80/443 with a domain, else `PUBLIC_PORT`)
9. Requests a Let's Encrypt cert (`nginx` path only — Caddy handles its own TLS)

> **Admin password is re-synced on every run.** The bootstrap admin is created
> only on the *first* run, so editing `WB_ADMIN_PASSWORD` in `.env` later would
> normally have **no effect** — which is a common "I can't log in" trap. To
> avoid it, the installer always re-syncs the admin row to whatever is in
> `.env`. Consequence: **re-running the installer resets the admin password.**
> To change it manually instead, see the cheatsheet below.

**After install:** log in at `https://wb.example.com` (or
`http://<ip>:<PUBLIC_PORT>` without a domain) as the admin user — the password
is printed at the end of the install and stored in
`/opt/whitelist-manager/.env`.

### Operations cheatsheet
```bash
systemctl status wb-manager              # is it up?
systemctl restart wb-manager             # restart after editing .env
journalctl -u wb-manager -f              # live logs
cat /opt/whitelist-manager/.env          # admin password / settings (mode 600)
ls /opt/whitelist-manager/data/app.db    # SQLite database

# nginx:
sudo nginx -t && sudo systemctl reload nginx
# caddy:
sudo systemctl reload caddy

# Change the admin password manually (without re-running install.sh):
sudo -u wb-manager /opt/whitelist-manager/.venv/bin/python - <<'PY'
import os, sys, asyncio
A="/opt/whitelist-manager"; os.chdir(A); sys.path.insert(0, A)
for line in open(f"{A}/.env"):
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from db import db; from security import hash_password
NEWPW = "your-new-password-here"
async def m():
    await db.connect()
    await db.execute(
        "UPDATE users SET password_hash=?, role='admin', enabled=1 WHERE username=?",
        (hash_password(NEWPW), os.environ.get("WB_ADMIN_USERNAME","admin")))
    await db.close()
asyncio.run(m())
print("admin password updated")
PY
# and update .env to match:
sudo sed -i "s|^WB_ADMIN_PASSWORD=.*|WB_ADMIN_PASSWORD=your-new-password-here|" /opt/whitelist-manager/.env
sudo systemctl restart wb-manager
```

### Updating the app
Re-run the installer — it rsyncs fresh code, reinstalls deps, and restarts the
service, while leaving `.env` and `data/` untouched (**except it re-syncs the
admin password from `.env`**):
```bash
sudo bash /tmp/whitelist-manager/deploy/install.sh
```

### Deployment files
| File | Purpose |
|------|---------|
| `deploy/install.sh` | One-shot idempotent installer (Ubuntu/Debian) |
| `deploy/wb-manager.service` | systemd unit template (host/port templated, hardened) |
| `deploy/nginx.sample.conf` | nginx site: TLS, rate limits, static serving, proxy |
| `deploy/wb-proxy.snippet.conf` | Shared `proxy_set_header` snippet |
| `.env.example` | Environment template (written into `.env` on first install) |

---

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `WB_HOST` / `WB_PORT` | `127.0.0.1` / `8000` | Bind address |
| `WB_ADMIN_USERNAME` / `WB_ADMIN_PASSWORD` | `admin` / `changeme-on-first-login` | Bootstrap admin (created on first run only) |
| `WB_DATA_DIR` | `./data` | SQLite db + runtime data |
| `WB_BINARIES_DIR` | `./binaries` | Where compiled binaries live |
| `WB_DEFAULT_MAX_CONCURRENT` | `3` | Default per-client slot cap |
| `WB_DEFAULT_TIMEOUT_SECONDS` | `3600` | Auto-stop an instance after N seconds |
| `WB_KILL_GRACE_SECONDS` | `5` | SIGTERM → wait → SIGKILL grace |
| `WB_SESSION_TTL` | `43200` (12h) | Login token lifetime |

---

## Architecture

```
main.py              FastAPI app, lifespan, SPA serving
config.py            all settings (env-overridable)
db.py                SQLite schema + async wrapper (singleton)
security.py          PBKDF2 hashing, tokens, auth dependencies
process_manager.py   ★ spawn / track / reap / kill / timeout  (the core)
services.py          business logic + the Remnawave extensibility seam
routers/
  auth.py            login / logout / me
  admin.py           CRUD clients + services + live overview
  client.py          utilization / list / start / stop
static/index.html    single-page dark UI
```

### How the Process Manager prevents zombies & leaks
- Each spawned child runs in its own **process group** (`start_new_session=True`),
  so signals target the whole tree, not just the top binary.
- A dedicated **waiter task** `await proc.wait()`s every child — asyncio reaps
  it the instant it exits, so no `waitpid` and no zombies.
- Stop sequence: **SIGTERM → grace period → SIGKILL**.
- A **reaper loop** reconciles the in-memory live set against the DB every few
  seconds and on shutdown stops everything still alive.
- On **server restart**, any DB rows left in `running/pending/stopping` are
  marked `crashed` (their PIDs are no longer our children) — the UI never lies.

### Concurrency limit (max 3)
Enforced in `InstanceService.start` by counting rows with a live status for
that user before inserting a new one; returns HTTP **409** if exceeded. The cap
is per-user (`users.max_concurrent`), configurable from the Admin UI.

### Security notes
- Passwords hashed with PBKDF2-HMAC-SHA256 (200k iterations), constant-time compare.
- Bearer-token sessions with expiry; stored in SQLite, revocable on logout.
- Client routes derive `user_id` from the token — a client can **only** ever see
  or stop their own instances.
- Bind to `127.0.0.1` by default. Put it behind a TLS-terminating reverse proxy
  (nginx/Caddy) before exposing it on a network.

---

## Extensibility — plugging in a Remnawave webhook

All user creation flows through `UserService.create_client(...)` in
`services.py`. To auto-create clients from a Remnawave webhook later, add a
router that calls exactly that method — no changes to the admin UI, DB, or
process manager are needed:

```python
# routers/remnawave.py (future)
from fastapi import APIRouter, Header, HTTPException
from services import user_service
router = APIRouter(prefix="/api/webhooks/remnawave")

@router.post("/user-created")
async def on_user_created(payload: dict, x_signature: str = Header(...)):
    if not verify(x_signature, payload):               # your HMAC check
        raise HTTPException(403)
    return await user_service.create_client(
        username=payload["username"],
        password=payload["temp_password"],
        external_ref=payload["uuid"],                  # de-dup key
    )
```

The `users.external_ref` UNIQUE column prevents duplicate clients from repeated
webhook deliveries.

---

## REST API summary

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/auth/login` | — | Get bearer token |
| POST | `/api/auth/logout` | any | Revoke token |
| GET  | `/api/auth/me` | any | Current user |
| GET  | `/api/admin/clients` | admin | List clients |
| POST | `/api/admin/clients` | admin | Create client |
| PATCH| `/api/admin/clients/{id}` | admin | Update client |
| DELETE | `/api/admin/clients/{id}` | admin | Delete client (+ stop instances) |
| GET/POST/PATCH/DELETE | `/api/admin/services[/{id}]` | admin | Manage services |
| GET  | `/api/admin/overview` | admin | All live instances |
| GET  | `/api/client/services` | client | Available services |
| GET  | `/api/client/utilization` | client | Slot usage (`{active, max, remaining}`) |
| GET  | `/api/client/instances` | client | Own active instances |
| POST | `/api/client/start` | client | Start a call (409 if at cap) |
| POST | `/api/client/stop/{id}` | client | Stop own instance |
| GET  | `/api/health` | — | Liveness + live process count |

---

## License & use

This is operator tooling for a legitimate anti-censorship relay. Use in
compliance with your local laws. The repository does not distribute the
`whitelist-bypass` binary itself — obtain releases from upstream.
