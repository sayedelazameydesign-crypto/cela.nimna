#!/usr/bin/env bash
# What:       reads GitHub Deployments + their status history, prints the SHA of the last good deployment to restore (dry run); --execute opens a PR restoring that exact tree on top of main.
# When:       a deploy is broken AND the environment has a successful deployment to go back to (after PR #20 merges, 55ef6d6 is that target whether its deploy succeeds or fails).
# On failure: exits non-zero (1 = no rollback target, 2 = gh/API error, 64 = usage) and changes nothing; with no target use scripts/emergency-revert.sh instead.
#
# rollback-prod.sh — find the last successful production deployment (GitHub Deployments API)
# and roll the default branch back to it through a reviewable PR.
#
# Usage:
#   scripts/rollback-prod.sh [--env "<environment>"] [--repo owner/name] [--execute]
#
#   default    dry run: print the live SHA and the rollback target SHA, change nothing
#   --execute  via `gh api`: build a NEW commit on top of the default branch whose tree is
#              exactly the target SHA's tree (history is not rewritten), push it to a
#              `rollback/...` branch and open a PR. Merging that PR is the deploy.
#
# Target selection inside one environment (a deployment counts as successful if ANY of its
# statuses is "success": GitHub marks superseded deployments "inactive"):
#   * newest deployment NOT successful -> target = newest successful one (what the provider
#     still serves: FastAPI Cloud keeps the last successful deployment live when a new one fails)
#   * newest deployment successful     -> target = newest earlier success with a different SHA
#
# Why a PR and not a GitHub "deployment" record: FastAPI Cloud creates deployments from
# `fastapi deploy`, pushes to the connected branch, or "Save and Redeploy"; it does not act on
# GitHub deployment events, so a new deployment record would change nothing in production.
# Faster manual path when available: redeploy the target from the provider dashboard.
#
# Needs: gh (authenticated; `repo` scope, plus `workflow` if the rollback touches
#        .github/workflows), python3.
# Exit codes: 0 ok (dry run / PR opened / nothing to do) · 1 no rollback target · 2 gh/API error · 64 usage
set -Eeuo pipefail

ENV_NAME=""; REPO=""; EXECUTE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) [ "$#" -ge 2 ] || { echo "rollback: --env needs a value" >&2; exit 64; }; ENV_NAME="$2"; shift 2 ;;
    --repo) [ "$#" -ge 2 ] || { echo "rollback: --repo needs a value" >&2; exit 64; }; REPO="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) sed -n '2,15p' "$0" >&2; exit 64 ;;
    *) echo "rollback: unknown argument: $1" >&2; exit 64 ;;
  esac
done
for bin in gh python3; do
  command -v "$bin" > /dev/null 2>&1 || { echo "rollback: required tool not found: $bin" >&2; exit 64; }
done

WORK="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/nimna-rollback.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# ghapi OUTFILE ARGS... — any gh failure is fatal (exit 2), never ignored
ghapi() {
  local out="$1"; shift
  if ! gh api "$@" > "$out" 2> "$WORK/gh.err"; then
    echo "rollback: gh api $* failed:" >&2; sed 's/^/  /' "$WORK/gh.err" >&2
    exit 2
  fi
}
json() { python3 -I "$WORK/rollback_json.py" "$@"; }
cat > "$WORK/rollback_json.py" <<'PY'
import json, sys
def docs(path):
    """gh --paginate prints one JSON document per page; merge arrays."""
    text, dec, i, out = open(path, encoding="utf-8").read(), json.JSONDecoder(), 0, []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.extend(obj if isinstance(obj, list) else [obj])
    return out
cmd, path = sys.argv[1], sys.argv[2]
if cmd == "field":                      # field FILE key[.key...]
    obj = docs(path)[0]
    for k in sys.argv[3].split("."):
        obj = obj[k]
    print(obj)
elif cmd == "envs":                     # distinct environments, newest first
    seen = []
    for d in docs(path):
        if d["environment"] not in seen:
            seen.append(d["environment"])
    print("\n".join(seen))
elif cmd == "deployments":              # id<TAB>sha<TAB>created_at for one environment
    for d in docs(path):
        if d["environment"] == sys.argv[3]:
            print(f'{d["id"]}\t{d["sha"]}\t{d["created_at"]}')
elif cmd == "succeeded":                # exit 0 if any status is success
    sys.exit(0 if any(s.get("state") == "success" for s in docs(path)) else 1)
elif cmd == "latest_state":
    states = docs(path)
    print(states[0]["state"] if states else "none")
elif cmd == "body":                     # body KEY=VALUE... -> JSON object (parents is a list)
    out = {}
    for kv in sys.argv[3:]:
        k, _, v = kv.partition("=")
        out[k] = [v] if k == "parents" else v
    print(json.dumps(out))
PY

if [ -z "$REPO" ]; then
  ghapi "$WORK/repo.json" "repos/{owner}/{repo}"
  REPO="$(json field "$WORK/repo.json" full_name)"
else
  ghapi "$WORK/repo.json" "repos/$REPO"
fi
DEFAULT_BRANCH="$(json field "$WORK/repo.json" default_branch)"

ghapi "$WORK/deployments.json" --paginate "repos/$REPO/deployments?per_page=100"
ENVS=(); while IFS= read -r line; do ENVS+=("$line"); done < <(json envs "$WORK/deployments.json")   # no mapfile: bash 3.2
[ "${#ENVS[@]}" -gt 0 ] || { echo "rollback: $REPO has no GitHub deployments" >&2; exit 1; }
if [ -z "$ENV_NAME" ]; then
  if [ "${#ENVS[@]}" -ne 1 ]; then
    echo "rollback: $REPO has ${#ENVS[@]} deployment environments — choose one with --env:" >&2
    printf '  --env "%s"\n' "${ENVS[@]}" >&2
    exit 64
  fi
  ENV_NAME="${ENVS[0]}"
