#!/usr/bin/env bash
# What:       `git revert`s ONE commit that is on main (a merge commit is reverted with -m 1) on a new branch and opens a PR titled "EMERGENCY REVERT: <sha>".
# When:       a deploy from main is live but broken and must be undone now; needs no GitHub Deployments history (works on a repo that has never had a successful deploy).
# On failure: exits non-zero (1 = revert conflict / precondition, 2 = git/gh/network error, 64 = usage); your checkout is never touched and the dry run pushes nothing.
#
# Usage: scripts/emergency-revert.sh <sha>     [--execute] [--base main] [--repo owner/name]
#        scripts/emergency-revert.sh --pr <N>  [--execute] [--base main] [--repo owner/name]
#
#   default    DRY RUN: fetch origin/<base>, perform the revert in a throw-away worktree, print the
#              diffstat and whether the result equals the pre-merge tree. Pushes nothing, opens nothing.
#   --execute  same, then push branch emergency-revert/<sha7>-<ts> and open the PR via `gh api`.
#   --pr N     revert the merge commit of merged PR #N (resolved with `gh api`).
#
# FastAPI Cloud has no deploy hook to call: it deploys every connected app when a new commit lands on
# the default branch. Merging the revert PR IS the deploy. Squash/rebase merges (1 parent) are reverted
# without -m. Needs: git (push access to origin), python3; gh for --pr/--execute.
set -Eeuo pipefail

TARGET=""; PR=""; EXECUTE=0; BASE="main"; REPO=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --pr) [ "$#" -ge 2 ] || { echo "revert: --pr needs a number" >&2; exit 64; }; PR="$2"; shift 2 ;;
    --base) [ "$#" -ge 2 ] || { echo "revert: --base needs a value" >&2; exit 64; }; BASE="$2"; shift 2 ;;
    --repo) [ "$#" -ge 2 ] || { echo "revert: --repo needs a value" >&2; exit 64; }; REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0" >&2; exit 64 ;;
    -*) echo "revert: unknown option: $1" >&2; exit 64 ;;
    *) [ -z "$TARGET" ] || { echo "revert: only one <sha>" >&2; exit 64; }; TARGET="$1"; shift ;;
  esac
done
if [ -n "$TARGET" ] && [ -n "$PR" ]; then echo "revert: give <sha> or --pr, not both" >&2; exit 64; fi
if [ -z "$TARGET" ] && [ -z "$PR" ]; then sed -n '6,7p' "$0" >&2; exit 64; fi
case "$PR" in ''|*[!0-9]*) [ -z "$PR" ] || { echo "revert: --pr must be a number" >&2; exit 64; } ;; esac
for bin in git python3; do
  command -v "$bin" > /dev/null 2>&1 || { echo "revert: required tool not found: $bin" >&2; exit 64; }
done
if [ -n "$PR" ] || [ "$EXECUTE" -eq 1 ]; then
  command -v gh > /dev/null 2>&1 || { echo "revert: gh is required for --pr / --execute" >&2; exit 64; }
fi
TOP="$(git rev-parse --show-toplevel 2> /dev/null)" || { echo "revert: run inside the git repository" >&2; exit 64; }
cd "$TOP"

