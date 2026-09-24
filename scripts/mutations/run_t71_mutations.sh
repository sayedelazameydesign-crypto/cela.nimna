#!/bin/bash
# T7.1 binding-migration mutations (MB1–MB4) — reviewer-runnable.
#
# Method: copy the tree to /tmp, mutate THE COPY, run the binding+gateway tests
# with python -B (no bytecode), then restore the copy — the working tree is
# never touched. Each mutation MUST fail at least one test.
#
# Usage:  bash scripts/mutations/run_t71_mutations.sh
# Needs:  .venv with pytest (python3 -m venv .venv && .venv/bin/pip install -e . pytest)
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$REPO/.venv/bin/python"
WORK="$(mktemp -d /tmp/mut_t71.XXXXXX)"

cleanup () { rm -rf "$WORK"; find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; }
trap cleanup EXIT

cp -r "$REPO/nimna" "$REPO/tests" "$REPO/pyproject.toml" "$REPO/workspace" "$REPO/skills" "$WORK"/ 2>/dev/null
find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null

mutate () { # $1=name $2=file-under-nimna $3=old $4=new
  python3 - "$1" "$2" "$3" "$4" << 'PYEOF'
import sys
from pathlib import Path
name, fname, old, new = sys.argv[1:5]
p = Path(f"{__import__('os').environ['WORK']}/nimna/{fname}")
t = p.read_text(encoding="utf-8")
assert old in t, f"MUTATION {name}: pattern not found"
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print(f"MUTATION {name}: applied to nimna/{fname}")
PYEOF
}

run_tests () {
  cd "$WORK"
  PYTHONDONTWRITEBYTECODE=1 "$PY" -B -m pytest tests/test_binding_migration.py tests/test_gateway.py 2>&1 \
    | grep -E "FAILED tests/|[0-9]+ (passed|failed)" | sed 's/^/    /'
  find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
}

restore () {
  rm -rf "$WORK/nimna"
  cp -r "$REPO/nimna" "$WORK/nimna"
  find "$WORK" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
}

echo "=== MB1: refusal branch becomes legacy fallback ==="
WORK="$WORK" mutate mb1 core/agent.py '        else:
            # invariant (T7.1): bound gateway + unregistered tool ⇒ NO handler
            self._audit(state, "gateway_refused",
                        {"tool": tool.name,
                         "reason": "not registered in the bound gateway (fail-closed binding)"})
            result = self._compact_tool_result(
                {"status": "NOT_IN_GATEWAY", "ok": False,
                 "reason": (f"{tool.name}: this agent is gateway-bound and the tool "
                            "is not registered in the gateway registry (fail-closed)")}, 10_000)
            ok, duration_ms = False, 0' \
'        else:
            result, ok, duration_ms = self.tools.execute(
                tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
            )  # MUTATED: refusal became legacy fallback'
run_tests; restore

echo "=== MB2: gateway failure falls back to legacy ==="
WORK="$WORK" mutate mb2 core/agent.py '            except Exception as exc:  # noqa: BLE001 — fail-closed: never legacy-fallback
                self._audit(state, "gateway_error",
                            {"tool": tool.name, "error": exc.__class__.__name__})
                result = self._compact_tool_result(
                    {"status": "GATEWAY_ERROR", "ok": False,
                     "reason": f"{exc.__class__.__name__}: {str(exc)[:120]}"}, 10_000)
                ok, duration_ms = False, 0' \
'            except Exception:
                result, ok, duration_ms = self.tools.execute(
                    tool.name, call.arguments, ctx, max_chars=self.settings.tool_result_max_chars
                )  # MUTATED: gateway failure falls back to legacy'
run_tests; restore

echo "=== MB3: swarm refusal branch removed ==="
WORK="$WORK" mutate mb3 agents/base.py '                elif gateway is not None and call.name not in getattr(gateway, "compat_tools", frozenset()):
                    err = (f"tool '"'"'{call.name}'"'"' is not registered in the bound execution "
                           "gateway (fail-closed binding)")
                    messages.append(Message.tool_result(call, f'"'"'{{"error":"{err}"}}'"'"'))
                    tool_calls_log.append({"name": call.name, "ok": False, "error": err})
                    continue' \
'                elif False:  # MUTATED: swarm refusal removed — falls through to legacy'
run_tests; restore

echo "=== MB4: bound gateway ignored entirely ==="
WORK="$WORK" mutate mb4 core/agent.py '        gateway = getattr(self, "execution_gateway", None)
        if gateway is None:' \
'        gateway = None  # MUTATED: binding ignored — every tool on the legacy path
        if gateway is None:'
run_tests; restore

echo "=== baseline sanity (clean copy) ==="
run_tests
