#!/usr/bin/env bash
# What:       calls a live deployment and verifies its security posture: public / and /api/health, 401 without/with a wrong key, the valid key accepted, 3 security headers, 429 + Retry-After after N+1 chat requests.
# When:       right after every deploy of code that includes PR #20 (and before announcing it); not against pre-PR-20 code, which has no auth and must fail.
# On failure: exits non-zero (1 = a check failed, 2 = network/TLS error — aborts at once, never `curl -k`, 64 = usage/missing key); the key is never printed.
#
# preflight-prod-check.sh — verify the live API security posture of a Nimna deployment.
#
# Usage:
#   NIMNA_API_KEY=<key> scripts/preflight-prod-check.sh <base-url> [--chat-limit N]
#   e.g. NIMNA_API_KEY=… scripts/preflight-prod-check.sh https://celanimna-3ffa6b22.fastapicloud.dev
#
# Checks (all must pass):
#   1. GET /                               -> 200 (static UI, public)
#   2. GET /api/health                     -> 200 + valid JSON with "status": "ok"
#   3. GET /api/chat  without key          -> 401
#   4. GET /api/chat  with the valid key   -> 200 or a 4xx other than 401 (405: route is POST-only)
#   5. GET /api/chat  with a wrong key     -> 401
#   6. X-Content-Type-Options: nosniff, X-Frame-Options: DENY,
#      Referrer-Policy: strict-origin-when-cross-origin on /, /api/health and the 401 response
#   7. (N+1) x POST /api/chat with the key -> requests 1..N not 429, request N+1 = 429 + Retry-After
#      N defaults to 30 (NIMNA_CHAT_RATE_LIMIT). The limiter only counts POST /api/chat, so the
#      burst uses POST with an empty JSON body: FastAPI answers 422 before the agent runs, which
#      means no model call and no cost. It DOES use up this key's chat budget for up to 60 s.
#
# Exit codes: 0 = all checks passed · 1 = at least one check failed ·
#             2 = network/TLS error (aborts immediately) · 64 = usage/config error
#
# Guarantees: no `curl -k`; TLS is always verified; network errors are fatal and never ignored;
# plain http:// is refused except for loopback hosts; the key is passed to curl through a 0600
# header file (not argv, so it does not show up in `ps`) and is never printed.
set -Eeuo pipefail

usage() { sed -n '2,9p' "$0" >&2; exit 64; }

BASE_URL=""
CHAT_LIMIT="${NIMNA_CHAT_RATE_LIMIT:-30}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --chat-limit) [ "$#" -ge 2 ] || usage; CHAT_LIMIT="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "preflight: unknown option: $1" >&2; exit 64 ;;
    *) [ -z "$BASE_URL" ] || usage; BASE_URL="$1"; shift ;;
  esac
done
BASE_URL="${BASE_URL:-${PREFLIGHT_BASE_URL:-}}"
[ -n "$BASE_URL" ] || { echo "preflight: missing <base-url>" >&2; usage; }
BASE_URL="${BASE_URL%/}"

for bin in curl python3; do
  command -v "$bin" > /dev/null 2>&1 || { echo "preflight: required tool not found: $bin" >&2; exit 64; }
done
if [ -z "${NIMNA_API_KEY:-}" ]; then
  echo "preflight: NIMNA_API_KEY is not set (export the production key first)" >&2
  exit 64
fi
case "$CHAT_LIMIT" in ''|*[!0-9]*) echo "preflight: --chat-limit must be a positive integer" >&2; exit 64 ;; esac
[ "$CHAT_LIMIT" -ge 1 ] || { echo "preflight: --chat-limit must be >= 1" >&2; exit 64; }

# refuse cleartext for anything that is not loopback (the key would travel unencrypted)
case "$BASE_URL" in
  https://*) ;;
  http://localhost|http://localhost:*|http://127.0.0.1|http://127.0.0.1:*|http://\[::1\]|http://\[::1\]:*) ;;
  *) echo "preflight: refusing $BASE_URL — use https:// (http:// is only allowed for loopback)" >&2; exit 64 ;;
esac

WORK="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/nimna-preflight.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
( umask 077
  printf 'X-Nimna-Key: %s\n' "$NIMNA_API_KEY" > "$WORK/key.hdr"
  printf 'X-Nimna-Key: %s\n' "$(python3 -c 'import secrets; print("wrong-" + secrets.token_urlsafe(32))')" > "$WORK/wrong.hdr" )

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }

