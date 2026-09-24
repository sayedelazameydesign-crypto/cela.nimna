# API security — operator runbook

The policy (what is enforced and why) lives in [`../SECURITY.md`](../SECURITY.md#api-access-control-http--websocket).
This page covers **how to operate it**: generating and rotating keys, per-platform setup,
calling the API, and troubleshooting. Code: `nimna/api/security.py`. Tests: `tests/test_auth.py`.

## 0. Before merging: FastAPI Cloud deploys from `main`

GitHub Deployments on this repository are created by `fastapi-cloud[bot]` for the commits on `main`
(`gh api repos/sayedelazameydesign-crypto/cela.nimna/deployments`). After this change, **a deployment
without `NIMNA_API_KEY` refuses to boot** (`SecurityConfigError` at import of `nimna.api.app:app`).

1. Generate a key (below).
2. Add `NIMNA_API_KEY` to the environment variables of **every** FastAPI Cloud app that deploys this
   repository (dashboard → app → environment variables), **before** the merge.
3. Merge, then verify: `curl -s -o /dev/null -w '%{http_code}' https://<app>/api/tools` returns `401`,
   and the same call with `-H "X-Nimna-Key: $NIMNA_API_KEY"` returns `200`.

## 1. Generate a key

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 43 chars, [A-Za-z0-9_-]
```

Production rules (checked at boot): at least 32 characters, characters limited to `[A-Za-z0-9._~+-]`
(valid both in an HTTP header and in a WebSocket sub-protocol token), not a placeholder or low-entropy value.
Base64 values containing `/` or `=` (e.g. Render `generateValue`) are refused on purpose.

Store it in the platform secret manager / `.env` (`chmod 600`). Never commit it, never put it in a URL.

## 2. Rotate a key without downtime

```bash
NIMNA_API_KEY=<old>,<new>    # 1. deploy with both
                             # 2. move every client to <new>
NIMNA_API_KEY=<new>          # 3. deploy with only the new key
```

Each key has its own rate-limit bucket and WebSocket budget.

## 3. Per-platform setup

| Platform | What to set |
|---|---|
| FastAPI Cloud | `NIMNA_API_KEY` in the app's environment variables (`NIMNA_ENV` can stay unset: production is the default). |
| Render | `render.yaml` already pins `NIMNA_ENV: "production"` and declares `NIMNA_API_KEY` as `sync: false` → paste the key in the dashboard before *Manual Deploy*. See [`DEPLOY-RENDER-FREE.md`](DEPLOY-RENDER-FREE.md). |
| Kubernetes | `kubectl create secret generic nimna-secrets --from-literal=NIMNA_API_KEY=<key> …` — the reference in `k8s/deployment.yaml` is **not optional**, so a missing key blocks the pod from starting. |
| docker compose | `NIMNA_API_KEY=<key>` in `.env` (loaded through `env_file`). The `desktop` profile also needs `VNC_PASSWORD` (≥ 12 chars, not a default). `nimna-sandbox` is **DEV ONLY — لا تنشر**. |
| Local dev | Either set a key, or `NIMNA_ENV=development` without one: the API is then served to loopback clients (`127.0.0.1`, `::1`) only. |

`nimna doctor --offline` prints the effective posture, for example
`✅ API security: env=production, auth=api-key (1 key), cors=deny all, chat=30/min/key, ws=10/key`.

## 4. Calling the API

```bash
# public
curl -s "$BASE/api/health"
# everything else
curl -s "$BASE/api/tools" -H "X-Nimna-Key: $NIMNA_API_KEY"
curl -s "$BASE/api/chat"  -H "X-Nimna-Key: $NIMNA_API_KEY" -H 'Content-Type: application/json' \
     -d '{"message":"hello"}'
# WebSocket (CLI clients can send the header)
websocat -H "X-Nimna-Key: $NIMNA_API_KEY" "wss://<host>/ws/my-session"
```

Browser / JavaScript (browsers cannot set headers on a WebSocket, so the key rides in a sub-protocol
that the server never echoes back):

```js
await fetch('/api/tools', { headers: { 'X-Nimna-Key': key } });
const ws = new WebSocket(`wss://${location.host}/ws/my-session`, ['nimna.v1', 'nimna.key.' + key]);
```

The built-in UI on `/` does this for you: it asks for the key on the first `401`, keeps it in
`sessionStorage` (per tab), and the 🔑 button changes or clears it.

## 5. CORS

- The built-in UI is same-origin: **no CORS entry is needed**.
- A separate frontend (e.g. the Next.js app under `web/`) needs its exact origin:
  `NIMNA_ALLOWED_ORIGINS=https://app.example.com,http://localhost:3000`.
- `*`, wildcard hosts, paths, and `user@host` are rejected at boot. Credentials (cookies) are never allowed;
  auth is the header only.

## 6. Limits

| Limit | Default | Response |
|---|---|---|
| `POST /api/chat` per key | 30 / minute (sliding window) | `429` + `Retry-After: <seconds>` |
| Concurrent `/ws/*` per key | 10 | close `1008` before accept (HTTP 403) |
| Messages per WebSocket | 10 / second (pre-existing) | `{"type":"error"}` |

The counters are per process. With several workers/replicas, the effective ceiling is
`limit × processes`. Put a shared limiter (Redis / API gateway) in front if you need a global cap.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Boot fails: `NIMNA_API_KEY is required when NIMNA_ENV=production` | No key in the environment | Set it (§1, §3) |
| Boot fails: `… is too short` / `placeholder` / `characters outside` | Weak or base64 key | Regenerate with `token_urlsafe(32)` |
| Boot fails: `NIMNA_ALLOWED_ORIGINS …` | Wildcard or non-origin entry | Use `scheme://host[:port]` only |
| `401 {"detail":"missing or invalid API key"}` | Header missing/wrong | Send `X-Nimna-Key` |
| `401` in development without a key | Request did not come from loopback (Docker bridge, LAN, proxy) | Set a key |
| `429` on `/api/chat` | More than 30 requests in the last minute for that key | Wait `Retry-After` seconds |
| WebSocket closes immediately (1008 / HTTP 403) | Bad key, or 10 connections already open for that key | Check the key; close idle tabs |
| `desktop` container exits with `nimna-vnc-guard: …` | `VNC_PASSWORD` empty/weak/short | Set a unique ≥ 12-char value in `.env` |
