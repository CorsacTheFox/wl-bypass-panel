# Android VPN Client — Functional & API Specification

> **Status:** Draft v1.0
> **Scope:** Specification for a native Android client that authenticates via
> Telegram and automatically requests relay instances from this backend.
> The app is *not* built in this stage — this document defines what it must do
> and exactly how it must talk to the service.
>
> This document is written to match the **current** service code in this repo.
> Where the current code needs a small change/refinement to support the app
> cleanly, it is marked **[BACKEND CHANGE]** and listed again in
> [§10. Required backend adjustments](#10-required-backend-adjustments).

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
2. On a successful sign-in the app **automatically requests a relay instance**
   via the backend API using a configurable **"quick link"** (server URL).
3. The app shows the resulting connection status and exposes the standard
   connect / disconnect / status UX.
4. The app can run entirely from a configurable base URL (so the same APK
   works against test/prod servers — no hard-coded host).

### Non-goals (this stage)
- Building the app.
- Re-implementing the WebRTC transport — the app consumes the existing
  `whitelist-bypass` Android runtime / its import format.
- An iOS client.

---

## 3. The two integration flows (choose one per build)

The backend exposes **two** ways for the app to get an instance. Pick the flow
that matches the product:

| | **Flow A — Authenticated client** | **Flow B — Public quick-launch** |
|---|---|---|
| Authenticate | `POST /api/auth/telegram` → bearer token | none |
| Request instance | `POST /api/client/start` | `GET /api/quick/link` or `POST /api/quick/start` |
| Per-user quota | Yes (`max_concurrent`, default 3) | Global cap (`quick_max_concurrent`, default 5) |
| Attribution | Tracked to the Telegram user | All billed to admin (user 1) |
| Privilege required | `can_create_instances=true` on the user | None (public) |
| Returns link async? | Yes — poll `GET /api/client/instances` | `/link` blocks server-side ~30s and returns plain text; `/start` returns the row and you poll `GET /api/quick/status/{id}` |

**Recommendation for an "automatic after Telegram auth" app:** **Flow A**. It
attributes usage to the real user, respects per-user quotas, and lets the
operator grant/revoke access per account. Flow B is simpler but is fully
public and unattributed — suitable only for a no-account "press button, get
VPN" product.

> **[BACKEND CHANGE — see §10.1]** Flow A currently requires `can_create_instances=true`,
> which **defaults to OFF** for Telegram-auto-created users. For the app to
> work out-of-the-box, either (a) the operator grants the flag per user, or
> (b) we add a server setting to auto-grant it. See §10.

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

## 5. Authentication — Telegram sign-in

### 5.1 How Telegram auth works on this backend

- The app must obtain Telegram **`initData`** for the configured bot.
- Two ways to get it:
  1. **Telegram SDK login widget** (recommended for a standalone Android app):
     use the official `TelegramClient` / `Tdlib` or the Telegram
     "Login via URL" flow to produce a signed `initData` for the bot whose
     token is set in `WB_TELEGRAM_BOT_TOKEN`.
  2. **Wrap the app as a Telegram Mini App**: open the bot inside Telegram,
     which injects `initData`. (Heavyweight for a native app; listed for
     completeness.)
- The backend validates `initData` with the official
  HMAC-SHA256(WebAppData, bot_token) algorithm, enforces a replay window
  (`WB_TG_INITDATA_MAX_AGE`, default 24h), and issues an **opaque bearer
  session token** (TTL `WB_SESSION_TTL`, default 12h).
- The same token is used for **all** subsequent `/api/client/*` calls.

> **[BACKEND CHANGE — see §10.2]** The Telegram bot token lives **only** on the
> server (`WB_TELEGRAM_BOT_TOKEN`); the app never needs it. The app only needs
> to know **which bot** to log into (its username), which should be shipped as
> part of the Server Profile or app config. Today there is no endpoint that
> advertises the bot username — see §10.2.

### 5.2 Request

```
POST {server_url}/api/auth/telegram
Content-Type: application/json

{
  "initData": "<url-encoded initData string from Telegram>"
}
```

### 5.3 Responses

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
| 200 | Authenticated | Store `token`; proceed to request instance. |
| 401 | `initData` invalid / expired / hash mismatch | Show "Telegram sign-in failed; retry." Clear any stored initData. |
| 404 | `WB_TELEGRAM_BOT_TOKEN` not set on server | "Server not configured for Telegram login." This is a config error — surface it prominently. |

### 5.4 Account-linking semantics (app must not be surprised by these)

On first sign-in the server resolves the account in this order:
1. Already linked by `telegram_id` → sign in.
2. Local account with the **same username** (case-insensitive) → link & sign in.
3. Otherwise **auto-create** a passwordless client with
   `can_create_instances=false` (default).

Because of (3), a brand-new Telegram user will sign in successfully but get
**403** when requesting an instance until an admin grants the privilege — see
§6.4 and §10.1.

### 5.5 Session & token lifecycle

- Persist `token` in Android `EncryptedSharedPreferences`.
- Treat the token as valid until a call returns **401**; then re-run §5.2 with
  a fresh `initData`. There is no refresh-token mechanism.
- On explicit sign-out, call `POST /api/auth/logout` (best-effort) and drop the
  token locally.

---

## 6. Requesting an instance (Flow A — authenticated client)

### 6.1 Discover available services
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

### 6.2 Check quota / privilege
```
GET {server_url}/api/client/utilization
Authorization: Bearer <token>
```
**200**
```json
{ "active": 1, "max": 3, "remaining": 2 }
```
(`max`/`remaining` are `null` for admin accounts — unlimited.)

### 6.3 Start an instance (the automatic step)
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

### 6.4 Error handling for `/api/client/start`
| Status | `detail` example | App behavior |
|--------|------------------|--------------|
| 403 | `Instance creation is disabled for this account` | **Privilege missing.** Stop auto-retry. Show: "Access not granted yet. Contact the operator." Offer a button to open the operator's Telegram (deep link from Server Profile). |
| 409 | `Concurrent limit reached (3/3)` | Quota full. Offer to stop an existing instance (`POST /api/client/stop/{id}`) and retry. |
| 404 | `service not found` | The `default_service_id` is stale; re-fetch `/api/client/services` and retry with a valid id. |
| 400 | `selected service is disabled` | Same — re-fetch services. |
| 401 | — | Token expired → re-authenticate (§5.2), then retry once. |

### 6.5 Poll for the connection link
The instance is returned with `status:"pending"` and `output_link:null`. The
app must poll until `output_link` is non-null or the instance terminates.

```
GET {server_url}/api/client/instances
Authorization: Bearer <token>
```
Returns the user's **active** instances (newest first), same row shape as §6.3.
The app finds its instance by `id` and inspects `status` + `output_link`.

Polling rules:
- Interval: `poll_interval_seconds` (default 0.5s; 1s is acceptable).
- Give up after `link_wait_timeout_seconds` (default 30s) → show
  "Instance took too long to become ready; try again."
- Terminal statuses that mean **no link will come**:
  `stopped`, `exited`, `crashed`, `timeout`. If reached with no
  `output_link`, surface `error` to the user.

When `output_link` is present → proceed to §8 (handoff to the data plane).

### 6.6 Stop an instance
```
POST {server_url}/api/client/stop/{instance_id}
Authorization: Bearer <token>
```
Returns the updated instance row (terminal `status`). The app may only stop
its **own** instances (enforced server-side via the token's `user_id`).

### 6.7 Auto-start policy
On launch (if signed in and `flow=client`), the app runs:
1. `GET /api/client/utilization`
2. If `remaining > 0` **and** no instance is already `pending/running` with an
   `output_link` → `POST /api/client/start` with `default_service_id` +
   `default_duration_seconds`.
3. Then poll (§6.5) and auto-connect (§8).

Auto-start must be a user-toggleable setting ("Connect on launch", default on).

---

## 7. Requesting an instance (Flow B — public quick-launch)

Use this when the product is "no account, just press the button". The app does
**not** authenticate; instances are created under the admin (user 1).

### 7.1 One-shot link (simplest)
```
GET {server_url}/api/quick/link
```
- **200** `text/plain` → the ready `join_link` (the server blocks up to ~30s).
- **429** → quick-launch cap reached (`quick_max_concurrent`).
- **404** → no enabled service configured.
- **504** → instance started but no link within the server timeout.

This is the closest thing to a literal "quick link": a single GET returns the
connection string. Ideal for an app that just wants to display/connect.

### 7.2 Start + poll (more control)
```
POST {server_url}/api/quick/start      → 201, instance row (id, status, ...)
GET  {server_url}/api/quick/status/{id}
```
`status` returns `{ id, status, output_link, error }`; poll until
`output_link` is set or status is terminal. Same polling rules as §6.5.

> **[BACKEND GAP — see §10.3]** Flow B is **fully public** (no auth, no token,
> no per-user limit beyond the global cap). Anyone who knows `server_url` can
> spawn instances. For an app distributed to end users this is usually
> unacceptable — see §10.3 for a lightweight fix.

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
4. On user disconnect, call `POST /api/client/stop/{id}` (Flow A) so the
   server frees the binary; for Flow B, best-effort — there is no token, so
   the app would need the instance `id` from §7.2 (not available with §7.1).

> **Note:** The transport runtime is **out of scope** for this spec; the app
> integrates the upstream Android client. This section only defines the
> contract at the seam: `output_link` (string) in → live tunnel out.

---

## 9. End-to-end sequences

### 9.1 Flow A (recommended) — happy path
```
App launch
  └─► (no token) obtain Telegram initData for the configured bot
        └─► POST /api/auth/telegram            → 200 { token }
              └─► GET  /api/client/utilization  → 200 { remaining: 3 }
                    └─► POST /api/client/start  → 201 { id:42, status:"pending", output_link:null }
                          └─► poll GET /api/client/instances  (every 0.5s, ≤30s)
                                └─► output_link:"wbstream://…"  → hand off to runtime → CONNECTED
```

### 9.2 Flow A — access not granted
```
POST /api/auth/telegram   → 200 { token }
POST /api/client/start    → 403 "Instance creation is disabled for this account"
  └─► UI: "Access not granted. Contact @operator."  (stop auto-retry)
```

### 9.3 Flow B — happy path (one-shot)
```
GET /api/quick/link  → 200 text/plain "wbstream://…"   → hand off → CONNECTED
```

---

## 10. Required backend adjustments

These are the changes needed so the current service supports the app cleanly.
Each is small and optional relative to the chosen flow.

### 10.1 Auto-grant instance-creation privilege for Telegram users **[Flow A]**
- **Problem:** Telegram-auto-created users get `can_create_instances=false`
  (see `services.py:get_or_create_for_telegram`), so `/api/client/start`
  returns 403. The operator currently must grant per user via Admin UI.
- **Fix (recommended):** add a boolean server setting
  `telegram_auto_can_create` (default `false`) read in
  `get_or_create_for_telegram`. When true, auto-created accounts are created
  with `can_create_instances=true`. Lets the app work out-of-the-box while
  keeping the operator in control via a single toggle.
- **Alternative (no code):** operator bulk-grants via Admin → Grant Access.

### 10.2 Advertise the Telegram bot username **[both flows]**
- **Problem:** the app must know **which bot** to log into, but no endpoint
  exposes it; the bot username is only implicit in `WB_TELEGRAM_BOT_TOKEN`.
- **Fix:** add a small **unauthenticated** discovery endpoint:
  ```
  GET /api/config  →  { "telegram_login_enabled": true,
                        "telegram_bot_username": "wl_cors_bot" }
  ```
  (only the *username*, never the token). The app uses it to drive the
  Telegram login widget and to show a correct "sign in with Telegram" state.

### 10.3 Protect the public quick flow (or drop it) **[Flow B]**
- **Problem:** `/api/quick/*` is fully unauthenticated; anyone with the URL can
  spawn instances up to the global cap.
- **Fix options:**
  - (a) Require an **app API key** header (`X-Api-Key`) checked against a
    server setting — cheap, sufficient to prevent drive-by abuse.
  - (b) Put `/quick` behind the same Telegram bearer token (turns Flow B into
    Flow A effectively).
  - (c) Rate-limit per IP at the nginx layer (already possible via
    `deploy/nginx.sample.conf`).
- **Recommendation:** if Flow B is shipped at all, add (a).

### 10.4 (Optional) A one-shot authenticated quick endpoint
- A convenience endpoint that combines "start + wait for link" under a bearer
  token, returning the `output_link` directly (mirroring `/api/quick/link` but
  attributed to the user). Reduces app round-trips from 3 calls to 1. Low
  priority; the current poll pattern is fine.

---

## 11. Non-functional requirements

- **TLS:** production `server_url` must be HTTPS. The backend binds 127.0.0.1
  and expects a reverse proxy (nginx/Caddy) for TLS — already provided by
  `deploy/install.sh`.
- **Secrets:** the app stores only the bearer `token` (encrypted at rest). It
  never stores bot tokens, cookies, or credentials — none are exposed by the
  API.
- **Timeouts:** all HTTP calls should use connect ≤10s, read ≤35s (the
  `/api/quick/link` path can block ~30s server-side).
- **Retries:** retry only on network errors and 5xx, with backoff. **Never**
  auto-retry 403 (privilege) — that needs human/operator action.
- **Background:** instance lifetime is server-enforced (`timeout_at`); the app
  need not keep a foreground timer, but should stop the instance on user logout
  if a "stop on exit" setting is on.
- **Minimal permissions:** the app should request only the permissions the
  WebRTC runtime needs (network); no SMS, contacts, etc.

---

## 12. Open questions for the operator

1. **Flow A or Flow B?** Determines whether the app has accounts at all.
2. **Default bot username** to ship in the app (or read from `/api/config`,
   §10.2).
3. **Auto-grant Telegram users** (§10.1) — yes/no as a server policy.
4. **Durations to offer** in the app UI (e.g. 1h/4h/8h/12h/24h as in the
   current Mini App) and whether they map to fixed `timeout_seconds` values.
5. Whether the app should bundle the `whitelist-bypass` Android runtime or
   hand off to the standalone installed client (affects §8).