fi
ROWS=(); while IFS= read -r line; do ROWS+=("$line"); done < <(json deployments "$WORK/deployments.json" "$ENV_NAME")
[ "${#ROWS[@]}" -gt 0 ] || { echo "rollback: no deployments for environment '$ENV_NAME'" >&2; exit 64; }

echo "repo:        $REPO (default branch: $DEFAULT_BRANCH)"
echo "environment: $ENV_NAME (${#ROWS[@]} deployments)"

NEWEST_OK=""; LIVE_SHA=""; TARGET_SHA=""; TARGET_ID=""; TARGET_AT=""; idx=0
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r id sha created <<< "$row"
  ghapi "$WORK/st.json" "repos/$REPO/deployments/$id/statuses?per_page=100"
  succeeded=0; json succeeded "$WORK/st.json" && succeeded=1
  if [ "$idx" -eq 0 ]; then
    state="$(json latest_state "$WORK/st.json")"
    echo "newest:      #$id ${sha:0:12} $created state=$state"
    [ "$succeeded" -eq 1 ] && NEWEST_OK=1 || NEWEST_OK=0
  fi
  if [ "$succeeded" -eq 1 ]; then
    if [ "$NEWEST_OK" -eq 0 ]; then                 # newest failed: restore the last good one
      TARGET_SHA="$sha"; TARGET_ID="$id"; TARGET_AT="$created"; LIVE_SHA="$sha"; break
    fi
    if [ -z "$LIVE_SHA" ]; then LIVE_SHA="$sha"     # the current good deployment
    elif [ "$sha" != "$LIVE_SHA" ]; then TARGET_SHA="$sha"; TARGET_ID="$id"; TARGET_AT="$created"; break
    fi
  fi
  idx=$((idx + 1))
done

if [ -z "$TARGET_SHA" ]; then
  if [ -z "$LIVE_SHA" ]; then
    echo "rollback: environment '$ENV_NAME' has no successful deployment — nothing to roll back to" >&2
  else
    echo "rollback: no earlier successful deployment with a different SHA than the live ${LIVE_SHA:0:12} in '$ENV_NAME'" >&2
  fi
  exit 1
fi

if [ "$NEWEST_OK" -eq 0 ]; then
  echo "live:        ${LIVE_SHA:0:12} (newest attempt failed; the provider keeps the last success live)"
else
  echo "live:        ${LIVE_SHA:0:12}"
fi
echo "ROLLBACK TARGET SHA: $TARGET_SHA  (deployment #$TARGET_ID, $TARGET_AT)"

if [ "$EXECUTE" -eq 0 ]; then
  echo
  echo "dry run — nothing changed. Re-run with --execute to open a rollback PR to $DEFAULT_BRANCH."
  exit 0
fi

ghapi "$WORK/head.json" "repos/$REPO/git/ref/heads/$DEFAULT_BRANCH"
HEAD_SHA="$(json field "$WORK/head.json" object.sha)"
ghapi "$WORK/headc.json" "repos/$REPO/git/commits/$HEAD_SHA"
ghapi "$WORK/target.json" "repos/$REPO/git/commits/$TARGET_SHA"
HEAD_TREE="$(json field "$WORK/headc.json" tree.sha)"
TARGET_TREE="$(json field "$WORK/target.json" tree.sha)"
if [ "$HEAD_TREE" = "$TARGET_TREE" ]; then
  echo "rollback: $DEFAULT_BRANCH (${HEAD_SHA:0:12}) already has the exact tree of ${TARGET_SHA:0:12} — no PR needed."
  echo "          Redeploy that code from the provider dashboard if production does not serve it."
  exit 0
fi

SHORT="${TARGET_SHA:0:7}"
BRANCH="rollback/${SHORT}-$(date -u +%Y%m%d%H%M%S)"
json body "$WORK" "message=rollback: restore tree of $SHORT ($ENV_NAME)

Restores the exact tree of $TARGET_SHA (last successful deployment #$TARGET_ID)
on top of $DEFAULT_BRANCH $HEAD_SHA. Generated by scripts/rollback-prod.sh." \
  "tree=$TARGET_TREE" "parents=$HEAD_SHA" > "$WORK/commit.body"
ghapi "$WORK/commit.json" -X POST "repos/$REPO/git/commits" --input "$WORK/commit.body"
NEW_SHA="$(json field "$WORK/commit.json" sha)"
json body "$WORK" "ref=refs/heads/$BRANCH" "sha=$NEW_SHA" > "$WORK/ref.body"
ghapi "$WORK/ref.json" -X POST "repos/$REPO/git/refs" --input "$WORK/ref.body"
json body "$WORK" "title=rollback: restore $SHORT ($ENV_NAME)" "head=$BRANCH" "base=$DEFAULT_BRANCH" \
  "body=Restores the code of the last successful deployment **$TARGET_SHA** (#$TARGET_ID, $TARGET_AT) in \`$ENV_NAME\`.

Created by \`scripts/rollback-prod.sh --execute\`. Merging this PR pushes to \`$DEFAULT_BRANCH\`, which is what triggers the provider deployment. Run \`scripts/preflight-prod-check.sh\` after the deploy." \
  > "$WORK/pr.body"
ghapi "$WORK/pr.json" -X POST "repos/$REPO/pulls" --input "$WORK/pr.body"
echo "rollback commit: $NEW_SHA on branch $BRANCH"
echo "rollback PR:     $(json field "$WORK/pr.json" html_url)"
echo "next: review + merge the PR (that push deploys), then run scripts/preflight-prod-check.sh"
