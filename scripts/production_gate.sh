#!/bin/bash
# T8.1 — Production Gate for cela.nimna (native Python hardening).
# Sequential, falsifiable, no masks. A step that cannot run FAILS the gate —
# there is no skip flag; an unverifiable step is an unverifiable candidate.
#
# Usage (inside the project venv):  bash scripts/production_gate.sh
# CI: runs the same script in the production-gate job.
set -uo pipefail
cd "$(dirname "$0")/.."
T8_SHA="9885f3c33b908b131855db4e8ac1277c881ea56c"
PY="${PYTHON:-python3}"
PASS=0; FAIL=0
TOTAL=7
step() { echo; echo "=== [$1/$TOTAL] $2 ==="; }
ok()   { echo "    ✔ $1"; PASS=$((PASS+1)); }
bad()  { echo "    ✘ $1"; FAIL=$((FAIL+1)); }

step 1 "T8 ancestor — candidate must contain 9885f3c (T7.1 closed + T8)"
if git merge-base --is-ancestor "$T8_SHA" HEAD 2>/dev/null; then
  ok "T8 commit is an ancestor of HEAD ($(git rev-parse --short HEAD))"
else
  bad "T8 commit $T8_SHA is NOT in HEAD's history — production candidate refused"
fi

step 2 "CI falsifiability — the gate's own home must keep no masks"
if bash scripts/check_ci_falsifiable.sh .github/workflows/ci.yml; then ok "ci.yml can fail"; else bad "ci.yml carries a mask (continue-on-error / || true / fail_action: false)"; fi

step 3 "Static syntax — compileall over production + tests"
if "$PY" -m compileall -q nimna tests; then ok "compileall clean"; find nimna tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; else bad "compileall failed"; fi

step 4 "Full offline suite — pytest"
# no extra -q: pyproject addopts already quiet — double quiet HIDES the count line
if "$PY" -m pytest --tb=line > /tmp/gate_pytest.log 2>&1; then
  ok "$(grep -E '[0-9]+ passed' /tmp/gate_pytest.log | tail -1)"
else
  bad "pytest failed — tail:"; tail -5 /tmp/gate_pytest.log
fi

step 5 "Dependency CVE audit — pip-audit (env as installed; blocking)"
"$PY" -m pip install -q -U "pip>=26.2" "setuptools>=83" 2>/dev/null \
  || { bad "toolchain harden (pip>=26.2, setuptools>=83) failed"; }
"$PY" -m pip_audit > /tmp/gate_audit.log 2>&1
AUDIT_RC=$?
if [ "$AUDIT_RC" -eq 0 ]; then
  ok "$(grep -E "No known|Found" /tmp/gate_audit.log | tail -1)"
else
  bad "pip-audit RED (rc=$AUDIT_RC) — vulnerabilities must be FIXED, not masked:"
  grep -E "Found|PYSEC|GHSA" /tmp/gate_audit.log 2>/dev/null | head -8
fi

step 6 "Runtime health evidence — real HTTP against a real server"
HEALTH_PORT=8765; export MODEL_PROVIDER=mock DB_PATH="$(mktemp -d /tmp/gate_db.XXXX)/gate.db"
"$PY" -m uvicorn nimna.api.app:app --host 127.0.0.1 --port "$HEALTH_PORT" > /tmp/gate_uv.log 2>&1 &
UPID=$!
trap 'kill "$UPID" 2>/dev/null' EXIT
HEALTH="no-answer"
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${HEALTH_PORT}/api/health" > /tmp/gate_health.json 2>/dev/null; then HEALTH="ok"; break; fi
  sleep 1
done
if [ "$HEALTH" = "ok" ]; then
  ok "GET /api/health → $(head -c 160 /tmp/gate_health.json)…"
else
  bad "server never answered /api/health — tail:"; tail -3 /tmp/gate_uv.log
fi

step 7 "Docker build — the shippable artifact (hard; no skip flag)"
if command -v docker > /dev/null 2>&1; then
  if docker build --pull -t nimna:production-candidate . ; then ok "image built: nimna:production-candidate"; else bad "docker build failed"; fi
else
  bad "docker CLI not found — artifact unverifiable here; the gate is RED until a CI/daemon run builds it"
fi

echo
echo "================== PRODUCTION GATE: $PASS passed / $FAIL failed =================="
if [ "$FAIL" -eq 0 ]; then
  echo "VERDICT: candidate CERTIFIED by this run"
  exit 0
fi
echo "VERDICT: BLOCKED — not a production candidate"
exit 1
