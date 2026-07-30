# Android VPN Client — Functional & API Specification

> **Status:** Draft v1.1
> **Scope:** Specification for a native Android client that authenticates via
> Telegram and automatically requests relay instances from this backend.
> The app is *not* built in this stage — this document defines what it must do
> and exactly how it must talk to the service.
>
> This document is written to match the **current** service code in this repo.
> Where the current code needs a small change/refinement to support the app
> cleanly, it is marked **[BACKEND CHANGE]** and listed again in
> [§10. Required backend adjustments](#10-required-backend-adjustments).
>
> **What's new in v1.1:** the **authorization model (Telegram)** and a
> **connection-lifecycle link model** are now fully specified and implemented:
>
> - An **unauthorized** client receives a regular short temporary link (the
>   public quick flow) — see [§5](#5-authorization--telegram-sign-in) and
>   [§7](#7-requesting-an-instance-flow-b--public-quick-launch).
> - An **authorized** (Telegram) client gets a link that lives for the **whole
>   connection**: the app issues `GET /api/app/connect` on connect and
>   `GET /api/app/disconnect` on disconnect, for the **same** link — see
>   [§6](#6-connection-lifecycle--link-model-authorized-client).

---

## 1. What the service actually is

Before specifying the app, the team must share one accurate mental model. This
is **not** a classic VPN (OpenVPN/WireGuard). It is a **WebRTC-traffic-tunneling
relay** (the `whitelist-bypass` family):

- The server runs a compiled binary ("creator") per user session.
- The binary negotiates a WebRTC session that *looks like* an ordinary browser
  video call to the ISP/DPI, and tunnels the user's traffic inside it.
- When the binary is ready it prints a line of the form:

  ```
  join_link: <scheme>://<payload>
  ```
  e.g. `wbstream://019ed925-...`, `dion.vc/...`, `https://vk.com/call/...`.

- That **`join_link`** is the connection string the Android client must import
  and connect to. It is stored on the server as `instances.output_link`.

**Implication for the app:** the app has two halves:

1. A **control plane** — talks to this backend over HTTPS/JSON to authenticate
   and request instances.
2. A **data plane** — the actual `whitelist-bypass` Android client runtime
   (the existing open-source app from
   `github.com/kulikov0/whitelist-bypass/releases`) which consumes the
   `join_link` and carries traffic. This spec owns the **control plane** and
   the **handoff** of the `join_link` to the data-plane runtime; it does **not**
   re-implement the WebRTC transport.

---

## 2. Goals & non-goals

### Goals
1. User opens the Android app and signs in **with Telegram** (one tap, no
   password).
2. On a successful sign-in the app is **authorized** and **automatically
   connects** — it calls `GET /api/app/connect`, gets a link that lives for the
   whole connection, and hands it to the relay runtime. The app calls
   `GET /api/app/disconnect` when the user disconnects.
3. The app shows the resulting connection status and exposes the standard
   connect / disconnect / status UX.
4. The app can run entirely from a configurable base URL (the "quick link" —
   server URL) so the same APK works against test/prod servers (no hard-coded
   host).
5. An **unauthorized** client (not signed in) can still get a regular short
   temporary link from the public quick flow, like any anonymous caller.

### Non-goals (this stage)
- Building the app.
- Re-implementing the WebRTC transport — the app consumes the existing
  `whitelist-bypass` Android runtime / its import format.
- An iOS client.

---

## 3. The two integration flows (choose one per build)

The backend exposes **two** ways for the app to get a link. Pick the flow that
matches the product:

| | **Authorized — Telegram sign-in** | **Unauthorized — public quick-launch** |
|---|---|---|
| Authenticate | `POST /api/auth/telegram` → bearer token (§5) | none |
| Get the link | `GET /api/app/connect` (§6) — one link per connection | `GET /api/quick/link` (§7) — a regular temporary link |
| Stop / release | `GET /api/app/disconnect` (§6) | none (expires after 15 min) |
| Link lifetime | the whole connection (default 24h) | 15 minutes, shared/global |
| Per-user quota | Yes (`max_concurrent`, default 3) | Global cap (`quick_max_concurrent`, default 5) |
| Attribution | Tracked to the Telegram user | All billed to admin (user 1) |
| Privilege required | `can_create_instances=true` (auto-grantable, §5.5) | None (optionally `X-Api-Key`, §7.3) |
| Returns link async? | `connect` blocks ~30s then returns it | `/link` blocks ~30s and returns plain text |

**Recommendation for an "automatic after Telegram auth" app:** the
**authorized** flow (§5–§6). It attributes usage to the real user, respects
per-user quotas, lets the operator grant/revoke access per account, and gives a
link that lasts the whole connection. The unauthorized flow (§7) is simpler but
unattributed — suitable only for a no-account "press button, get a link"
product; it hands every caller the same kind of short temporary link.

---

## 4. Configuration the app holds

The app stores a small, user-editable **Server Profile** (the "configurable
quick link"):

| Field | Example | Required | Notes |
|-------|---------|----------|-------|
| `server_url` | `https://wl.example.com` | yes | Base URL, no trailing slash. Must be HTTPS in production. |
| `flow` | `client` / `quick` | yes | Which integration flow (§3). Default `client`. |
| `default_service_id` | `2` or `0` | no | Preselected service; `0`/absent = server default / first enabled. |
| `default_duration_seconds` | `3600` | no | Instance lifetime; clamped to server policy. |
| `poll_interval_seconds` | `0.5` | no | How often to poll for the `output_link`. Default 0.5s. |
| `link_wait_timeout_seconds` | `30` | no | Give up waiting for the link after this. Default 30s (matches server). |

The `server_url` **is** the "quick link": entering it is the only setup step.
A deep link / QR import of the form
`wlapp://add?server=https://wl.example.com&flow=client` should also populate
the profile (nice-to-have).

---

## 5. Authorization & Telegram sign-in

Authorization is what distinguishes the two client types in this spec:

- **Authorized client** — signed in with Telegram. Gets a link that lives for
  the whole connection via the dedicated connect/disconnect endpoints (§6).
- **Unauthorized client** — never signs in. Gets a regular short temporary link
  from the public quick flow (§7), exactly like any other anonymous caller.

### 5.1 How Telegram auth works on this backend

- The app must obtain Telegram **`initData`** for the configured bot.
- Two ways to get it:
  1. **Mini App initData (recommended for a standalone Android app):** open the
     bot inside Telegram (e.g. via an in-app browser / Custom Tab over the
     bot's `https://t.me/<bot>/` start URL, or the Telegram SDK), which injects
     a signed `initData` for the bot whose token is set in
     `WB_TELEGRAM_BOT_TOKEN`. The app forwards that string to the backend.
  2. **Telegram Login widget:** use Telegram's standalone OAuth "Login via URL"
     flow to produce a signed `initData`. More integration work; same backend
     endpoint. Listed for completeness.
- The backend validates `initData` with the official
  HMAC-SHA256(WebAppData, bot_token) algorithm, enforces a replay window
  (`WB_TG_INITDATA_MAX_AGE`, default 24h), and issues an **opaque bearer
  session token** (TTL `WB_SESSION_TTL`, default 12h).
- The same token is used for **all** subsequent authorized calls
  (`/api/client/*` and the new `/api/app/*` connect/disconnect endpoints).
- The bot token lives **only** on the server; the app never needs it.

### 5.2 Discover whether Telegram login is available
Before showing a "Sign in with Telegram" button, the app calls the **public**
discovery endpoint to learn whether login is enabled and **which bot** to open:

```
GET {server_url}/api/config
```
**200**
```json
{
  "telegram_login_enabled": true,
  "telegram_bot_username": "wl_cors_bot"
}
```
If `telegram_login_enabled` is `false`, the server has no bot token configured
(sign-in will return 404) — show an "unavailable" state. If
`telegram_bot_username` is empty, the bot must be configured out-of-band (ship
it in the app / Server Profile).

> Implemented in `routers/public.py`. Only the **username** is ever returned —
> never the token. (Resolves the old §10.2 gap.)

### 5.3 Request — Telegram sign-in
```
POST {server_url}/api/auth/telegram
Content-Type: application/json

{
  "initData": "<url-encoded initData string from Telegram>"
}
```

### 5.4 Responses
**200 OK**
```json
{
  "token": "PbkZ...opaque...string",
  "role": "client",
  "username": "tg_12345678",
  "must_change_password": false
}
```

| Status | Meaning | App behavior |
|--------|---------|--------------|
| 200 | Authenticated | Store `token`; the client is now **authorized** → use §6. |
| 401 | `initData` invalid / expired / hash mismatch | Show "Telegram sign-in failed; retry." Clear any stored initData. |
| 404 | `WB_TELEGRAM_BOT_TOKEN` not set on server | "Server not configured for Telegram login." Surface prominently; fall back to the **unauthorized** flow (§7) if the product allows it. |

### 5.5 Account-linking semantics (app must not be surprised by these)
On first sign-in the server resolves the account in this order:
1. Already linked by `telegram_id` → sign in.
2. Local account with the **same username** (case-insensitive) → link & sign in.
3. Otherwise **auto-create** a passwordless client.

A new account's `can_create_instances` flag is set from the server policy:
- **`telegram_auto_can_create = true`** (recommended for this app): the
  auto-created account can launch instances immediately — the app works
  out-of-the-box.
- **`false` (default):** the account is created with the privilege **off**, so
  `GET /api/app/connect` (and `/api/client/start`) will return **403** until an
  admin grants it. Surface: "Access not granted yet. Contact the operator."

This setting is tunable at runtime via Admin → Settings
(`PUT /api/admin/settings`, field `telegram_auto_can_create`) and via the
`WB_TELEGRAM_AUTO_CAN_CREATE` env var. (Resolves the old §10.1 gap.)

### 5.6 Session & token lifecycle
- Persist `token` in Android `EncryptedSharedPreferences`.
- Treat the token as valid until a call returns **401**; then re-run §5.3 with
  a fresh `initData`. There is no refresh-token mechanism.
- On explicit sign-out, call `POST /api/auth/logout` (best-effort) and drop the
  token locally. It is good practice to call `GET /api/app/disconnect` first so
  the server frees the running instance (see §6).

---

## 6. Connection lifecycle & link model (authorized client)

This is the core of the "authorized client gets a link for the whole time
they're connected" requirement. The app drives the connection with **two
separate GET requests** — one on connect, one on disconnect — for the **same**
link.

> All endpoints in this section require the Telegram bearer token
> (`Authorization: Bearer <token>` from §5).

### 6.1 Model
- On **connect**, the app calls `GET /api/app/connect`. The server starts a
  **long-lived instance** attributed to the authenticated user and returns its
  `output_link`. That link is valid for the whole connection
  (`app_default_timeout_seconds`, default **24h**, tunable via Admin Settings /
  `WB_APP_DEFAULT_TIMEOUT_SECONDS`).
- The instance is tagged `app_session=1` so the server can track it as the
  user's app connection (distinct from public quick sessions or web sessions).
- On **disconnect**, the app calls `GET /api/app/disconnect`. The server stops
  the user's live app-session instance, freeing the binary. Disconnect is
  **idempotent** (a repeat does nothing).
- **Reconnect / relaunch while connected:** if the app calls `/connect` again
  while its instance is still alive and already has a link, the server returns
  the **same** `output_link` (and `instance_id`) instead of starting a new one.
  This is what guarantees "the same link for the whole connection."

### 6.2 Connect — request
```
GET {server_url}/api/app/connect?service_id=2
Authorization: Bearer <token>
```
`service_id` is **optional**. Omit to use the server default (the admin-
configured quick service, else the first enabled service — same preference as
the public quick flow). The call **blocks server-side up to ~30s** while the
binary prints its `join_link`, then returns it.

### 6.3 Connect — responses
**200 OK**
```json
{
  "instance_id": 42,
  "output_link": "wbstream://019ed925-...",
  "status": "running",
  "expires_at": "2026-07-30T11:00:00+00:00"
}
```
`expires_at` is the instance's `timeout_at` (when the long-lived session will
auto-expire if not disconnected first). Pass `output_link` to the data plane
(§8).

| Status | `detail` example | App behavior |
|--------|------------------|--------------|
| 200 | link ready | Store `instance_id`; hand `output_link` to the runtime (§8). |
| 401 | — | Token expired → re-authenticate (§5.3), then retry once. |
| 403 | `Instance creation is disabled for this account` | Privilege missing (§5.5). Stop auto-retry; "Access not granted. Contact @operator." |
| 404 | `no enabled services available` | Server misconfiguration. |
| 409 | `Concurrent limit reached (3/3)` | Quota full. Offer to disconnect first (`GET /api/app/disconnect`) and retry. |
| 504 | `instance started but produced no link within the timeout` | The binary was slow; "Instance took too long; try again." |
| 502 | `instance ended without producing a link` | Binary crashed; surface `error`. |

### 6.4 Disconnect — request & response
```
GET {server_url}/api/app/disconnect
Authorization: Bearer <token>
```
**200 OK**
```json
{
  "stopped": [42],
  "instance_id": 42
}
```
`stopped` is the list of app-session instance ids that were actually stopped
this call; it is `[]` (and `instance_id` `null`) when nothing was live — a
duplicate or late disconnect is safe.

### 6.5 Quota & privilege (authorized clients are still bounded)
Authorized app users are ordinary users and remain subject to:
- **`max_concurrent`** — connect returns **409** if the cap is full (stop an
  existing instance first).
- **`can_create_instances`** — connect returns **403** if not granted
  (see §5.5). A brand-new Telegram user may hit this until `telegram_auto_can_create`
  is on or an admin grants the privilege.

### 6.6 Authorized vs unauthorized clients — at a glance
| | **Authorized (Telegram)** | **Unauthorized** |
|---|---|---|
| Authenticates | `POST /api/auth/telegram` → bearer token | never |
| Gets the link via | `GET /api/app/connect` (§6) | `GET /api/quick/link` (§7) |
| Link lifetime | the whole connection (default 24h), server-stopped on `GET /api/app/disconnect` | short (15 min), shared/global |
| Attribution | tracked to the Telegram user | billed to admin (user 1) |
| Per-user quota | yes (`max_concurrent`) | global cap only (`quick_max_concurrent`) |
| Same link on reconnect? | yes (resume-active) | n/a (each GET is a fresh 15-min link) |
| Access control | `can_create_instances` + token | optional `X-Api-Key` (§7.3) |

### 6.7 End-to-end (authorized, happy path)
```
App launch
  └─► (no token) GET /api/config                 → 200 { telegram_login_enabled, telegram_bot_username }
        └─► open bot in Telegram → obtain initData
              └─► POST /api/auth/telegram        → 200 { token }
                    └─► GET /api/app/connect      → 200 { instance_id, output_link, expires_at }   (≤ ~30s)
                          └─► hand output_link to runtime → CONNECTED
                                └─► (user leaves / app disconnects)
                                      └─► GET /api/app/disconnect → 200 { stopped:[42] }   → server frees binary
```

> The app may also use the lower-level `/api/client/*` endpoints from §9 if it
> needs manual service selection or history; the connect/disconnect pair in §6
> is the recommended, purpose-built path for "one link per connection".

---

## 7. Requesting an instance — unauthorized client (public quick-launch)

The **unauthorized** client never signs in. It receives a regular temporary
link from the public quick flow — the same short link anyone else would get.

### 7.1 One-shot link (the regular temporary link)
```
GET {server_url}/api/quick/link
```
- **200** `text/plain` → a ready `join_link` (the server blocks up to ~30s).
- **429** → quick-launch cap reached (`quick_max_concurrent`).
- **404** → no enabled service configured.
- **504** → instance started but no link within the server timeout.

This is a single GET that returns the connection string — a 15-minute,
unattributed, shared temporary instance under the admin (user 1). Ideal for a
"press button, get a link" experience with no account.

### 7.2 Start + poll (more control)
```
POST {server_url}/api/quick/start      → 201, instance row (id, status, ...)
GET  {server_url}/api/quick/status/{id}
```
`status` returns `{ id, status, output_link, error }`; poll until
`output_link` is set or status is terminal. Polling rules as in §9.5.

> Because this flow has no token, the app cannot reliably stop the instance on
> disconnect — it simply expires after 15 minutes. (If stop-on-exit is needed,
> use the authorized flow in §6.)

### 7.3 Guarding the public flow (operator choice)
By default `/api/quick/*` is open. To prevent anonymous abuse on a deployed
server, the operator sets `WB_QUICK_TOKEN`; when set, every quick call must
present it as the `X-Api-Key` header **or** `?token=` query param, else **401**:
```
GET {server_url}/api/quick/link
X-Api-Key: <token>          # or: /api/quick/link?token=<token>
```
Leave `WB_QUICK_TOKEN` empty to keep the flow fully public (dev). The same
guard applies to `/api/quick/start`, `/api/quick/status/{id}` and the `/quick`
HTML page. (Resolves the old §10.3 gap.)

---

## 8. Handoff: `output_link` → the WebRTC runtime

Once the app has a non-null `output_link`, it must launch/connect the actual
relay. The link scheme varies by service (`wbstream://`, `dion.vc/…`,
`https://vk.com/call/…`, `tm://`, …).

App responsibilities:
1. Treat `output_link` as an **opaque connection string** — do not parse it.
2. Pass it to the bundled `whitelist-bypass` Android runtime via its supported
   import/URI mechanism (the same UI flow as "add connection from clipboard"
   in the existing client).
3. Show connection state derived from the runtime, and keep the instance row's
   `status`/`timeout_at` for display (e.g. "Session ends in 47 min").
4. On user disconnect, call `GET /api/app/disconnect` (authorized, §6) so the
   server frees the binary; for the unauthorized flow there is no token, so the
   instance simply expires after 15 minutes (the app would need the instance
   `id` from §7.2 only if it wanted to poll, not available with §7.1).

> **Note:** The transport runtime is **out of scope** for this spec; the app
> integrates the upstream Android client. This section only defines the
> contract at the seam: `output_link` (string) in → live tunnel out.

---

## 9. Low-level client API (optional manual control)

The connect/disconnect pair in §6 is the recommended path for the app. If the
app instead wants manual service selection, history, or fine-grained control,
it can use the generic authenticated client endpoints below. All require the
Telegram bearer token. These are the same endpoints the web dashboard uses.

### 9.1 Discover available services
```
GET {server_url}/api/client/services
Authorization: Bearer <token>
```
**200**
```json
[
  { "id": 2, "name": "wbstream-eu", "enabled": true }
]
```
Credentials/cookies are intentionally **not** returned. The app only needs
`id` and `name` (to label the connection).

### 9.2 Check quota / privilege
```
GET {server_url}/api/client/utilization
Authorization: Bearer <token>
```
**200**
```json
{ "active": 1, "max": 3, "remaining": 2 }
```
(`max`/`remaining` are `null` for admin accounts — unlimited.)

### 9.3 Start an instance
```
POST {server_url}/api/client/start
Authorization: Bearer <token>
Content-Type: application/json

{
  "service_id": 2,
  "timeout_seconds": 3600
}
```
`timeout_seconds` is optional; omit to use the server default
(`WB_DEFAULT_TIMEOUT_SECONDS`, 1h). The server clamps/enforces policy.

**201 Created** — returns the instance row immediately, **before** the
`output_link` exists:
```json
{
  "id": 42,
  "user_id": 7,
  "service_id": 2,
  "service_name": "wbstream-eu",
  "pid": 12345,
  "status": "pending",
  "started_at": "2026-07-29T10:00:00",
  "ended_at": null,
  "exit_code": null,
  "error": null,
  "timeout_at": "2026-07-29T11:00:00+00:00",
  "output_link": null
}
```

### 9.4 Error handling for `/api/client/start`
| Status | `detail` example | App behavior |
|--------|------------------|--------------|
| 403 | `Instance creation is disabled for this account` | **Privilege missing.** Stop auto-retry. "Access not granted. Contact the operator." |
| 409 | `Concurrent limit reached (3/3)` | Quota full. Stop an existing instance (`POST /api/client/stop/{id}`) and retry. |
| 404 | `service not found` | `default_service_id` is stale; re-fetch `/api/client/services`. |
| 400 | `selected service is disabled` | Same — re-fetch services. |
| 401 | — | Token expired → re-authenticate (§5.3), then retry once. |

### 9.5 Poll for the connection link
The instance is returned with `status:"pending"` and `output_link:null`. Poll
until `output_link` is non-null or the instance terminates.
```
GET {server_url}/api/client/instances
Authorization: Bearer <token>
```
Returns the user's **active** instances (newest first), same row shape as §9.3.
Find the instance by `id` and inspect `status` + `output_link`.

Polling rules:
- Interval: `poll_interval_seconds` (default 0.5s; 1s is acceptable).
- Give up after `link_wait_timeout_seconds` (default 30s) → "Instance took too
  long to become ready; try again."
- Terminal statuses that mean **no link will come**:
  `stopped`, `exited`, `crashed`, `timeout`. If reached with no
  `output_link`, surface `error`.

When `output_link` is present → proceed to §8 (handoff).

### 9.6 Stop an instance
```
POST {server_url}/api/client/stop/{instance_id}
Authorization: Bearer <token>
```
Returns the updated instance row (terminal `status`). The app may only stop
its **own** instances (enforced server-side via the token's `user_id`).

---

## 10. End-to-end sequences

### 10.1 Authorized client — happy path (recommended)
```
App launch
  └─► (no token) GET /api/config                 → 200 { telegram_login_enabled, telegram_bot_username }
        └─► open bot in Telegram → obtain initData
              └─► POST /api/auth/telegram        → 200 { token }
                    └─► GET /api/app/connect      → 200 { instance_id, output_link, expires_at }  (≤ ~30s)
                          └─► hand output_link to runtime → CONNECTED
                                └─► GET /api/app/disconnect → 200 { stopped:[..] }
```

### 10.2 Authorized client — access not granted
```
POST /api/auth/telegram   → 200 { token }
GET  /api/app/connect      → 403 "Instance creation is disabled for this account"
  └─► UI: "Access not granted. Contact @operator."  (stop auto-retry)
```

### 10.3 Authorized client — reconnect keeps the same link
```
GET /api/app/connect  → 200 { instance_id: 42, output_link: "wbstream://…" }   (CONNECTED)
  ... app killed & relaunched while instance still alive ...
GET /api/app/connect  → 200 { instance_id: 42, output_link: "wbstream://…" }   (SAME link, no new instance)
```

### 10.4 Unauthorized client — one-shot
```
GET /api/quick/link  → 200 text/plain "wbstream://…"   → hand off → CONNECTED   (expires in 15 min)
```

---

## 11. Required backend adjustments

These were the changes needed so the service supports the app cleanly. **All
are implemented in this revision** (v1.1); pointers to the code are below.

### 11.1 Auto-grant instance-creation privilege for Telegram users — **DONE**
- **Was:** Telegram-auto-created users got `can_create_instances=false`, so
  `/api/app/connect` and `/api/client/start` returned 403.
- **Implemented:** server policy `telegram_auto_can_create`
  (`config.TELEGRAM_AUTO_CAN_CREATE` / `WB_TELEGRAM_AUTO_CAN_CREATE`, also
  runtime-tunable via Admin Settings). When on, auto-created accounts get the
  privilege so the app works out-of-the-box. Read in
  `services.UserService.get_or_create_for_telegram`. See §5.5.

### 11.2 Advertise the Telegram bot username — **DONE**
- **Was:** no endpoint exposed which bot to log into.
- **Implemented:** `GET /api/config → { telegram_login_enabled, telegram_bot_username }`
  in `routers/public.py` (username from `WB_TELEGRAM_BOT_USERNAME`; only the
  username, never the token). See §5.2.

### 11.3 Protect the public quick flow — **DONE**
- **Was:** `/api/quick/*` was fully unauthenticated.
- **Implemented:** when `WB_QUICK_TOKEN` is set, all quick calls require
  `X-Api-Key` (or `?token=`), else 401; unset stays open for dev. Guard lives
  in `routers/quick._require_quick_token` and also covers the `/quick` page.
  See §7.3.

### 11.4 Connection-lifecycle endpoints (authorized client) — **DONE**
- **Added:** `GET /api/app/connect` and `GET /api/app/disconnect`
  (`routers/app.py`), backed by `InstanceService.start_or_resume_active` /
  `stop_app_session` in `services.py`, with the `instances.app_session` flag
  (`db.py` migration) for attribution and idempotent reconnect. See §6.

### 11.5 (Optional) One-shot authenticated quick endpoint — superseded
- The connect endpoint in §6 already provides a single call that returns the
  `output_link` directly under the user's token, so this is no longer needed.

---

## 12. Non-functional requirements

- **TLS:** production `server_url` must be HTTPS. The backend binds 127.0.0.1
  and expects a reverse proxy (nginx/Caddy) for TLS — already provided by
  `deploy/install.sh`.
- **Secrets:** the app stores only the bearer `token` (encrypted at rest). It
  never stores bot tokens, cookies, or credentials — none are exposed by the
  API.
- **Timeouts:** all HTTP calls should use connect ≤10s, read ≤35s (the
  `/api/quick/link` and `/api/app/connect` paths can block ~30s server-side).
- **Retries:** retry only on network errors and 5xx, with backoff. **Never**
  auto-retry 403 (privilege) — that needs human/operator action.
- **Background:** instance lifetime is server-enforced (`timeout_at`); the app
  need not keep a foreground timer, but **should call `GET /api/app/disconnect`
  (authorized) on exit/logout** so the server frees the binary. For the
  unauthorized flow there is no token, so the instance simply expires.
- **Minimal permissions:** the app should request only the permissions the
  WebRTC runtime needs (network); no SMS, contacts, etc.

---

## 13. Open questions for the operator

1. **Authorized-only, unauthorized-only, or both?** If the app must support
   Telegram sign-in, ship §6 (authorized). A pure no-account app uses §7 only.
2. **Default bot username** to ship in the app (or read from `/api/config`,
   §5.2).
3. **Auto-grant Telegram users** (`telegram_auto_can_create`, §5.5) — yes/no as
   a server policy.
4. **Connection duration** the authorized link should live for
   (`app_default_timeout_seconds`, default 24h) — and whether the app offers a
   duration picker that maps to a chosen `timeout_seconds` via the low-level
   `/api/client/start` (§9).
5. Whether the app should bundle the `whitelist-bypass` Android runtime or
   hand off to the standalone installed client (affects §8).
