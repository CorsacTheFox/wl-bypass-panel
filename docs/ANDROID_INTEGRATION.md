# Android App Integration Guide

This document describes how the Android application integrates with the
Whitelist-Bypass service to create and authorize instances.

A machine-readable OpenAPI spec lives alongside this file at
[`android_api.yaml`](./android_api.yaml) — import it into your HTTP client or
code generator (e.g. OpenAPI Generator) for typed models.

---

## 1. Overview

Every time the app connects, it asks the server to **create a temporary
instance**. The instance is a real running process that already exposes the
target service; the user authorizes **inside** that service during a short
(10-minute) window. Once the user has authenticated via **Telegram**, the app
**claims** the instance: ownership transfers to the user, the running process is
kept (so the authorization done inside it is preserved), and the lifetime is
extended to the full default (1 hour). When the user disconnects, the app
explicitly stops the instance.

```
 ┌───────┐      POST /api/app/instances          ┌────────┐
 │ App   │ ───────────────────────────────────▶  │ Server │  starts temp instance (10 min)
 │       │ ◀─────────────────────────────────── │        │  instance_id + claim_token
 │       │                                          └────────┘
 │       │      (user authorizes inside the spawned service)
 │       │
 │       │      POST /api/app/instances/{id}/claim  (with Telegram initData + claim_token)
 │       │ ───────────────────────────────────▶  │ Server │  validates initData, transfers
 │       │ ◀─────────────────────────────────── │        │  ownership, resets TTL to 1h,
 │       │                                          │        │  returns session token
 │       │      ...app uses the instance/output_link under the session token...
 │       │
 │       │      DELETE /api/app/instances/{id}     │        │
 │       │ ───────────────────────────────────▶  │        │  stops the instance
 └───────┘                                          └────────┘
```

---

## 2. Server configuration

Set these environment variables on the server (see `.env.example`):

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `WB_APP_TOKEN` | **yes** | (empty) | Shared static secret the app sends in `X-App-Token`. Empty = whole `/api/app` router returns **404**. |
| `WB_TELEGRAM_BOT_TOKEN` | **yes** | (empty) | Bot token from BotFather. Enables Telegram login + the claim flow. Empty = claim returns **404**. |
| `WB_APP_TEMP_TIMEOUT` | no | `600` | Temp-instance lifetime in seconds (the auth window). |
| `WB_DEFAULT_TIMEOUT_SECONDS` | no | `3600` | Lifetime after claim (1 hour). |
| `WB_QUICK_MAX_CONCURRENT` | no | `5` | Global cap on simultaneous temp/quick instances. |

Generate a strong app token:

```sh
openssl rand -hex 32
```

---

## 3. Authentication model

The Android API uses **two** layers:

1. **App token** — every `/api/app/*` request must include:
   ```
   X-App-Token: <WB_APP_TOKEN>
   ```
   This identifies the *application*, not the user. Without it (or if the server
   has `WB_APP_TOKEN` unset) every endpoint returns `404` / `401`.

2. **Telegram user identity** — the user authenticates via a Telegram WebApp
   `initData` string. On Android this is obtained through the **Telegram Bot
   WebApp SDK** (open the bot via the Telegram app and read the signed `initData`
   the SDK injects). The server validates it cryptographically (HMAC-SHA256 with
   the bot token) — the app never sends a password. After validation the server
   returns an opaque **bearer session token** used as:
   ```
   Authorization: Bearer <token>
   ```

---

## 3a. Telegram Mini App bridge (`/tg-auth`)

The Android app cannot read Telegram's `initData` directly — it lives inside
Telegram's WebView. A **thin server-side Mini App page** bridges the two: when
the user taps "Sign in with Telegram" in the Android app, the app opens the bot
(`@wl_cors_bot`) in Telegram; Telegram loads this page, which reads `initData`
and deep-links it back to the Android app.

The page is built into the server at **`GET /tg-auth`** (`static/tg-auth.html`)
and does exactly one thing:

```js
Telegram.WebApp.openLink(
  "corsconnect://tginit?initdata=" + encodeURIComponent(initData)
);
```

**BotFather setup (one-time):**

1. Open `@BotFather` → your bot → **Bot Settings** → **Menu Button** (or **Mini
   App Settings**).
2. Set the **Mini App URL** to your deployed bridge page:
   ```
   https://<your-domain>/tg-auth
   ```
   (Telegram requires HTTPS. **Do not** point it at `/` — that loads the web
   dashboard, which logs into the web app instead of returning to Android.)
3. Save. The bot's menu button now opens the bridge page.

**How the handoff works:**

| Step | Actor | Action |
|---|---|---|
| 1 | Android app | User taps "Sign in with Telegram"; app opens `@wl_cors_bot`. |
| 2 | Telegram | Loads `https://<domain>/tg-auth` inside its WebView; injects `initData`. |
| 3 | Bridge page | Reads `Telegram.WebApp.initData`, validates shape, calls `openLink(corsconnect://tginit?initdata=...)`. |
| 4 | Android OS | Intent-filter (`scheme=corsconnect host=tginit`) delivers the deep link to the running app via `onNewIntent`. |
| 5 | Android app | `Uri.decode`s `initdata`, then `POST /api/app/instances/{id}/claim`. |

