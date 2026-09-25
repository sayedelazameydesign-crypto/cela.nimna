#!/usr/bin/env bash
# Session start: report git continuity state.
# READ-ONLY by design — it never recovers anything automatically.
# Background: .git objects evaporate between sessions (worktree persists,
# unpushed commits do not), so every commit must be pushed immediately.
set -u
BRANCH="$(git branch --show-current 2>/dev/null || echo '?')"
echo "== branch: $BRANCH"
git fetch --all --quiet 2>&1 | head -3
echo "== HEAD (last 5):"
git log --oneline -5 2>/dev/null || echo "(no local history)"
echo "== status (first 20 lines):"
git status --porcelain | head -20
TOTAL="$(git status --porcelain | wc -l)"
[ "$TOTAL" -gt 20 ] && echo "... ($TOTAL lines total)"
UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")"
if [ -n "$UPSTREAM" ]; then
  LOCAL="$(git rev-parse HEAD 2>/dev/null || echo '?')"
  REMOTE="$(git rev-parse "$UPSTREAM" 2>/dev/null || echo '?')"
  if [ "$LOCAL" = "$REMOTE" ]; then
    echo "== continuity: IN SYNC with $UPSTREAM"
  else
    echo "== continuity: DIVERGED - local HEAD $LOCAL vs $UPSTREAM $REMOTE"
    echo "   (report only; decide before acting)"
  fi
else
  echo "== continuity: no upstream configured for $BRANCH"
fi
