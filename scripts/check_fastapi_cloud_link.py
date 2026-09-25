#!/usr/bin/env python3
"""Falsifiable gate for the GitHub ↔ FastAPI Cloud link (offline).

لا يتصل بـFastAPI Cloud ولا بـGitHub. يفحص العقد في المستودع:
entrypoint، مسار الصحة، عقد البيئة، الـworkflow، و`.fastapicloudignore`.

    python scripts/check_fastapi_cloud_link.py     # CI step in 00-integrity.yml

Exit 0 = العقد سليم.
Exit 1 = فشل واحد على الأقل، مع سطر لكل سبب.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "fastapi-cloud.yaml"
CONFIG_PY = ROOT / "nimna" / "config.py"
WORKFLOW = ROOT / ".github" / "workflows" / "fastapi-cloud-deploy.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
IGNORE = ROOT / ".fastapicloudignore"
PYTHON_VERSION = ROOT / ".python-version"
PYPROJECT = ROOT / "pyproject.toml"
DOCS = ROOT / "docs" / "DEPLOY-FASTAPI-CLOUD.md"

SECRET_NAME = re.compile(r"(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD|REDIS_URL)$")
SECRET_LITERAL = re.compile(
    r"\b(AIza[0-9A-Za-z_\-]{20,}|AQ\.[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-)\b"
)
ENV_READ = re.compile(
    r"(?:_env(?:_bool|_int_unvalidated|_int|_float|_optional_float|_list)?|_key_source|os\.getenv"
    r"|os\.environ\.get|_env_flag)\(\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']"
)
FORBIDDEN_IGNORE = re.compile(r"^(skills/?|\*\.md|nimna/?|pyproject\.toml)$")
REQUIRED_IGNORE = ("tests/", ".github/")
REQUIRED_HARD_ENV = {
    "NIMNA_ENV": "production",
    "MAX_SPEND_USD": "0",
    "AGENT_AUTO_APPROVE": "false",
    "MODEL_PROVIDER": "gemini",
    "SANDBOX_BACKEND": "subprocess",
    "BROWSER_USE_ENABLED": "false",
    "COMPUTER_ENABLED": "false",
}
REQUIRED_SECRETS = {"NIMNA_API_KEY", "GEMINI_API_KEY"}
ALLOWED_SYNC = {"github_actions", "github_app"}
DEPLOY_MARKERS = (
    "fastapi deploy",
    "FASTAPI_CLOUD_TOKEN",
    "FASTAPI_CLOUD_APP_ID",
    "fastapi-cloud-cli",
)
AUTO_TRIGGERS = {"push", "pull_request"}


def _norm_model(model_id: object) -> str:
    """Same canonical form as nimna.models.registry.normalise_model_id."""
    text = str(model_id or "").strip().lower()
    return text[len("models/"):] if text.startswith("models/") else text


def _default_free_tier_models() -> set[str]:
    """Code default of GEMINI_FREE_TIER_MODELS.

    `nimna/config.py` is stdlib-only, so it is loaded standalone (not via the
    `nimna` package) — the gate must keep working with just PyYAML installed.
    """
    try:
        spec = importlib.util.spec_from_file_location("_nimna_config_for_gate", CONFIG_PY)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # dataclasses resolve string annotations through sys.modules[__name__]
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        declared = getattr(module, "DEFAULT_GEMINI_FREE_TIER_MODELS", ())
    except Exception:
        declared = ()
    return {_norm_model(item) for item in declared} - {""}


def _code_env_names() -> set[str]:
    names: set[str] = set()
    for pkg in ("nimna", "security"):
        base = ROOT / pkg
        if not base.is_dir():
            continue
        for source in base.rglob("*.py"):
            names.update(ENV_READ.findall(source.read_text(encoding="utf-8", errors="replace")))
    return names


def _declared_routes() -> set[str]:
    app_py = ROOT / "nimna" / "api" / "app.py"
    if not app_py.is_file():
        return set()
    text = app_py.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"@app\.(?:get|post|put|delete|route)\(\s*[\"']([^\"']+)[\"']", text))


def _check_ignore(errors: list[str]) -> None:
    if not IGNORE.is_file():
        errors.append(".fastapicloudignore مفقود — بدونها تُرفع tests/ وdocs/ بلا حاجة")
        return
    lines = [
        line.strip()
        for line in IGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for line in lines:
        if FORBIDDEN_IGNORE.match(line):
            errors.append(
                f".fastapicloudignore: القاعدة {line!r} تمنع رفع كود التشغيل "
                "(skills/ وnimna/ وSKILL.md إلزامية)"
            )
    for needed in REQUIRED_IGNORE:
        if needed not in lines:
            errors.append(f".fastapicloudignore: يجب استبعاد {needed} من الرفع")


def _check_pyproject(expected_entrypoint: str, expected_python: str, errors: list[str]) -> None:
    if not PYPROJECT.is_file():
        errors.append("pyproject.toml مفقود")
        return
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = [str(item) for item in data.get("project", {}).get("dependencies", [])]
    if not any(item.startswith("fastapi") and "[standard]" in item for item in deps):
        errors.append("pyproject.toml: fastapi[standard] إلزامي لنشر FastAPI Cloud")
    requires = str(data.get("project", {}).get("requires-python") or "")
    if not requires:
        errors.append("pyproject.toml: requires-python مفقود")
    tool = data.get("tool", {}).get("fastapi", {})
    entry = str(tool.get("entrypoint") or "")
    if entry != expected_entrypoint:
        errors.append(
            f"pyproject.toml [tool.fastapi] entrypoint={entry!r} "
            f"— المتوقع {expected_entrypoint!r}"
        )
    module, _, attr = expected_entrypoint.partition(":")
    module_path = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    if not module_path.is_file():
        errors.append(f"entrypoint module missing: {module_path.relative_to(ROOT)}")
    else:
        source = module_path.read_text(encoding="utf-8", errors="replace")
        if attr and attr not in source:
            errors.append(f"{module_path.relative_to(ROOT)}: لا يعرّف {attr}")
    if not PYTHON_VERSION.is_file():
        errors.append(".python-version مفقود — FastAPI Cloud يحتاجه لتحديد CPython")
    else:
        pinned = PYTHON_VERSION.read_text(encoding="utf-8").strip()
        if pinned != expected_python:
            errors.append(
                f".python-version={pinned!r} — العقد يثبت {expected_python!r} "
                "(يطابق Dockerfile وCI)"
            )


def _wf_label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def _on_block(data: dict):
    """GitHub `on:` becomes YAML 1.1 boolean True under PyYAML — read both keys."""
    if not isinstance(data, dict):
        return None
    on = data.get("on")
    if on is None:
        on = data.get(True)
    return on


def _workflow_on_triggers(data: dict) -> set[str]:
    on = _on_block(data)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    if isinstance(on, dict):
        return {str(key) for key in on}
    return set()


def _glob_hits_branch(pattern: str, branch: str) -> bool:
    import fnmatch

    pattern = str(pattern).strip().lstrip("refs/heads/")
    if pattern in {branch, "*", "**"}:
        return True
    return fnmatch.fnmatch(branch, pattern)


def _event_hits_branch(event, branch: str) -> bool:
    """True when this GitHub event can fire for `branch` (the GitHub App default).

    Bare `on: push` (no filter) hits every branch, including main.
    `branches: [staging]` or `branches-ignore: [main]` do not conflict.
    """
    if event is None or event is True:
        return True
    if isinstance(event, str):
        return _glob_hits_branch(event, branch)
    if isinstance(event, list):
        return any(_event_hits_branch(item, branch) for item in event)
    if not isinstance(event, dict):
        return True
    if "branches" in event:
        names = event.get("branches") or []
        if isinstance(names, str):
            names = [names]
        return any(_glob_hits_branch(str(name), branch) for name in names)
    if "branches-ignore" in event:
        ignored = event.get("branches-ignore") or []
        if isinstance(ignored, str):
            ignored = [ignored]
        return not any(_glob_hits_branch(str(name), branch) for name in ignored)
    return True


def _auto_triggers_hitting_branch(data: dict, branch: str) -> set[str]:
    on = _on_block(data)
    hits: set[str] = set()
    if isinstance(on, str):
        if on in AUTO_TRIGGERS:
            hits.add(on)
        return hits
    if isinstance(on, list):
        return {str(item) for item in on if str(item) in AUTO_TRIGGERS}
    if not isinstance(on, dict):
        return hits
    for event_name in AUTO_TRIGGERS:
        if event_name in on and _event_hits_branch(on.get(event_name), branch):
            hits.add(event_name)
    return hits


def _text_deploys(text: str) -> bool:
    return any(marker in text for marker in DEPLOY_MARKERS)


def _local_workflow_path(uses: str) -> Path | None:
    raw = uses.split("@", 1)[0].strip()
    for prefix in ("./.github/workflows/", ".github/workflows/"):
        if raw.startswith(prefix):
            return WORKFLOWS_DIR / Path(raw).name
    return None


def _local_action_dir(uses: str) -> Path | None:
    raw = uses.split("@", 1)[0].strip()
    for prefix in ("./.github/actions/", ".github/actions/"):
        if raw.startswith(prefix):
            return WORKFLOWS_DIR.parent / "actions" / Path(raw).name
    return None


def _iter_job_uses_and_runs(data: dict):
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if isinstance(uses, str):
            yield ("uses", uses)
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("uses"), str):
                yield ("uses", step["uses"])
            if isinstance(step.get("run"), str):
                yield ("run", step["run"])
            env = step.get("env")
            if isinstance(env, dict):
                for value in env.values():
                    yield ("env", str(value))


def _path_deploys_fastapi_cloud(path: Path, seen: set[Path]) -> bool:
    """True if this workflow/action (or a local `uses:` it calls) deploys.

    Follows reusable workflows and composite actions inside the repo. External
    `owner/repo/.github/workflows/...@ref` cannot be inspected offline — a
    marker in the `uses:` string still counts.
    """
    resolved = path.resolve() if path.exists() else path
    if resolved in seen:
        return False
    if not path.is_file():
        # composite action directory
        for name in ("action.yml", "action.yaml"):
            candidate = path / name
            if candidate.is_file():
                return _path_deploys_fastapi_cloud(candidate, seen)
        return False
    seen.add(resolved)
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return _text_deploys(text)
    if not isinstance(data, dict):
        return _text_deploys(text)
    for kind, value in _iter_job_uses_and_runs(data):
        if kind in {"run", "env"} and _text_deploys(value):
            return True
        if kind == "uses":
            if _text_deploys(value):
                return True
            local_wf = _local_workflow_path(value)
            if local_wf is not None and _path_deploys_fastapi_cloud(local_wf, seen):
                return True
            local_action = _local_action_dir(value)
            if local_action is not None and _path_deploys_fastapi_cloud(local_action, seen):
                return True
    # composite action body
    runs = data.get("runs")
    if isinstance(runs, dict):
        for step in runs.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("run"), str) and _text_deploys(step["run"]):
                return True
            env = step.get("env")
            if isinstance(env, dict) and any(_text_deploys(str(v)) for v in env.values()):
                return True
            if isinstance(step.get("uses"), str):
                nested = _local_action_dir(step["uses"]) or _local_workflow_path(step["uses"])
                if nested is not None and _path_deploys_fastapi_cloud(nested, seen):
                    return True
    return False


def _check_all_workflows(sync_mode: str, default_branch: str, errors: list[str]) -> None:
    """Fail CI (exit 1) if *any* workflow would auto-deploy to FastAPI Cloud on `main`.

    CI jobs may use on.push / on.pull_request. That is allowed. Combining those
    triggers *when they can fire for the default branch* with a FastAPI Cloud
    deploy step (this file, a reusable workflow, or a composite action) is not.
    `branches: [staging]` / `branches-ignore: [main]` are not a conflict.
    """
    if not WORKFLOWS_DIR.is_dir():
        errors.append(".github/workflows مفقود")
        return
    files = sorted(list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml")))
    if not files:
        errors.append(".github/workflows فارغ")
        return
    for path in files:
        text = path.read_text(encoding="utf-8")
        label = _wf_label(path)
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"{label}: YAML غير صالح: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{label}: الجذر ليس خريطة")
            continue
        deploys = _path_deploys_fastapi_cloud(path, set())
        if ("continue-on-error" in text or "|| true" in text) and deploys:
            errors.append(f"{label}: قناع فشل (continue-on-error / || true) ممنوع على مسار النشر")
        if SECRET_LITERAL.search(text):
            errors.append(f"{label}: شكل مفتاح سرّي في النص — ممنوع")
        if sync_mode != "github_app" or not deploys:
            continue
        conflict = _auto_triggers_hitting_branch(data, default_branch)
        if conflict:
            errors.append(
                f"{label}: نشر FastAPI Cloud مع {sorted(conflict)} على فرع {default_branch!r} "
                "— المزامنة التلقائية مسؤولية fastapi-cloud[bot] فقط"
            )


def _check_workflow(workflow_rel: str, default_branch: str, sync_mode: str, errors: list[str]) -> None:
    path = ROOT / workflow_rel
    if not path.is_file():
        errors.append(f"workflow مفقود: {workflow_rel}")
        return
    text = path.read_text(encoding="utf-8")
    for needle in (
        "FASTAPI_CLOUD_TOKEN",
        "FASTAPI_CLOUD_APP_ID",
        "uv run fastapi deploy",
        default_branch,
        "workflow_dispatch",
    ):
        if needle not in text:
            errors.append(f"{workflow_rel}: ناقص {needle!r}")
    if "continue-on-error" in text or "|| true" in text:
        errors.append(f"{workflow_rel}: قناع فشل (continue-on-error / || true) ممنوع")
    if SECRET_LITERAL.search(text):
        errors.append(f"{workflow_rel}: شكل مفتاح سرّي في النص — ممنوع")
    if sync_mode == "github_app" and re.search(r"(?ms)^on:.*?^\s+push:", text):
        errors.append(
            f"{workflow_rel}: sync_mode=github_app يمنع on.push — "
            "fastapi-cloud[bot] يزامن main مسبقاً (نشر مزدوج)"
        )


def _check_env(entries: list, errors: list[str]) -> None:
    known = _code_env_names()
    seen: set[str] = set()
    values: dict[str, str] = {}
    for index, entry in enumerate(entries):
        where = f"env[{index}]"
        if not isinstance(entry, dict) or "key" not in entry:
            errors.append(f"{where}: يحتاج `key`")
            continue
        key = str(entry["key"])
        where = f"env {key}"
        if key in seen:
            errors.append(f"{where}: مكرر")
        seen.add(key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            errors.append(f"{where}: اسم المتغير يجب أن يكون UPPER_SNAKE")
        is_secret = bool(entry.get("secret"))
        if "value" in entry and not isinstance(entry.get("value"), str):
            errors.append(
                f"{where}: value يجب أن تكون نصاً مقتبساً (ليست {type(entry.get('value')).__name__})"
            )
        if is_secret and "value" in entry:
            errors.append(f"{where}: سرّ لا يُكتب كـ value في العقد — secret: true فقط")
        if SECRET_NAME.search(key) and not is_secret:
            errors.append(f"{where}: مفتاح سرّي يجب secret: true بلا قيمة")
        if not is_secret and "value" not in entry:
            errors.append(f"{where}: متغير غير سرّي يحتاج value")
        if key not in known and not SECRET_NAME.search(key):
            errors.append(f"{where}: لا يقرئه الكود في nimna/ أو security/ — متغير ميت أو اسم خاطئ")
        if isinstance(entry.get("value"), str):
            values[key] = entry["value"]
    for key in REQUIRED_SECRETS:
        if key not in seen:
            errors.append(f"env: السرّ الإلزامي {key} مفقود")
        elif not any(
            isinstance(item, dict) and item.get("key") == key and item.get("secret") is True
            for item in entries
        ):
            errors.append(f"env {key}: يجب secret: true")
    for key, expected in REQUIRED_HARD_ENV.items():
        if values.get(key) != expected:
            errors.append(f"env {key}: يجب {expected!r} (وجد {values.get(key)!r})")
    # ميزانية صفر لا تخدم إلا نموذجاً مُعلَناً مجانياً. مرآة CI لـ
    # CostGuard.assert_boot_policy: تبديل GEMINI_MODEL بلا توسيع
    # GEMINI_FREE_TIER_MODELS ينشر بناءً يرفض الإقلاع (CostPolicyError).
    if values.get("MODEL_PROVIDER") == "gemini" and values.get("MAX_SPEND_USD") == "0":
        model = _norm_model(values.get("GEMINI_MODEL", ""))
        raw_declared = values.get("GEMINI_FREE_TIER_MODELS")
        if raw_declared is None:
            declared = _default_free_tier_models()
        else:
            declared = {_norm_model(item) for item in raw_declared.split(",")} - {""}
        if model and model not in declared:
            errors.append(
                f"env GEMINI_MODEL={model!r}: ليس ضمن GEMINI_FREE_TIER_MODELS "
                f"({', '.join(sorted(declared)) or 'فارغة'}) مع MAX_SPEND_USD=0 — الإقلاع سيُرفض "
                "(CostPolicyError). أضِفه إلى GEMINI_FREE_TIER_MODELS أو اضبط "
                "MODEL_COST_*_USD_PER_1K مع MAX_SPEND_USD موجبة"
            )
    docker_paths = {key: values[key] for key in ("SKILLS_DIR", "WORKSPACE_DIR", "DB_PATH") if key in values}
    for key, value in docker_paths.items():
        if value.startswith("/app/"):
            errors.append(
                f"env {key}={value!r}: مسار Docker — FastAPI Cloud يشغّل الشجرة كما هي "
                "(skills / workspace / data/nimna.db)"
            )


def _check_link(link: dict, errors: list[str]) -> None:
    expected = {
        "github_owner": "sayedelazameydesign-crypto",
        "github_repo": "cela.nimna",
        "github_url": "https://github.com/sayedelazameydesign-crypto/cela.nimna",
        "fastapi_cloud_team_slug": "sayedelazameydesign-424e4d8c",
        "fastapi_cloud_team_url": (
            "https://dashboard.fastapicloud.com/sayedelazameydesign-424e4d8c/apps"
        ),
        "default_branch": "main",
        "python": "3.11",
        "entrypoint": "nimna.api.app:app",
        "health_path": "/api/health",
        "sync_mode": "github_app",
        "primary_app": "celanimna-3ffa6b22",
        "primary_url": "https://celanimna-3ffa6b22.fastapicloud.dev",
        "spare_app": "celanimna",
        "spare_role": "legacy-duplicate",
        "spare_disconnect_todo": "open",
        "spare_disconnect_owner": "sayedelazameydesign-crypto",
        "spare_disconnect_since": "2026-09-25",
    }
    if not isinstance(link, dict):
        errors.append("link: يجب أن يكون خريطة")
        return
    for key, value in expected.items():
        got = link.get(key)
        if got != value:
            errors.append(f"link.{key}: وجد {got!r} — المتوقع {value!r}")
    sync_mode = link.get("sync_mode")
    if sync_mode not in ALLOWED_SYNC:
        errors.append(f"link.sync_mode={sync_mode!r} — يجب {' أو '.join(sorted(ALLOWED_SYNC))}")
    health = str(link.get("health_path") or "")
    routes = _declared_routes()
    if health and routes and health not in routes:
        errors.append(
            f"link.health_path={health!r} ليس مساراً معلناً في nimna/api/app.py"
        )
    docs_rel = str(link.get("docs") or "docs/DEPLOY-FASTAPI-CLOUD.md")
    docs_path = ROOT / docs_rel
    if not docs_path.is_file():
        errors.append(f"دليل الربط مفقود: {docs_rel}")
    else:
        text = docs_path.read_text(encoding="utf-8")
        for needle in (
            expected["github_url"],
            expected["fastapi_cloud_team_url"],
            expected["primary_url"],
            "FASTAPI_CLOUD_TOKEN",
            "FASTAPI_CLOUD_APP_ID",
            "Source Repository",
            "fastapi-cloud[bot]",
            "ليست بيئة staging",
            "legacy-duplicate",
            "celanimna",
            "إجراء معلّق",
            "sayedelazameydesign-crypto",
        ):
            if needle not in text:
                errors.append(f"{docs_rel}: ناقص {needle!r}")
        todo = link.get("spare_disconnect_todo")
        if todo == "open" and "- [ ]" not in text:
            errors.append(f"{docs_rel}: todo=open يتطلب صندوق `- [ ]` (لا تُزل التتبع)")
        if todo == "done" and "- [x]" not in text and "- [X]" not in text:
            errors.append(f"{docs_rel}: todo=done يتطلب `- [x]`")


def main(argv: list[str] | None = None) -> int:
    del argv
    if not CONTRACT.is_file():
        print("fastapi-cloud link FAIL: fastapi-cloud.yaml مفقود — لا ربط بلا عقد", file=sys.stderr)
        return 1
    raw = CONTRACT.read_text(encoding="utf-8")
    errors: list[str] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if SECRET_LITERAL.search(line):
            errors.append(f"fastapi-cloud.yaml:{number}: شكل مفتاح سرّي في النص — ممنوع")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(f"fastapi-cloud link FAIL: YAML غير صالح: {exc}", file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        errors.append("الجذر يجب أن يكون خريطة (link / env)")
        doc = {}
    unknown = set(doc) - {"link", "env"}
    if unknown:
        errors.append(f"مفاتيح جذر غير معروفة: {sorted(unknown)}")
    link = doc.get("link") if isinstance(doc.get("link"), dict) else {}
    _check_link(link if isinstance(link, dict) else {}, errors)
    env = doc.get("env")
    if not isinstance(env, list) or not env:
        errors.append("`env` مفقودة أو فارغة")
    else:
        _check_env(env, errors)
    entrypoint = str((link or {}).get("entrypoint") or "nimna.api.app:app")
    python = str((link or {}).get("python") or "3.11")
    workflow = str((link or {}).get("workflow") or ".github/workflows/fastapi-cloud-deploy.yml")
    branch = str((link or {}).get("default_branch") or "main")
    sync_mode = str((link or {}).get("sync_mode") or "")
    _check_pyproject(entrypoint, python, errors)
    _check_workflow(workflow, branch, sync_mode, errors)
    _check_all_workflows(sync_mode, branch, errors)
    _check_ignore(errors)
    if errors:
        print("fastapi-cloud link FAIL:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    n_env = len(env) if isinstance(env, list) else 0
    print(
        f"fastapi-cloud link PASS: {n_env} متغير بيئة، entrypoint={entrypoint}, "
        f"sync={link.get('sync_mode') if isinstance(link, dict) else '?'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
