#!/bin/bash
# T7.1 binding-migration mutations (MB1–MB4) — reviewer-runnable, parallel-safe.
#
# Isolation: EVERY mutation cycle copies the tree into its OWN mktemp dir,
# mutates the COPY, tests with python -B, and deletes it (trap). The working
# tree is never touched. Concurrency: --jobs N fans the four mutations across
# N workers, each with its own mktemp dir; the ONLY shared resource is the
# summary file, guarded by a real flock(1). Default: --jobs 1.
#
# Usage: scripts/mutations/run_t71_mutations.sh [--jobs N]
# Needs: .venv with pytest (python3 -m venv .venv && .venv/bin/pip install -e . pytest)
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
JOBS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS="$2"; shift 2 ;;
    --jobs=*) JOBS="${1#--jobs=}"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$JOBS" in ''|*[!0-9]*|0) echo "--jobs must be a positive integer" >&2; exit 2;; esac

SUMMARY="$(mktemp /tmp/t71_mut_summary.XXXXXX)"
LOCKFILE="$SUMMARY.lock"
exec 9>"$LOCKFILE"

# run_one <name> — one FULL cycle in its own isolated mktemp dir
run_one () {
  local name="$1"
  local WORK; WORK="$(mktemp -d /tmp/mut_t71.XXXXXX)"
  echo "[$name] WORK: $WORK"
  cp -r "$REPO/nimna" "$REPO/tests" "$REPO/pyproject.toml" "$REPO/workspace" \
     "$REPO/skills" "$WORK"/ 2>/dev/null
  find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

  python3 - "$name" "$WORK" << 'PYEOF'
import hashlib, json, os, sys
from pathlib import Path

name, WORK = sys.argv[1], sys.argv[2]
# (file-under-nimna, pattern, replacement) — the four mandated mutations
MUTATIONS = {
 "mb1": ("core/agent.py",
  '''        else:
            # invariant (T7.1): bound gateway + unregistered tool ⇒ NO handler
            self._audit(state, "gateway_refused",
                        {"tool": tool.name,
                         "reason": "not registered in the bound gateway (fail-closed binding)"})
            result = self._compact_tool_result(
                {"status": "NOT_IN_GATEWAY", "ok": False,
                 "reason": (f"{tool.name}: this agent is gateway-bound and the tool "
                            "is not registered in the gateway registry (fail-closed)")}, 10_000)
            ok, duration_ms = False, 0''',
  '''        else:
            result, ok, duration_ms = self.tools.execute(
                tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
            )  # MUTATED: refusal became legacy fallback'''),
 "mb2": ("core/agent.py",
  '''            except Exception as exc:  # noqa: BLE001 — fail-closed: never legacy-fallback
                self._audit(state, "gateway_error",
                            {"tool": tool.name, "error": exc.__class__.__name__})
                result = self._compact_tool_result(
                    {"status": "GATEWAY_ERROR", "ok": False,
                     "reason": f"{exc.__class__.__name__}: {str(exc)[:120]}"}, 10_000)
                ok, duration_ms = False, 0''',
  '''            except Exception:
                result, ok, duration_ms = self.tools.execute(
                    tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
                )  # MUTATED: gateway failure falls back to legacy'''),
 "mb3": ("agents/base.py",
  '''                elif gateway is not None and call.name not in getattr(gateway, "compat_tools", frozenset()):
                    err = (f"tool '{call.name}' is not registered in the bound execution "
                           "gateway (fail-closed binding)")
                    messages.append(Message.tool_result(call, f'{{"error":"{err}"}}'))
                    tool_calls_log.append({"name": call.name, "ok": False, "error": err})
                    continue''',
  '''                elif False:  # MUTATED: swarm refusal removed — falls through to legacy'''),
 "mb4": ("core/agent.py",
  '''        gateway = getattr(self, "execution_gateway", None)
        if gateway is None:''',
  '''        gateway = None  # MUTATED: binding ignored — every tool on the legacy path
        if gateway is None:'''),
}
fname, old, new = MUTATIONS[name]
p = Path(WORK) / "nimna" / fname
t = p.read_text(encoding="utf-8")
assert old in t, f"{name}: pattern not found in {fname}"
p.write_text(t.replace(old, new, 1), encoding="utf-8")
digest = hashlib.sha256(p.read_bytes()).hexdigest()
print(f"[{name}] mutated-copy sha256: {digest}  ({fname})")
(Path(WORK) / ".mutinfo").write_text(json.dumps({"name": name, "file": fname,
                                                  "sha256": digest}), encoding="utf-8")
PYEOF

  cd "$WORK"
  RESULT=$(PYTHONDONTWRITEBYTECODE=1 "$PY" -B -m pytest tests/test_binding_migration.py \
             tests/test_gateway.py 2>&1 | grep -E "FAILED tests/|[0-9]+ (passed|failed)")
  find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  echo "[$name] $RESULT" | sed 's/^/    /'
  flock 9
  echo "$name | $RESULT | $(cat "$WORK/.mutinfo" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"][:16])')" >> "$SUMMARY"
  flock -u 9
  cd "$REPO"
  rm -rf "$WORK"
}

echo "=== T7.1 mutations (jobs=$JOBS) ==="
if [ "$JOBS" -eq 1 ]; then
  run_one mb1; run_one mb2; run_one mb3; run_one mb4
else
  run_one mb1 & P1=$!; run_one mb2 & P2=$!
  run_one mb3 & P3=$!; run_one mb4 & P4=$!
  wait $P1 $P2 $P3 $P4
fi

echo "=== baseline sanity (clean copy) ==="
BASE="$(mktemp -d /tmp/mut_t71_base.XXXXXX)"
cp -r "$REPO/nimna" "$REPO/tests" "$REPO/pyproject.toml" "$REPO/workspace" "$REPO/skills" "$BASE"/ 2>/dev/null
( cd "$BASE" && PYTHONDONTWRITEBYTECODE=1 "$PY" -B -m pytest tests/test_binding_migration.py tests/test_gateway.py 2>&1 \
  | grep -E "[0-9]+ (passed|failed)" | tail -1 )
rm -rf "$BASE"

echo "=== summary (flock-collected) ==="
cat "$SUMMARY"
rm -f "$SUMMARY" "$LOCKFILE"
