#!/usr/bin/env python3
"""Falsifiable gate for `render.yaml` (offline, no Render account needed).

لماذا هذا الملف؟ `render.yaml` هو عقد نشر: خطأ واحد صغير (قيمة غير مقتبسة، مفتاح
مكتوب بخطأ إملائي، مفتاح سرّي مضمّن في النص) يجعل النشر المجاني يفشل بصمت أو
يسرّب. الاختبار لا يتصل بـRender ولا يبني صورة Docker — يفحص النص مقابل ما يقبله
Blueprint spec مقابل ما يقرؤه الكود فعلاً في `nimna/config.py`.

    python scripts/check_render_blueprint.py          # CI step in 00-integrity.yml

Exit 0 = كل الفحوص نجحت (أو لا يوجد render.yaml في هذا الفرع: SKIP).
Exit 1 = فشل واحد على الأقل، مع سطر لكل سبب.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT = ROOT / "render.yaml"

ROOT_KEYS = {"services", "databases", "envVarGroups", "projects", "ungrouped", "previews",
             "previewsEnabled"}
SERVICE_TYPES = {"web", "pserv", "worker", "cron", "keyvalue", "workflow"}
RUNTIMES = {"node", "python", "ruby", "go", "elixir", "rust", "docker", "image", "static"}
WEB_PLANS = {"free", "0.5c-512mb", "1c-2g", "2c-4g", "2c-8g", "2c-16g", "4c-8g", "4c-16g",
             "4c-32g", "8c-16g", "8c-32g", "8c-64g", "12c-24g", "12c-48g", "12c-96g"}
AUTO_DEPLOY = {"commit", "checksPass", "off"}
ENV_VALUE_SOURCES = {"value", "generateValue", "sync", "fromDatabase", "fromService", "fromGroup"}
# منافذ يحظرها Render على خدمات Free (https://render.com/docs/free)
RESERVED_FREE_PORTS = {18012, 18013, 19099}

SECRET_NAME = re.compile(r"(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD)$")
SECRET_LITERAL = re.compile(
    r"\b(AIza[0-9A-Za-z_\-]{20,}|AQ\.[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-)\b"
)
ENV_READ = re.compile(
    r"(?:_env(?:_bool|_int_unvalidated|_int|_float|_optional_float|_list)?|_key_source|os\.getenv"
    r"|os\.environ\.get|_env_flag)\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']"
)


def _code_env_names() -> set[str]:
    """Every environment name the runtime actually reads (typo detector)."""
    names: set[str] = set()
    for pkg in ("nimna", "security"):
        for source in (ROOT / pkg).rglob("*.py"):
            names.update(ENV_READ.findall(source.read_text(encoding="utf-8", errors="replace")))
    return names


def _declared_routes() -> set[str]:
    """Paths of FastAPI routes — healthCheckPath must be one of them."""
    app_py = ROOT / "nimna" / "api" / "app.py"
    if not app_py.is_file():
        return set()
    text = app_py.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"@app\.(?:get|post|put|delete|route)\(\s*[\"']([^\"']+)[\"']", text))


def check_services(services: list, errors: list[str]) -> None:
    routes = _declared_routes()
    known_env = _code_env_names()
    for index, service in enumerate(services):
        where = f"services[{index}]"
        if not isinstance(service, dict):
            errors.append(f"{where}: يجب أن يكون خريطة (mapping)")
            continue
        label = str(service.get("name") or f"{where}")
        where = f"service '{label}'"
        for required in ("name", "type"):
            if not service.get(required):
                errors.append(f"{where}: الحقل الإلزامي `{required}` مفقود")
        stype = service.get("type")
        if stype and stype not in SERVICE_TYPES:
            errors.append(f"{where}: type={stype!r} ليس من أنواع Render المعروفة {sorted(SERVICE_TYPES)}")
        runtime = service.get("runtime")
        if runtime and runtime not in RUNTIMES:
            errors.append(f"{where}: runtime={runtime!r} غير مدعوم في blueprint spec")
        if runtime == "docker":
            for native_only in ("buildCommand", "startCommand"):
                if native_only in service:
                    errors.append(f"{where}: {native_only} غير مدعوم مع runtime: docker (استخدم CMD/Docker Command)")
            dockerfile = Path(service.get("dockerfilePath") or "./Dockerfile")
            if not (ROOT / dockerfile).is_file():
                errors.append(f"{where}: dockerfilePath={dockerfile} لا يوجد في المستودع")
            context = Path(service.get("dockerContext") or ".")
            if not (ROOT / context).is_dir():
                errors.append(f"{where}: dockerContext={context} ليس دليلاً")
        plan = service.get("plan")
        if plan is not None and stype == "web" and plan not in WEB_PLANS:
            errors.append(f"{where}: plan={plan!r} ليس من خطط web services ({sorted(WEB_PLANS)[:4]}…)")
        if plan == "free":
            if "disk" in service:
                errors.append(f"{where}: قرص دائم لا يتوفر على plan: free")
            if "maintenanceMode" in service:
                errors.append(f"{where}: maintenanceMode يتطلب خطة مدفوعة")
        health = service.get("healthCheckPath")
        if health is not None:
            if not isinstance(health, str) or not health.startswith("/"):
                errors.append(f"{where}: healthCheckPath يجب أن يبدأ بـ '/'")
            elif routes and health not in routes:
                errors.append(f"{where}: healthCheckPath={health!r} ليس مساراً معلناً في nimna/api/app.py "
                              f"— الـdeploy سيُلغى بعد 15 دقيقة")
        if "autoDeploy" in service:
            errors.append(f"{where}: `autoDeploy` مهجور — استخدم autoDeployTrigger: \"off\"/\"commit\"/\"checksPass\"")
        trigger = service.get("autoDeployTrigger")
        if trigger is not None and (not isinstance(trigger, str) or trigger not in AUTO_DEPLOY):
            errors.append(f"{where}: autoDeployTrigger={trigger!r} — يجب نص من {sorted(AUTO_DEPLOY)}; "
                          f"`off` بلا اقتباس يصبح boolean في YAML 1.1")
        _check_env_vars(service, label, known_env, errors)


def _check_env_vars(service: dict, label: str, known_env: set[str], errors: list[str]) -> None:
    env_vars = service.get("envVars", [])
    if env_vars and not isinstance(env_vars, list):
        errors.append(f"service '{label}': envVars يجب أن تكون قائمة")
        return
    seen: set[str] = set()
    for entry in env_vars or []:
        if not isinstance(entry, dict) or "key" not in entry:
            errors.append(f"service '{label}': كل مدخل envVars يحتاج `key`")
            continue
        key = str(entry["key"])
        where = f"service '{label}' envVar {key}"
        if key in seen:
            errors.append(f"{where}: مكرر")
        seen.add(key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            errors.append(f"{where}: اسم المتغير يجب أن يكون UPPER_SNAKE")
        sources = set(entry) - {"key"}
        if not sources:
            errors.append(f"{where}: لا مصدر قيمة (value / sync / generateValue / from…)")
        unknown = sources - ENV_VALUE_SOURCES
        if unknown:
            errors.append(f"{where}: مفاتيح غير معروفة في blueprint spec: {sorted(unknown)}")
        value = entry.get("value")
        if "value" in entry and not isinstance(value, str):
            errors.append(f"{where}: value يجب أن تكون نصاً مُقتبساً (ليست {type(value).__name__}) "
                          f"— true/false/off/10000 بلا اقتباس تُقرأ YAML scalar")
        if SECRET_NAME.search(key) and "value" in entry:
            errors.append(f"{where}: مفتاح سرّي لا يُكتب كـ value في blueprint — استخدم sync: false")
        if SECRET_NAME.search(key) and entry.get("sync") is not False and "generateValue" not in entry:
            errors.append(f"{where}: مفتاح سرّي يجب أن يكون sync: false (أو generateValue)")
        if key not in known_env and not SECRET_NAME.search(key):
            errors.append(f"{where}: لا يقرئه الكود في nimna/ أو security/ — متغير ميت أو اسم خاطئ")


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags yet
    if not BLUEPRINT.is_file():
        print("render blueprint SKIP: لا يوجد render.yaml في هذا الفرع")
        return 0
    raw = BLUEPRINT.read_text(encoding="utf-8")
    errors: list[str] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if SECRET_LITERAL.search(line):
            errors.append(f"render.yaml:{number}: شكل مفتاح سرّي في النص — ممنوع")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:  # pragma: no cover - parse errors are obvious
        print(f"render blueprint FAIL: YAML غير صالح: {exc}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        errors.append("الجذر يجب أن يكون خريطة (services/databases/…)")
        doc = {}
    unknown_root = set(doc) - ROOT_KEYS
    if unknown_root:
        errors.append(f"مفاتيح جذر غير معروفة: {sorted(unknown_root)}")
    services = doc.get("services")
    if not isinstance(services, list) or not services:
        errors.append("`services` مفقودة أو فارغة")
    else:
        check_services(services, errors)
    if errors:
        print("render blueprint FAIL:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    n_env = sum(len(s.get("envVars") or []) for s in services if isinstance(s, dict))
    print(f"render blueprint PASS: {len(services)} خدمة، {n_env} متغير بيئة "
          f"— كل الأسماء مقرؤه في nimna/ وكل المصادر صالحة")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