# req NAME METHOD PATH [extra curl args...] -> sets CODE; body in $WORK/NAME.body, headers in $WORK/NAME.hdr
req() {
  local name="$1" method="$2" path="$3"; shift 3
  local rc=0
  CODE="$(curl --silent --show-error --proto '=http,https' --connect-timeout 10 --max-time 30 \
                --request "$method" --output "$WORK/$name.body" --dump-header "$WORK/$name.hdr" \
                --write-out '%{http_code}' "$@" "$BASE_URL$path")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "preflight: network/TLS error on $method $path (curl exit $rc) — aborting" >&2
    exit 2
  fi
}

header_value() {  # header_value FILE NAME -> value of the last occurrence (case-insensitive name)
  tr -d '\r' < "$1" | awk -v n="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" -F': *' \
    'tolower($1) == n { sub(/^[^:]*: */, ""); v = $0 } END { print v }'
}

echo "Nimna preflight — $BASE_URL"

echo "[1] GET /"
req root GET /
[ "$CODE" = "200" ] && ok "GET / -> 200" || bad "GET / -> $CODE (expected 200)"

echo "[2] GET /api/health"
req health GET /api/health
if [ "$CODE" = "200" ]; then
  if python3 - "$WORK/health.body" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"        invalid JSON: {exc}"); sys.exit(1)
if not isinstance(data, dict) or data.get("status") != "ok":
    print(f"        unexpected payload: status={data.get('status') if isinstance(data, dict) else type(data).__name__!r}")
    sys.exit(1)
PY
  then ok "GET /api/health -> 200 + JSON status=ok"; else bad "GET /api/health -> 200 but body is not the expected JSON"; fi
else
  bad "GET /api/health -> $CODE (expected 200)"
fi

echo "[3-5] /api/chat authentication"
req nokey GET /api/chat
[ "$CODE" = "401" ] && ok "GET /api/chat without key -> 401" || bad "GET /api/chat without key -> $CODE (expected 401)"

req goodkey GET /api/chat -H "@$WORK/key.hdr"
if [ "$CODE" = "200" ] || { [ "$CODE" -ge 400 ] && [ "$CODE" -lt 500 ] && [ "$CODE" != "401" ]; }; then
  ok "GET /api/chat with valid key -> $CODE (authenticated)"
else
  bad "GET /api/chat with valid key -> $CODE (expected 200 or a non-401 4xx)"
fi

req wrongkey GET /api/chat -H "@$WORK/wrong.hdr"
[ "$CODE" = "401" ] && ok "GET /api/chat with wrong key -> 401" || bad "GET /api/chat with wrong key -> $CODE (expected 401)"

echo "[6] security headers"
for resp in root health nokey; do
  for pair in "X-Content-Type-Options=nosniff" "X-Frame-Options=DENY" "Referrer-Policy=strict-origin-when-cross-origin"; do
    name="${pair%%=*}"; want="${pair#*=}"
    got="$(header_value "$WORK/$resp.hdr" "$name")"
    if [ "$got" = "$want" ]; then ok "$name: $want ($resp)"; else bad "$name on $resp response = '${got:-<missing>}' (expected '$want')"; fi
  done
done

echo "[7] rate limit: $((CHAT_LIMIT + 1)) x POST /api/chat (limit $CHAT_LIMIT/min per key)"
early=""
for i in $(seq 1 "$CHAT_LIMIT"); do
  req "burst" POST /api/chat -H "@$WORK/key.hdr" -H 'Content-Type: application/json' --data '{}'
  case "$CODE" in
    429) early="$i"; break ;;
    401|5??) bad "burst request $i -> $CODE (auth or server error)"; early="error"; break ;;
  esac
done
if [ -z "$early" ]; then
  ok "requests 1..$CHAT_LIMIT accepted by the limiter (last status $CODE)"
  req last POST /api/chat -H "@$WORK/key.hdr" -H 'Content-Type: application/json' --data '{}'
  retry="$(header_value "$WORK/last.hdr" "Retry-After")"
  if [ "$CODE" = "429" ] && [[ "$retry" =~ ^[0-9]+$ ]] && [ "$retry" -ge 1 ] && [ "$retry" -le 60 ]; then
    ok "request $((CHAT_LIMIT + 1)) -> 429, Retry-After: $retry"
  elif [ "$CODE" = "429" ]; then
    bad "request $((CHAT_LIMIT + 1)) -> 429 but Retry-After is '${retry:-<missing>}' (expected 1..60)"
  else
    bad "request $((CHAT_LIMIT + 1)) -> $CODE (expected 429 — limit higher than $CHAT_LIMIT, or counters split across replicas)"
  fi
elif [ "$early" != "error" ]; then
  bad "429 already at request $early/$CHAT_LIMIT — the key was used in the last 60 s or the limit is lower; wait 60 s and rerun"
fi

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