**Deep-link contract (must match the Android manifest):**

- Scheme: `corsconnect` (exact)
- Host: `tginit` (exact)
- Query param: `initdata` (lowercase; `initData` also accepted by the app)
- Value: the **raw, complete** `initData`, URL-encoded (`encodeURIComponent`).

The bridge page handles the edge cases from the spec automatically:
- **`initData` empty** (opened outside Telegram) → shows "Open in Telegram", no redirect.
- **`openLink` unavailable** (very old client) → falls back to `window.location.href`.
- **`initData` expired (>24h)** → no page action; the server returns `401` at claim and the app re-prompts.

> The `initData` has a replay window (`WB_TG_INITDATA_MAX_AGE`, default 24 h).
> Obtain it fresh each session.

---

## 4. Endpoint reference (summary)

All under the base URL of your deployment. Full schemas in `android_api.yaml`.

### `GET /api/app/health`
Readiness probe. Returns `{ok, telegram_enabled, service_available}`.

### `POST /api/app/instances`
Create a temporary instance.
- Request body (optional): `{ "service_id": <int> }` — omit to use the server's configured service.
- Response `201`: `{ instance_id, claim_token, status, output_link, service_name, temp_expires_at, temp_ttl_seconds }`
- `429` if the global cap is reached; `404` if no enabled service.

### `POST /api/app/instances/{instance_id}/claim`
Transfer the temp instance to the authenticated user and extend its lifetime.
- Request body: `{ "telegram_init_data": "<initData>", "claim_token": "<token>" }`
- Response `200`: `{ instance_id, user_id, token, role, username, status, output_link, expires_at }`
- `401` bad initData; `404` token/instance mismatch or Telegram disabled; `409` already claimed; `410` instance ended.

### `GET /api/app/instances/{instance_id}`
Poll status and `output_link`. Returns `{ id, user_id, status, output_link, error, timeout_at, is_quick, service_name }`.

### `DELETE /api/app/instances/{instance_id}`
Stop the instance (call on disconnect). Idempotent.
- Request body: `{ "claim_token": "<token>" }` — required for a temp (pre-claim) instance; after claim the bearer-authenticated client API may be used instead.
- Response `200`: `{ ok, instance: <InstanceState> }`

### `POST /api/auth/telegram`
Standalone Telegram login (not required by the claim flow, which validates initData internally, but useful to re-establish a stored session). Body `{ "initData": "<initData>" }` → `{ token, role, username, must_change_password }`.

---

## 5. Recommended client flow

1. **On app open:** call `GET /api/app/health`. If `service_available` is false, show "service unavailable"; if `telegram_enabled` is false, the claim step will not work.
2. **Create temp instance:** `POST /api/app/instances`. Store `instance_id` and `claim_token`. Poll `GET /api/app/instances/{id}` until `output_link` appears (or `status` becomes terminal).
3. **Authorize inside the service:** present `output_link` to the user; they complete authorization within the temp window (`WB_APP_TEMP_TIMEOUT`, default 10 min).
4. **Authenticate + claim:** the user taps "Sign in with Telegram", which opens `@wl_cors_bot`; Telegram loads the `/tg-auth` bridge page, which deep-links `initData` back to the app via `corsconnect://tginit?initdata=...` (see [§3a](#3a-telegram-mini-app-bridge-tg-auth)). The app extracts `initData`, then `POST /api/app/instances/{id}/claim`. Persist the returned `token` and `username`.
5. **Already authenticated?** If the app holds a valid bearer from a prior session, it can still call `claim` with fresh `initData` to transfer the instance, or simply reuse the existing session and call `POST /api/app/instances` then immediately claim.
6. **On disconnect:** `DELETE /api/app/instances/{id}`. If the app crashes, the server's `timeout_at` (1 h after claim, 10 min before) kills the instance as a safety net.

---

## 6. Status values

Instance `status` transitions: `pending → running → stopping → {stopped|exited|crashed|timeout}`. Treat `pending`/`running`/`stopping` as live; the others as terminal.

## 7. Error format

All errors are `{"detail": "<message>"}` with standard HTTP codes:
`400` bad input, `401` bad/missing token, `403` forbidden, `404` not found / feature disabled, `409` conflict, `410` gone, `429` rate/cap limit, `504` link timeout.

---

## 8. Implementation notes

- Temporary instances run server-side under the admin account with the same
  flag the `/quick` flow uses, so the global cap, process reaper, and
  restart-reconciliation all apply automatically.
- Claiming does **not** restart the process — the session the user authorized
  inside the spawned service is preserved. Only the owner and the lifetime
  change.
- The `claim_token` is single-use and held in server memory; a server restart
  invalidates outstanding tokens (the corresponding temp instances are still
  reclaimed by their timeout).
