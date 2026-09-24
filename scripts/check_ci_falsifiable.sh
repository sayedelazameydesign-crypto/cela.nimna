#!/bin/bash
# T8.1 hardening — a workflow file is FALSIFIABLE only if it can fail.
# Refuses: continue-on-error, "|| true", fail_action: false — the three masks
# that turn a security/integrity gate into a rubber stamp.
# Usage: check_ci_falsifiable.sh <workflow.yml> [...]
set -u
rc=0
for f in "$@"; do
  [ -f "$f" ] || { echo "FALSIFIABILITY: missing file: $f"; rc=1; continue; }
  if grep -n "continue-on-error" "$f" > /tmp/cf_hits.$$ 2>/dev/null; then
    echo "FALSIFIABILITY VIOLATION in $f: continue-on-error present:"; cat /tmp/cf_hits.$$; rc=1
  fi
  if grep -n "|| true" "$f" > /tmp/cf_hits.$$ 2>/dev/null; then
    echo "FALSIFIABILITY VIOLATION in $f: '|| true' present:"; cat /tmp/cf_hits.$$; rc=1
  fi
  if grep -n "fail_action: false\|fail_action:false" "$f" > /tmp/cf_hits.$$ 2>/dev/null; then
    echo "FALSIFIABILITY VIOLATION in $f: fail_action: false present:"; cat /tmp/cf_hits.$$; rc=1
  fi
done
rm -f /tmp/cf_hits.$$
[ "$rc" -eq 0 ] && echo "FALSIFIABILITY OK: $* can fail — no masks found"
exit "$rc"
