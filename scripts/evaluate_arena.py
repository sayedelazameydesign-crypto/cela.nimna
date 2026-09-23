"""Arena diff evaluation — turn a unified ``git diff`` into an honest report.

Used by ``.github/workflows/arena_diff_eval.yml`` on every pull request:

    git diff origin/main...HEAD > changes.diff
    python scripts/evaluate_arena.py --diff_file changes.diff --output result.md

The script is **stdlib only** so it runs on a bare runner without ``pip``.

What it does
------------
1. Parses the unified diff (files, +/- lines, added/deleted/renamed/binary).
2. Groups the change by category (code / tests / skills / docs / workflows /
   infra / config) and raises *risk signals* that matter for this repository:
   secret-like additions, tracked ``.env`` files, sensitive paths (sandbox,
   tools, governance, CI, Docker/k8s, dependency pins) and code changed
   without touching ``tests/``.
3. Optionally forwards the diff + metrics to an external Arena endpoint
   (``ARENA_API_URL`` + ``ARENA_API_KEY``). Without those the benchmark row is
   reported as ``SKIPPED`` — never as a fabricated ``PASS`` (see
   ``docs/VERIFICATION-MATRIX.md``: ``UNKNOWN`` is never promoted to ``PASS``).
4. Renders Markdown (for the PR comment) and/or JSON (for machines).

Exit codes: ``0`` report produced, ``1`` usage / IO error, ``2`` ``--strict``
and the change carries a HIGH risk signal or the remote benchmark FAILED.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPORT_VERSION = 1
MAX_CAPTURED_ADDED_LINES = 20_000  # per diff, keeps secret scanning bounded
MAX_LISTED_FILES = 200
DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_DIFF_BYTES = 200_000
LARGE_DIFF_LINES = 1_000

# --------------------------------------------------------------------------- #
# Repository knowledge (paths are relative to the repository root)
# --------------------------------------------------------------------------- #
CODE_ROOTS = ("nimna/", "security/", "scripts/")
SENSITIVE_PATHS = (
    ".github/workflows/",
    "nimna/tools/sandbox.py",
    "nimna/tools/builtin/",
    "nimna/governance/",
    "nimna/core/approval.py",
    "nimna/api/app.py",
    "security/",
    "Dockerfile",
    "docker-compose.yml",
    "k8s/",
    "requirements.txt",
    "pyproject.toml",
)
ENV_FILE_NAMES = {".env", ".env.local", ".env.production", ".env.staging"}

# name -> compiled pattern applied to *added* lines only. The matched value is
# never echoed back; only the file, line number and pattern name are reported.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    (
        "generic-assignment",
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d)[A-Z0-9_]*\b\s*[:=]\s*"
            r"[\"']?(?P<value>[A-Za-z0-9_\-/+=.]{16,})[\"']?"
        ),
    ),
)
PLACEHOLDER_HINTS = (
    "${{", "os.environ", "getenv", "your-", "your_", "<", ">", "xxx", "changeme",
    "example", "placeholder", "redacted", "dummy", "***",
)


@dataclass
class FileChange:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added | deleted | renamed | modified
    added: int = 0
    removed: int = 0
    binary: bool = False
    added_lines: list[tuple[int, str]] = field(default_factory=list)

    @property
    def category(self) -> str:
        return categorize(self.path)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("added_lines", None)
        data["category"] = self.category
        return data


@dataclass
class RiskSignal:
    level: str  # HIGH | MEDIUM | INFO
    code: str
    message: str
    files: list[str] = field(default_factory=list)


LEVEL_ORDER = {"HIGH": 3, "MEDIUM": 2, "INFO": 1, "LOW": 0}
LEVEL_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "INFO": "ℹ️", "LOW": "🟢"}
STATUS_ICON = {
    "PASS": "✅", "FAIL": "❌", "SKIPPED": "⏭️", "MOCKED": "🧪", "ERROR": "⚠️", "UNKNOWN": "❔",
}


# --------------------------------------------------------------------------- #
# Diff parsing
# --------------------------------------------------------------------------- #
def _clean_path(raw: str) -> str:
    """Strip ``a/``/``b/`` prefixes, quotes and ``diff -u`` timestamps."""
    path = raw.strip().split("\t", 1)[0].strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _path_from_git_header(line: str) -> str:
    rest = line[len("diff --git "):]
    marker = rest.rfind(" b/")
    if rest.startswith("a/") and marker > 0:
        return _clean_path(rest[marker + 1:])
    return _clean_path(rest.split(" ", 1)[-1])


def parse_unified_diff(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    current: FileChange | None = None
    in_hunk = False
    new_lineno = 0
    captured = 0

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            current = FileChange(path=_path_from_git_header(raw))
            files.append(current)
            in_hunk = False
            continue

        if current is None:
            if raw.startswith("--- "):  # plain ``diff -u`` without git headers
                current = FileChange(path=_clean_path(raw[4:]))
                files.append(current)
                in_hunk = False
            continue

        if raw.startswith("@@"):
            in_hunk = True
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            new_lineno = int(match.group(1)) if match else 0
            continue

        if not in_hunk:
            if raw.startswith("new file mode"):
                current.status = "added"
            elif raw.startswith("deleted file mode"):
                current.status = "deleted"
            elif raw.startswith("rename from "):
                current.old_path = raw[len("rename from "):].strip()
                current.status = "renamed"
            elif raw.startswith("rename to "):
                current.path = raw[len("rename to "):].strip()
                current.status = "renamed"
            elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                current.binary = True
            elif raw.startswith("--- "):
                old = _clean_path(raw[4:])
                if old == "/dev/null":
                    current.status = "added"
                elif current.old_path is None and current.status != "renamed":
                    current.old_path = old
            elif raw.startswith("+++ "):
                new = _clean_path(raw[4:])
                if new == "/dev/null":
                    current.status = "deleted"
                else:
                    current.path = new
            continue

        if raw.startswith("+"):
            current.added += 1
            if captured < MAX_CAPTURED_ADDED_LINES:
                current.added_lines.append((new_lineno, raw[1:]))
                captured += 1
            new_lineno += 1
        elif raw.startswith("-"):
            current.removed += 1
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            new_lineno += 1  # context line

    return files


# --------------------------------------------------------------------------- #
# Classification and risk signals
# --------------------------------------------------------------------------- #
def categorize(path: str) -> str:
    name = Path(path).name
    if path.startswith(".github/"):
        return "workflows"
    if path.startswith("tests/") or name.startswith("test_") or name == "conftest.py":
        return "tests"
    if path.startswith("skills/"):
        return "skills"
    if path.startswith(("docs/",)) or name.lower().endswith((".md", ".rst", ".txt")):
        return "docs"
    if path.startswith(("k8s/", "infra/")) or name in {"Dockerfile", "docker-compose.yml", ".dockerignore"}:
        return "infra"
    if name in {"pyproject.toml", "requirements.txt", ".env.example", ".gitignore"} or name.endswith(
        (".toml", ".yml", ".yaml", ".ini", ".cfg", ".json")
    ):
        return "config"
    if path.startswith(CODE_ROOTS) or name.endswith((".py", ".js", ".ts", ".html", ".css")):
        return "code"
    return "other"


def _is_sensitive(path: str) -> bool:
    return any(path == item or path.startswith(item) for item in SENSITIVE_PATHS)


def _looks_like_placeholder(line: str, value: str | None) -> bool:
    lowered = line.lower()
    if any(hint in lowered for hint in PLACEHOLDER_HINTS):
        return True
    if value is not None:
        if len(set(value)) <= 3:  # "xxxxxxxxxxxxxxxx", "0000000000000000"
            return True
    return False


def scan_secrets(files: Iterable[FileChange]) -> list[tuple[str, int, str]]:
    """Return ``(path, line_no, pattern_name)`` for suspicious *added* lines."""
    hits: list[tuple[str, int, str]] = []
    for change in files:
        if change.binary or change.status == "deleted":
            continue
        for lineno, line in change.added_lines:
            for name, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.groupdict().get("value") if match.groupdict() else None
                if name == "generic-assignment" and _looks_like_placeholder(line, value):
                    continue
                hits.append((change.path, lineno, name))
                break
    return hits


def risk_signals(files: list[FileChange], totals: dict[str, int]) -> list[RiskSignal]:
    signals: list[RiskSignal] = []

    env_files = sorted(f.path for f in files if Path(f.path).name in ENV_FILE_NAMES and f.status != "deleted")
    if env_files:
        signals.append(RiskSignal(
            "HIGH", "env-file-tracked",
            "ملف بيئة نصي (plaintext .env) داخل التغييرات — يجب ألا يُتتبع أبداً",
            env_files,
        ))

    hits = scan_secrets(files)
    if hits:
        signals.append(RiskSignal(
            "HIGH", "secret-like-addition",
            "أسطر مضافة تشبه مفاتيح/أسراراً (القيمة غير معروضة عمداً)",
            sorted({f"{path}:{lineno} ({name})" for path, lineno, name in hits}),
        ))

    sensitive = sorted(f.path for f in files if _is_sensitive(f.path))
    if sensitive:
        signals.append(RiskSignal(
            "MEDIUM", "sensitive-path",
            "مسارات حساسة تم تعديلها (sandbox / tools / governance / CI / Docker / التبعيات) — تحتاج مراجعة بشرية",
            sensitive,
        ))

    code_files = [f for f in files if f.category == "code" and f.path.startswith(CODE_ROOTS) and not f.binary]
    test_files = [f for f in files if f.category == "tests"]
    if code_files and not test_files:
        signals.append(RiskSignal(
            "MEDIUM", "code-without-tests",
            "تغييرات في الكود بدون أي تحديث تحت `tests/`",
            sorted(f.path for f in code_files)[:MAX_LISTED_FILES],
        ))

    deleted_tests = sorted(f.path for f in test_files if f.status == "deleted")
    if deleted_tests:
        signals.append(RiskSignal("MEDIUM", "tests-deleted", "تم حذف ملفات اختبار", deleted_tests))

    binaries = sorted(f.path for f in files if f.binary)
    if binaries:
        signals.append(RiskSignal("INFO", "binary-files", "ملفات ثنائية لا يمكن مراجعتها نصياً", binaries))

    if totals["added"] + totals["removed"] > LARGE_DIFF_LINES:
        signals.append(RiskSignal(
            "INFO", "large-diff",
            f"حجم التغيير كبير ({totals['added'] + totals['removed']} سطر) — يُفضّل تقسيمه",
        ))

    signals.sort(key=lambda s: -LEVEL_ORDER[s.level])
    return signals


def overall_level(signals: list[RiskSignal]) -> str:
    if any(s.level == "HIGH" for s in signals):
        return "HIGH"
    if any(s.level == "MEDIUM" for s in signals):
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def summarize(files: list[FileChange]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    totals = {
        "files": len(files),
        "added": sum(f.added for f in files),
        "removed": sum(f.removed for f in files),
        "files_added": sum(1 for f in files if f.status == "added"),
        "files_deleted": sum(1 for f in files if f.status == "deleted"),
        "files_renamed": sum(1 for f in files if f.status == "renamed"),
        "files_modified": sum(1 for f in files if f.status == "modified"),
        "files_binary": sum(1 for f in files if f.binary),
    }
    categories: dict[str, dict[str, int]] = {}
    for change in files:
        bucket = categories.setdefault(change.category, {"files": 0, "added": 0, "removed": 0})
        bucket["files"] += 1
        bucket["added"] += change.added
        bucket["removed"] += change.removed
    return totals, categories


# --------------------------------------------------------------------------- #
# Benchmark (remote Arena endpoint — opt-in)
# --------------------------------------------------------------------------- #
def run_benchmark(payload: dict[str, Any], diff_text: str, env: dict[str, str]) -> dict[str, Any]:
    mode = (env.get("ARENA_EVAL_MODE") or "auto").strip().lower()
    url = (env.get("ARENA_API_URL") or "").strip()
    key = (env.get("ARENA_API_KEY") or "").strip()

    if mode == "mock":
        return {
            "status": "MOCKED",
            "score": None,
            "mode": "mock",
            "detail": "وضع تجريبي لاختبار خط الأنابيب فقط — لا يمثّل نتيجة تقييم حقيقية",
        }
    if mode == "offline" or not url or not key:
        missing = [name for name, value in (("ARENA_API_URL", url), ("ARENA_API_KEY", key)) if not value]
        reason = "ARENA_EVAL_MODE=offline" if mode == "offline" else "لم يُضبط " + " / ".join(missing)
        return {"status": "SKIPPED", "score": None, "mode": "offline", "detail": reason}

    max_bytes = int(env.get("ARENA_MAX_DIFF_BYTES") or DEFAULT_MAX_DIFF_BYTES)
    body = dict(payload)
    body["diff"] = diff_text[:max_bytes]
    body["diff_truncated"] = len(diff_text) > max_bytes
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "nimna-arena-diff-eval/1",
        },
    )
    timeout = float(env.get("ARENA_TIMEOUT") or DEFAULT_TIMEOUT_S)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL comes from CI secret
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"status": "ERROR", "score": None, "mode": "remote", "detail": f"HTTP {exc.code} من خادم Arena"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "ERROR", "score": None, "mode": "remote", "detail": f"تعذّر الوصول إلى Arena: {exc.__class__.__name__}"}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "ERROR", "score": None, "mode": "remote", "detail": "ردّ Arena ليس JSON صالحاً"}
    if not isinstance(parsed, dict):
        return {"status": "ERROR", "score": None, "mode": "remote", "detail": "ردّ Arena بصيغة غير متوقعة"}

    status = str(parsed.get("status") or parsed.get("verdict") or "UNKNOWN").upper()
    if status in {"PASSED", "OK", "SUCCESS"}:
        status = "PASS"
    elif status in {"FAILED", "REJECTED"}:
        status = "FAIL"
    if status not in STATUS_ICON:
        status = "UNKNOWN"
    score = parsed.get("score")
    detail = parsed.get("summary") or parsed.get("notes") or parsed.get("detail") or ""
    return {"status": status, "score": score, "mode": "remote", "detail": str(detail)[:2000]}


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _code_list(items: list[str], limit: int = 12) -> str:
    shown = ", ".join(f"`{item}`" for item in items[:limit])
    extra = len(items) - limit
    return shown + (f" … (+{extra})" if extra > 0 else "")


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["metrics"]
    bench = report["benchmark"]
    risk = report["risk"]
    ctx = report["context"]
    head = (ctx.get("head_sha") or "")[:12] or "HEAD"

    lines: list[str] = ["### 🎯 Arena Evaluation Results — تقييم الفروقات", ""]
    lines.append(
        f"**Base:** `{ctx.get('base_ref') or '?'}` · **Head:** `{head}` · "
        f"**Arena mode:** `{bench['mode']}`"
    )
    lines.append("")

    if totals["files"] == 0:
        lines.append("لا توجد تعديلات لفحصها (No Diff found).")
        return "\n".join(lines) + "\n"

    score = f" (Score: {bench['score']})" if bench.get("score") is not None else ""
    bench_cell = f"{STATUS_ICON.get(bench['status'], '❔')} **{bench['status']}**{score}"
    if bench.get("detail"):
        bench_cell += f" — {bench['detail']}"
    lines += [
        "| المقياس (Metric) | النتيجة (Value) |",
        "| :--- | :--- |",
        (
            f"| **الملفات المتغيرة (Files changed)** | `{totals['files']}` "
            f"(added {totals['files_added']} · modified {totals['files_modified']} · "
            f"deleted {totals['files_deleted']} · renamed {totals['files_renamed']} · "
            f"binary {totals['files_binary']}) |"
        ),
        f"| **عدد الأسطر المضافة (Lines added)** | `+{totals['added']}` |",
        f"| **عدد الأسطر المحذوفة (Lines removed)** | `-{totals['removed']}` |",
        f"| **حالة الـ Arena Benchmark** | {bench_cell} |",
        f"| **مستوى المخاطر (Risk level)** | {LEVEL_ICON[risk['level']]} **{risk['level']}** |",
        "",
    ]

    lines += ["**التوزيع حسب الفئة (By category):**", "", "| الفئة | ملفات | + | - |", "| :--- | ---: | ---: | ---: |"]
    for name, bucket in sorted(report["categories"].items(), key=lambda kv: -kv[1]["files"]):
        lines.append(f"| {name} | {bucket['files']} | {bucket['added']} | {bucket['removed']} |")
    lines.append("")

    lines.append("**إشارات المخاطر (Risk signals):**")
    if risk["signals"]:
        for signal in risk["signals"]:
            entry = f"- {LEVEL_ICON[signal['level']]} **{signal['level']}** `{signal['code']}` — {signal['message']}"
            if signal["files"]:
                entry += ": " + _code_list(signal["files"])
            lines.append(entry)
    else:
        lines.append("- 🟢 لا توجد إشارات مخاطر من الفحص الثابت.")
    lines.append("")

    lines += [
        "**ملاحظات التقييم:**",
        f"- تم فحص التغييرات ومقارنتها بالفرع الأساسي `{ctx.get('base_ref') or '?'}`.",
        "- `SKIPPED` / `MOCKED` / `UNKNOWN` لا تعني نجاحاً — لا يُرفع `UNKNOWN` إلى `PASS` تلقائياً "
        "(راجع `docs/VERIFICATION-MATRIX.md`).",
        "- هذا الفحص ثابت (static) ولا يستبدل `pytest` أو بوابة `00-integrity`.",
        "",
    ]

    files = report["files"]
    lines += [f"<details><summary>الملفات ({len(files)})</summary>", "", "| الملف | الحالة | + | - |", "| :--- | :--- | ---: | ---: |"]
    for change in files[:MAX_LISTED_FILES]:
        label = change["status"] + (" (binary)" if change["binary"] else "")
        if change["status"] == "renamed" and change.get("old_path"):
            label += f" ← `{change['old_path']}`"
        lines.append(f"| `{change['path']}` | {label} | {change['added']} | {change['removed']} |")
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"| … | +{len(files) - MAX_LISTED_FILES} more | | |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build_report(diff_text: str, env: dict[str, str] | None = None, *,
                 base_ref: str | None = None, head_sha: str | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    files = parse_unified_diff(diff_text)
    totals, categories = summarize(files)
    signals = risk_signals(files, totals)
    context = {
        "repository": env.get("GITHUB_REPOSITORY") or None,
        "base_ref": base_ref or env.get("ARENA_BASE_REF") or env.get("GITHUB_BASE_REF") or None,
        "head_sha": head_sha or env.get("ARENA_HEAD_SHA") or env.get("GITHUB_SHA") or None,
        "pr_number": env.get("ARENA_PR_NUMBER") or None,
    }
    report: dict[str, Any] = {
        "version": REPORT_VERSION,
        "context": context,
        "metrics": totals,
        "categories": categories,
        "risk": {"level": overall_level(signals), "signals": [asdict(s) for s in signals]},
        "files": [f.public_dict() for f in files],
    }
    if totals["files"] == 0:
        report["benchmark"] = {"status": "SKIPPED", "score": None, "mode": "offline", "detail": "لا يوجد diff"}
    else:
        payload = {k: v for k, v in report.items() if k != "files"}
        payload["files"] = report["files"][:MAX_LISTED_FILES]
        report["benchmark"] = run_benchmark(payload, diff_text, env)
    return report


def _read_diff(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a git diff and emit a Markdown/JSON Arena report.")
    parser.add_argument("--diff_file", "--diff-file", dest="diff_file", required=True,
                        help="Path to a unified diff, or '-' for stdin")
    parser.add_argument("--output", help="Write the Markdown report here (default: stdout)")
    parser.add_argument("--json-output", dest="json_output", help="Also write the machine-readable JSON report here")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown",
                        help="Format printed to stdout / --output (default: markdown)")
    parser.add_argument("--base-ref", dest="base_ref", help="Base ref label (default: $ARENA_BASE_REF / $GITHUB_BASE_REF)")
    parser.add_argument("--head-sha", dest="head_sha", help="Head sha label (default: $ARENA_HEAD_SHA / $GITHUB_SHA)")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 2 when a HIGH risk signal exists or the remote benchmark reports FAIL")
    args = parser.parse_args(argv)

    try:
        diff_text = _read_diff(args.diff_file)
    except OSError as exc:
        print(f"cannot read diff: {exc}", file=sys.stderr)
        return 1

    report = build_report(diff_text, base_ref=args.base_ref, head_sha=args.head_sha)
    markdown = render_markdown(report)
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

    primary = json_text if args.format == "json" else markdown
    if args.output:
        Path(args.output).write_text(primary, encoding="utf-8")
    else:
        sys.stdout.write(primary)
    if args.json_output:
        Path(args.json_output).write_text(json_text, encoding="utf-8")

    if args.strict and (report["risk"]["level"] == "HIGH" or report["benchmark"]["status"] == "FAIL"):
        print("strict mode: HIGH risk signal or benchmark FAIL", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