WORK="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/nimna-revert.XXXXXX")"
WT="$WORK/wt"
cleanup() {
  if [ -d "$WT" ]; then git worktree remove --force "$WT" > /dev/null 2>&1 || rm -rf "$WT"; fi
  git worktree prune > /dev/null 2>&1 || :   # housekeeping only; the result was already reported
  rm -rf "$WORK"
}
trap cleanup EXIT
ghapi() {   # ghapi OUTFILE ARGS... — any failure is fatal
  local out="$1"; shift
  if ! gh api "$@" > "$out" 2> "$WORK/gh.err"; then
    echo "revert: gh api $* failed:" >&2; sed 's/^/  /' "$WORK/gh.err" >&2
    exit 2
  fi
}
jfield() { python3 -I -c 'import json,sys
obj = json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    obj = obj[k]
print(str(obj).lower() if isinstance(obj, bool) else obj)' "$@"; }
resolve_repo() {
  [ -n "$REPO" ] && return 0
  if [ -n "${GITHUB_REPOSITORY:-}" ]; then REPO="$GITHUB_REPOSITORY"; return 0; fi
  REPO="$(git remote get-url origin | python3 -I -c 'import re,sys
m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", sys.stdin.read().strip())
print(m.group(1) if m else "")')"
  [ -n "$REPO" ] || { echo "revert: origin is not a GitHub URL — pass --repo owner/name" >&2; exit 64; }
}

if [ -n "$PR" ]; then
  resolve_repo
  ghapi "$WORK/pr.json" "repos/$REPO/pulls/$PR"
  if [ "$(jfield "$WORK/pr.json" merged)" != "true" ]; then
    echo "revert: PR #$PR is not merged — there is nothing on $BASE to revert" >&2
    exit 1
  fi
  TARGET="$(jfield "$WORK/pr.json" merge_commit_sha)"
  echo "PR #$PR merge commit: $TARGET"
fi

if ! git fetch --quiet origin "$BASE"; then
  echo "revert: git fetch origin $BASE failed (network/auth)" >&2
  exit 2
fi
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  # a shallow clone hides parents: a merge would look like a root commit and the revert would be wrong
  echo "note: shallow clone — fetching full history first (git fetch --unshallow)"
  if ! git fetch --quiet --unshallow origin; then
    echo "revert: git fetch --unshallow failed (network/auth)" >&2
    exit 2
  fi
fi
SHA="$(git rev-parse --verify --quiet "$TARGET^{commit}")" || { echo "revert: '$TARGET' is not a commit (after fetch)" >&2; exit 64; }
if ! git merge-base --is-ancestor "$SHA" "origin/$BASE"; then
  echo "revert: $SHA is not on origin/$BASE — only commits that were deployed from $BASE can be reverted" >&2
  exit 64
fi
read -r -a PARENTS <<< "$(git rev-list --parents -n 1 "$SHA")"
NPARENTS=$(( ${#PARENTS[@]} - 1 ))
case "$NPARENTS" in
  1) MFLAG=(); KIND="single-parent commit (squash/rebase merge or direct commit)"; PRE="${PARENTS[1]}" ;;
  2) MFLAG=(-m 1); KIND="merge commit (reverted with -m 1: back to its first parent)"; PRE="${PARENTS[1]}" ;;
  0) echo "revert: $SHA is the root commit — cannot revert it" >&2; exit 64 ;;
  *) echo "revert: $SHA is an octopus merge ($NPARENTS parents) — revert it by hand" >&2; exit 64 ;;
esac
HEAD_SHA="$(git rev-parse "origin/$BASE")"
LATER="$(git rev-list --count "$SHA..origin/$BASE")"

echo "repo checkout:  $TOP"
echo "base:           origin/$BASE @ ${HEAD_SHA:0:12}"
echo "revert target:  $SHA"
echo "                $(git log -1 --format=%s "$SHA")"
echo "kind:           $KIND"
echo "commits after:  $LATER on $BASE (kept — this is a real git revert, not a reset)"

git worktree add --quiet --detach "$WT" "origin/$BASE" 2> "$WORK/wt.err" || {
  echo "revert: git worktree add failed:" >&2; sed 's/^/  /' "$WORK/wt.err" >&2; exit 2; }
IDENT=()
if [ -z "$(git config user.email || :)" ]; then
  IDENT=(-c user.name="nimna emergency-revert" -c user.email="emergency-revert@users.noreply.github.com")
fi
if ! git -C "$WT" ${IDENT[@]+"${IDENT[@]}"} revert --no-edit ${MFLAG[@]+"${MFLAG[@]}"} "$SHA" > "$WORK/revert.log" 2>&1; then
  CONFLICTS="$(git -C "$WT" diff --name-only --diff-filter=U)"
  if [ -n "$CONFLICTS" ]; then
    echo "revert: CONFLICT — later commits on $BASE changed the same lines. Files:" >&2
    printf '%s\n' "$CONFLICTS" | sed 's/^/  /' >&2
    echo "revert: resolve by hand: git revert ${MFLAG[*]+${MFLAG[*]}} $SHA   (nothing was pushed)" >&2
  else
    echo "revert: git revert failed (already reverted? nothing to revert?):" >&2
    sed 's/^/  /' "$WORK/revert.log" >&2
  fi
  exit 1
