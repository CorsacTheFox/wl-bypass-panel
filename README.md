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
virtualenv, DB init, systemd service, nginx (TLS + rate limits), firewall, and
optional Let's Encrypt certificate.

```bash
# 1. Get the code onto the server (git clone, scp, rsync, ...)
scp -r . ubuntu@your-server:/tmp/whitelist-manager
ssh ubuntu@your-server

# 2. Run the installer as root.
#    With a domain -> full HTTPS via Let's Encrypt:
sudo DOMAIN=wb.example.com EMAIL=you@example.com \
     bash /tmp/whitelist-manager/deploy/install.sh

#    Without a domain -> plain HTTP on :8000 (good for a quick test / LAN):
sudo bash /tmp/whitelist-manager/deploy/install.sh
```

That single command:
1. Installs `python3`, `nginx`, `sqlite3`, `ufw`, `certbot` (+ nginx plugin)
2. Creates a system user `wb-manager` (no shell, no home — least privilege)
3. Installs the app to `/opt/whitelist-manager`, builds the venv, installs deps
4. Seeds `/opt/whitelist-manager/.env` (copy of `.env.example`, with a
   **randomly-generated admin password**) and initializes the SQLite DB
5. Installs + enables + starts the `wb-manager` systemd service
6. Installs the nginx site (`nginx.sample.conf`) + shared `wb-proxy.conf` snippet
7. Opens firewall ports (80/443 with a domain, else 8000)
8. Issues a Let's Encrypt cert and configures nginx for HTTPS + redirect

**After install:** log in at `https://wb.example.com` (or `http://<ip>:8000`)
as `admin` — the password is printed during install and stored in
`/opt/whitelist-manager/.env`.

### Operations cheatsheet
```bash
systemctl status wb-manager              # is it up?
systemctl restart wb-manager             # restart after editing .env
journalctl -u wb-manager -f              # live logs
sudo nginx -t && sudo systemctl reload nginx   # after nginx changes
cat /opt/whitelist-manager/.env          # admin password / settings
ls /opt/whitelist-manager/data/app.db    # SQLite database
```

### Updating the app
Re-run the installer — it rsyncs fresh code, reinstalls deps, and restarts
the service, while leaving `.env` and `data/` untouched:
```bash
sudo bash /tmp/whitelist-manager/deploy/install.sh
```

### Deployment files
| File | Purpose |
|------|---------|
| `deploy/install.sh` | One-shot idempotent installer (Ubuntu/Debian) |
| `deploy/wb-manager.service` | systemd unit template (dedicated user, hardening, restart) |
| `deploy/nginx.sample.conf` | nginx site: TLS, rate limits, static serving, proxy |
| `deploy/wb-proxy.snippet.conf` | Shared `proxy_set_header` snippet |
| `.env.example` | Environment template (copied to `.env` on first install) |

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
