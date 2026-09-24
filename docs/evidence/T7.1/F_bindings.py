#!/usr/bin/env python3
"""T7.1-F — deterministic enumeration of gateway-binding & execution sites.

التوجيه الثلاثي عند الربط (إلزامي):
كل موضع في كود الإنتاج يلمس ربط الـ gateway أو تنفيذه إما
  (1) مُدرَج في القاعدة الذهبية 10+8 (GOLDEN أدناه)، أو
  (2) استثناء موثَّق بسبب صريح في EXCEPTIONS،
غياب التوجيه ⇒ السكربت يفشل (exit 2) والتعداد يُعدّ ناقصاً.

Deterministic: fixed scan order, sorted output, no timestamps, no env.
Exit codes: 0 = complete · 2 = untriaged site(s) found · 3 = golden site
missing from code (binding regression) · 4 = both.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ["nimna"]
# سطور الإنتاج التي تلمس الربط/التنفيذ — كاشف عام واسع (لا ادعاءات سلبية)
GENERIC = re.compile(
    r"execution_gateway|gateway\.has\(|gateway\.invoke_for_agent\(|compat_tools"
    r"|NOT_IN_GATEWAY|GATEWAY_ERROR|gateway_refused|gateway_compat"
    r"|gateway is None|gateway is not None"
)

# القاعدة الذهبية: 10 مواقع ربط + 8 مواقع تنفيذ (BND-9).
# كل موقع = (kind, file,anchors) — الـ anchors أنماط أسطره كلها (موقع واحد
# قد يمتد لأسطر متعددة: فرع refusal مثلاً = سطر audit + سطر payload).
GOLDEN: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # ---- binding (10) ----
    "B01": ("binding", "core/agent.py", (r"execution_gateway: Optional\[Any\] = None",)),
    "B02": ("binding", "core/agent.py", (r"self\.execution_gateway = execution_gateway",)),
    "B03": ("binding", "core/agent.py", (r"gateway = self\.execution_gateway",)),
    "B04": ("binding", "core/agent.py", (r"gateway = getattr\(self, .execution_gateway., None\)",)),
    "B05": ("binding", "core/agent.py", (r"if gateway is None:",)),
    "B06": ("binding", "agents/base.py", (r"execution_gateway: Any = None",)),
    "B07": ("binding", "agents/base.py", (r"self\.execution_gateway = execution_gateway",)),
    "B08": ("binding", "agents/base.py", (r"gateway = getattr\(self, .execution_gateway., None\)",)),
    "B09": ("binding", "bootstrap.py", (r"execution_gateway: Optional\[Any\] = None",
                                        r"execution_gateway=execution_gateway\)")),
    "B10": ("binding", "core/planner_swarm.py", (r"execution_gateway=getattr\(parent_agent,",)),
    # ---- execution (8) ----
    "X01": ("execution", "core/agent.py", (r"outcome = gateway\.invoke_for_agent\(",)),
    "X02": ("execution", "core/agent.py", (r"elif gateway\.has\(tool\.name\):",)),
    "X03": ("execution", "core/agent.py", (r"self\._audit\(state, .gateway_error.,",
                                           r'"status": "GATEWAY_ERROR"')),
    "X04": ("execution", "core/agent.py", (r"elif tool\.name in getattr\(gateway, .compat_tools.",
                                           r"self\._audit\(state, .gateway_compat.,")),
    "X05": ("execution", "core/agent.py", (r"self\._audit\(state, .gateway_refused.,",
                                           r'"status": "NOT_IN_GATEWAY"')),
    "X06": ("execution", "agents/base.py", (r"if gateway is not None and gateway\.has\(call\.name\):",)),
    "X07": ("execution", "agents/base.py", (r"outcome = gateway\.invoke_for_agent\(",)),
    "X08": ("execution", "agents/base.py", (r"elif gateway is not None and call\.name not in getattr\(gateway, .compat_tools.",)),
}

# استثناءات موثَّقة: (file, anchor-regex) -> السبب. أي إدخال يظهر في المخرج
# تحت "exceptions" بسبب مكتوب — الاستثناء بلا سبب مرفوض بالتصميم.
EXCEPTIONS: dict[tuple[str, str], str] = {
    ("execution/gateway.py", r"compat_tools: tuple\[str, \.\.\.\] = \(\)"):
        "تعريف حقل compat_tools على الـ ExecutionGateway نفسه — نقطة إعلان "
        "القائمة الصريحة (عقد T7.1: compat مُعلَن مُدقَّق)، ليست موقع ربط ولا "
        "مسار تنفيذ؛ أي تعديل هنا يغيّر توجيه X04/X08 ويُراجع معهما.",
    ("execution/gateway.py", r"self\.compat_tools = frozenset\(compat_tools\)"):
        "إسناد حقل compat_tools داخل مُنشئ الـ gateway — نفس طبيعة الإعلان "
        "أعلاه؛ ليست موقع ربط ولا مسار تنفيذ.",
}


def head_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()[:12]
    except Exception:
        return "unknown"


def scan() -> list[dict]:
    hits: list[dict] = []
    for sub in SCAN_ROOTS:
        for py in sorted((ROOT / sub).rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            rel = py.relative_to(ROOT).as_posix()
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if GENERIC.search(line):
                    hits.append({"file": rel, "line": lineno, "text": line.strip()})
    return hits


def triage(hits: list[dict]) -> dict:
    """كل hit يُوجَّه إلى golden id أو استثناء بسبب — وإلا فهو UNTRIAGED."""
    matched: dict[str, list[dict]] = {gid: [] for gid in GOLDEN}
    untriaged: list[dict] = []
    exceptions_hit: list[dict] = []
    for h in sorted(hits, key=lambda x: (x["file"], x["line"], x["text"])):
        placed = False
        for gid, (kind, fname, anchors) in sorted(GOLDEN.items()):
            if h["file"].endswith(f"nimna/{fname}") and any(re.search(a, h["text"]) for a in anchors):
                matched[gid].append(h)
                placed = True
                break
        if placed:
            continue
        for (efile, erx), reason in sorted(EXCEPTIONS.items()):
            if h["file"].endswith(f"nimna/{efile}") and re.search(erx, h["text"]):
                exceptions_hit.append({**h, "reason": reason})
                placed = True
                break
        if not placed:
            untriaged.append(h)

    sites, missing = [], []
    for gid, (kind, fname, _anchors) in sorted(GOLDEN.items()):
        occ = matched[gid]
        entry = {"id": gid, "kind": kind, "file": f"nimna/{fname}", "triage": "enumerated"}
        if occ:
            entry["lines"] = sorted({o["line"] for o in occ})
            entry["text"] = occ[0]["text"]
        else:
            entry["lines"] = []
            missing.append(entry)
        sites.append(entry)
    return {"sites": sites, "untriaged": untriaged, "missing": missing,
            "exceptions": exceptions_hit}


def main() -> int:
    hits = scan()
    report = triage(hits)
    out = {
        "generated_from_commit": head_commit(),
        "golden_rule": "10 binding + 8 execution (BND-9); triage-at-binding mandatory",
        "binding_count": sum(1 for s in report["sites"] if s["kind"] == "binding" and s["lines"]),
        "execution_count": sum(1 for s in report["sites"] if s["kind"] == "execution" and s["lines"]),
        "sites": report["sites"],
        "untriaged": report["untriaged"],
        "missing": report["missing"],
        "exceptions": report["exceptions"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True))
    rc = 0
    if report["untriaged"]:
        rc |= 2
    if report["missing"]:
        rc |= 3
    if rc:
        print(f"ENUMERATION INCOMPLETE: untriaged={len(report['untriaged'])} "
              f"missing={len(report['missing'])}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
