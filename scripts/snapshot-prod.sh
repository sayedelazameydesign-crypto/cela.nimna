#!/usr/bin/env bash
# What:       saves an offline snapshot of one commit into .snapshots/: its SHA, a `git archive` tarball (+ sha256), and the NAMES (never values) of the env vars that code reads.
# When:       before every merge to main (main auto-deploys to FastAPI Cloud), so the last known-good code can be redeployed even without GitHub.
# On failure: exits non-zero (1 = snapshot failed verification, 2 = git/network error, 64 = usage) and deletes the partial snapshot; nothing is ever half-written.
#
# Usage: scripts/snapshot-prod.sh [--ref REF] [--out DIR]
#   --ref REF   commit to snapshot (default: origin/main, after `git fetch origin main`)
#   --out DIR   destination (default: <repo>/.snapshots — must be git-ignored when inside the repo)
#
# Output (<ts> = UTC timestamp, e.g. 20260924T160501Z):
#   <ts>.sha               the full commit SHA (use it with `git revert`, `git archive`, `git checkout`)
#   <ts>.tar.gz            `git archive` of that commit; `git get-tar-commit-id` returns the same SHA
#   <ts>.tar.gz.sha256     checksum of the tarball
#   <ts>.env.names         env var names read by that code / listed in its .env.example — values are
#                          NOT captured: copy them from the FastAPI Cloud dashboard yourself
#
# Redeploy a snapshot without GitHub (FastAPI Cloud has no tarball upload; the CLI deploys a folder):
#   mkdir /tmp/nimna-restore && tar -xzf .snapshots/<ts>.tar.gz -C /tmp/nimna-restore
#   fastapi deploy /tmp/nimna-restore --app-id <app-id>
set -Eeuo pipefail

REF=""; OUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref) [ "$#" -ge 2 ] || { echo "snapshot: --ref needs a value" >&2; exit 64; }; REF="$2"; shift 2 ;;
    --out) [ "$#" -ge 2 ] || { echo "snapshot: --out needs a value" >&2; exit 64; }; OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0" >&2; exit 64 ;;
    *) echo "snapshot: unknown argument: $1" >&2; exit 64 ;;
  esac
done
for bin in git python3 gzip tar; do
  command -v "$bin" > /dev/null 2>&1 || { echo "snapshot: required tool not found: $bin" >&2; exit 64; }
done
if command -v sha256sum > /dev/null 2>&1; then sha256() { sha256sum "$@"; }
elif command -v shasum > /dev/null 2>&1; then sha256() { shasum -a 256 "$@"; }   # macOS
else echo "snapshot: required tool not found: sha256sum (or shasum)" >&2; exit 64; fi
TOP="$(git rev-parse --show-toplevel 2> /dev/null)" || { echo "snapshot: run inside the git repository" >&2; exit 64; }
cd "$TOP"

if [ -z "$REF" ]; then
  if ! git fetch --quiet origin main; then
    echo "snapshot: git fetch origin main failed (network/auth) — refusing to snapshot a stale origin/main" >&2
    exit 2
  fi
  REF="origin/main"
fi
SHA="$(git rev-parse --verify --quiet "$REF^{commit}")" || { echo "snapshot: '$REF' is not a commit here" >&2; exit 64; }

OUT="${OUT:-$TOP/.snapshots}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
case "$OUT/" in
  "$TOP"/*)
    if ! git check-ignore -q "$OUT/probe.tar.gz"; then
      echo "snapshot: $OUT is inside the repo but not git-ignored — refusing (tarballs must never be committed)" >&2
      exit 64
    fi ;;
esac

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$OUT/$TS"
for f in "$BASE.sha" "$BASE.tar.gz"; do
  [ ! -e "$f" ] || { echo "snapshot: $f already exists" >&2; exit 64; }
done
STAGE="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/nimna-snapshot.XXXXXX")"
DONE=0
cleanup() {
  rm -rf "$STAGE"
  [ "$DONE" -eq 1 ] || rm -f "$BASE.sha" "$BASE.tar.gz" "$BASE.tar.gz.sha256" "$BASE.env.names"
}
trap cleanup EXIT
fail() { echo "snapshot: $*" >&2; exit 1; }

# 1. archive (git failure => exit 2)
git archive --format=tar.gz -o "$STAGE/snap.tar.gz" "$SHA" || { echo "snapshot: git archive failed" >&2; exit 2; }

# 2. verify before publishing: gzip integrity, embedded commit id, file count, extractable
gzip -t "$STAGE/snap.tar.gz" || fail "tarball is not valid gzip"
gzip -dc "$STAGE/snap.tar.gz" > "$STAGE/snap.tar" || fail "tarball does not decompress"
# (not a pipe: get-tar-commit-id stops after the header and gzip would die of SIGPIPE under pipefail)
EMBEDDED="$(git get-tar-commit-id < "$STAGE/snap.tar")" || fail "tarball has no embedded commit id"
[ "$EMBEDDED" = "$SHA" ] || fail "tarball commit id $EMBEDDED != $SHA"
mkdir "$STAGE/tree"
tar -xf "$STAGE/snap.tar" -C "$STAGE/tree" || fail "tarball does not extract"
EXPECTED="$(git ls-tree -r --name-only "$SHA" | wc -l | tr -d ' ')"
ACTUAL="$(cd "$STAGE/tree" && find . \( -type f -o -type l \) | wc -l | tr -d ' ')"
[ "$EXPECTED" = "$ACTUAL" ] || fail "tarball has $ACTUAL files, commit has $EXPECTED"

# 3. env var names from the snapshotted code (names only)
python3 -I - "$STAGE/tree" > "$STAGE/env.names" <<'PY' || fail "could not extract env var names"
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
read = re.compile(r"""(?:_env\w*|_key_source|os\.getenv|os\.environ\.get|_env_flag)\(\s*["']([A-Z][A-Z0-9_]{2,})["']"""
                  r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]{2,})["']\s*\]""")
names = set()
for py in (sorted((root / "nimna").rglob("*.py")) if (root / "nimna").is_dir() else []):
    for m in read.finditer(py.read_text(encoding="utf-8", errors="replace")):
        names.add(m.group(1) or m.group(2))
example = root / ".env.example"
if example.is_file():
    for line in example.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*=", line)
        if m:
            names.add(m.group(1))
for n in sorted(names):
    print(n)
PY
[ -s "$STAGE/env.names" ] || fail "no env var names found in $SHA"

# 4. publish atomically-enough: everything verified above, then move into place
umask 077
printf '%s\n' "$SHA" > "$BASE.sha"
mv "$STAGE/snap.tar.gz" "$BASE.tar.gz"
( cd "$OUT" && sha256 "$TS.tar.gz" > "$TS.tar.gz.sha256" )
{
  echo "# env var NAMES read by $SHA (nimna/**/*.py + .env.example) — values are not recorded."
  echo "# Copy the values of the ones you set from the FastAPI Cloud dashboard (App -> Environment Variables)."
  cat "$STAGE/env.names"
} > "$BASE.env.names"
( cd "$OUT" && sha256 -c "$TS.tar.gz.sha256" > /dev/null ) || fail "checksum mismatch after write"
DONE=1

echo "snapshot of $REF"
echo "  sha:       $SHA"
echo "  files:     $ACTUAL"
echo "  env names: $(grep -vc '^#' "$BASE.env.names")"
echo "  written:"
for f in "$BASE.sha" "$BASE.tar.gz" "$BASE.tar.gz.sha256" "$BASE.env.names"; do
  printf '    %s (%s bytes)\n' "$f" "$(wc -c < "$f" | tr -d ' ')"
done