fi
NEW_SHA="$(git -C "$WT" rev-parse HEAD)"
if [ "$LATER" -eq 0 ]; then
  if [ "$(git rev-parse "$NEW_SHA^{tree}")" = "$(git rev-parse "$PRE^{tree}")" ]; then
    echo "result tree:    IDENTICAL to ${PRE:0:12} (the code before $SHA)"
  else
    echo "revert: result tree differs from ${PRE:0:12} although nothing was merged after — aborting" >&2
    exit 1
  fi
else
  echo "result tree:    $SHA undone; the $LATER later commit(s) kept"
fi
echo "diffstat vs origin/$BASE:"
git diff --stat "origin/$BASE" "$NEW_SHA" | tail -n 15 | sed 's/^/  /'

SHORT="${SHA:0:7}"
if [ "$EXECUTE" -eq 0 ]; then
  echo
  echo "DRY RUN — the revert applies cleanly. Nothing was pushed and no PR was opened."
  echo "Run again with --execute to push emergency-revert/$SHORT-<ts> and open 'EMERGENCY REVERT: $SHA'."
  exit 0
fi

resolve_repo
BRANCH="emergency-revert/$SHORT-$(date -u +%Y%m%d%H%M%S)"
if ! git push --quiet origin "$NEW_SHA:refs/heads/$BRANCH" 2> "$WORK/push.err"; then
  echo "revert: git push of $BRANCH failed:" >&2; sed 's/^/  /' "$WORK/push.err" >&2
  exit 2
fi
echo "pushed:         $BRANCH ($NEW_SHA)"
python3 -I - "$WORK/pr.body" "$SHA" "$BRANCH" "$BASE" "$KIND" "$LATER" "$HEAD_SHA" <<'PY'
import json, sys
path, sha, branch, base, kind, later, head = sys.argv[1:]
body = f"""**Emergency revert of `{sha}`** ({kind}).

- Base: `{base}` @ `{head}`; commits after the reverted one that are kept: {later}.
- Created by `scripts/emergency-revert.sh --execute` (a real `git revert`, no force-push).
- **Merging this PR is the deploy**: FastAPI Cloud deploys every connected app when a new commit lands on `{base}`.

After merging:
1. FastAPI Cloud dashboard -> each app -> Deployments: wait for **Ready** (a failed build keeps the previous deployment live).
2. `curl -fsS https://<app>/api/health` returns 200 and `curl -fsS -o /dev/null https://<app>/` returns 200.
3. If this reverts the API-auth PR, the API is **unauthenticated again** until it is re-applied (`git revert <this revert>`).
"""
json.dump({"title": f"EMERGENCY REVERT: {sha}", "head": branch, "base": base, "body": body}, open(path, "w"))
PY
if ! gh api -X POST "repos/$REPO/pulls" --input "$WORK/pr.body" > "$WORK/pr.out" 2> "$WORK/gh.err"; then
  echo "revert: branch $BRANCH is pushed, but opening the PR failed:" >&2; sed 's/^/  /' "$WORK/gh.err" >&2
  echo "revert: open it by hand: https://github.com/$REPO/compare/$BASE...$BRANCH?expand=1" >&2
  exit 2
fi
PR_URL="$(jfield "$WORK/pr.out" html_url)"
cat <<EOF

================ EMERGENCY REVERT READY ================
PR:        $PR_URL
title:     EMERGENCY REVERT: $SHA
branch:    $BRANCH -> $BASE
deploy:    FastAPI Cloud has no deploy hook here — merging this PR pushes to $BASE,
           which deploys every connected FastAPI Cloud app.
verify:    dashboard Deployments = Ready; /api/health 200; / 200
=========================================================
EOF
