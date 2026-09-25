"""Command line interface.

    nimna chat                      interactive REPL (approvals asked inline)
    nimna ask "..."                 one-shot request
    nimna skills [list|show|validate]   (alias: nimna skills)
    nimna tools  [list]                 (alias: nimna tools)
    nimna serve [--port 8000]       FastAPI server + web UI
    nimna approvals [list|resolve]  (alias: nimna resume)
    nimna resume RUN_ID             resolve a pending run (shorthand)
    nimna doctor                    diagnose environment / keys / docker / db
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import Settings
from .core.approval import ConsolePrompt
from .core.state import AgentResult


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env(getattr(args, "env_file", ".env"))
    if getattr(args, "provider", None):
        settings.provider = args.provider
    if getattr(args, "model", None):
        if settings.provider == "gemini":
            settings.gemini_model = args.model
        else:
            settings.openai_model = args.model
    if getattr(args, "no_verify", False):
        settings.verify = False
    return settings


def _print_result(result: AgentResult, *, verbose: bool = False) -> None:
    print()
    if result.pending:
        # friendly approval preview like the spec example
        print(f"⚠️  موافقة مطلوبة: {result.pending.tool_name}")
        print(f"   الوصف: {result.pending.description}")
        print(f"   الوسائط: {json.dumps(result.pending.tool_call.arguments, ensure_ascii=False)}")
        target = result.pending.tool_call.arguments.get("path") or result.pending.tool_call.arguments.get("filename") or result.pending.tool_call.arguments.get("url") or ""
        if target:
            print(f"   الأثر: سيتم الكتابة/الوصول إلى: {target}")
        print(f"   الحل: nimna approvals resolve {result.pending.approval_id}  (أو --deny للرفض)")
        print()
    print(result.reply or result.error or "(no reply)")
    meta = []
    if result.skills_used:
        meta.append("المهارة: " + ", ".join(result.skills_used))
        # also print English for CLI users
        meta.append("skills: " + ", ".join(result.skills_used))
    if result.tool_calls:
        meta.append("tools: " + ", ".join(
            f"{c.name}{'' if c.ok else '(error)'}{'(denied)' if c.approved is False else ''}" for c in result.tool_calls
        ))
    meta.append(f"steps: {result.steps}")
    if result.usage.get("total_tokens"):
        meta.append(f"tokens: {result.usage['total_tokens']}")
    print("\n— " + " | ".join(meta))
    if verbose:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


def cmd_ask(args: argparse.Namespace) -> int:
    from .bootstrap import build_agent

    agent = build_agent(_settings(args), approval_policy=ConsolePrompt())
    # preview before execution (as requested in review §7)
    print(f"المهارة المتوقعة: سيختار الوكيل من بين {len(agent.skills)} مهارة")
    print(f"الأدوات المتاحة: {', '.join(sorted(agent.tools.names()))[:200]}")
    print(f"الموافقة مطلوبة: {'لا' if agent.settings.auto_approve else 'نعم للأدوات الحساسة'}")
    print()
    result = agent.run(args.message, session_id=args.session)
    _print_result(result, verbose=args.verbose)
    if result.status.value == "awaiting_approval":
        print(f"\nRun suspended. Resolve with: nimna approvals resolve {result.run_id}")
    return 0 if result.status.value != "error" else 1


def cmd_chat(args: argparse.Namespace) -> int:
    from .bootstrap import build_agent

    settings = _settings(args)
    agent = build_agent(settings, approval_policy=ConsolePrompt())
    session_id = args.session or uuid.uuid4().hex[:12]
    print(f"Nimna agent – provider={agent.provider.name} model={agent.provider.model} "
          f"skills={len(agent.skills)} session={session_id}")
    print("Type your request (Ctrl-D or 'exit' to quit).\n")
    while True:
        try:
            text = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit", "خروج"}:
            break
        result = agent.run(text, session_id=session_id)
        _print_result(result, verbose=args.verbose)
        print()
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    from .skills.manager import SkillManager
    from .tools import default_registry

    settings = _settings(args)
    manager = SkillManager(settings.skills_dir)
    registry = default_registry()
    cmd = getattr(args, "skills_cmd", None) or "list"
    if cmd == "list":
        for meta in manager.list():
            risk = f" [{meta.risk_level}]" if meta.risk_level != "safe" else ""
            print(f"{meta.name:20s} v{meta.version:8s}{risk:12s} {meta.description}")
            if meta.allowed_tools:
                print(f"{'':20s} tools: {', '.join(meta.allowed_tools)}")
        for name, error in manager.errors.items():
            print(f"{name:20s} ERROR: {error}", file=sys.stderr)
        return 0
    if cmd == "show":
        try:
            skill = manager.get(args.name)
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(json.dumps(skill.meta.model_dump(), ensure_ascii=False, indent=2))
        print("\n" + skill.instructions)
        return 0
    if cmd == "validate":
        report = manager.validate_all(registry_names=set(registry.names()))
        failed = False
        for name, warnings in report.items():
            status = "OK " if not warnings else ("ERR" if any(w.startswith("ERROR") for w in warnings) else "WARN")
            failed |= status == "ERR"
            print(f"[{status}] {name}")
            for warning in warnings:
                print(f"      - {warning}")
        return 1 if failed else 0
    return 2


def cmd_tools(args: argparse.Namespace) -> int:
    from .tools import default_registry

    registry = default_registry()
    for tool in registry.all():
        risk = tool.risk if tool.risk_fn is None else f"{tool.risk}/dynamic"
        print(f"{tool.name:22s} [{risk:14s}] {tool.description}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app
    from .api.security import SecurityConfigError
    from .models import CostPolicyError

    settings = _settings(args)
    try:
        app = create_app(settings)
    except (SecurityConfigError, CostPolicyError) as exc:
        print(f"nimna serve: refusing to start — {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port,
                log_level=settings.log_level.lower())
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    from .bootstrap import build_agent

    agent = build_agent(_settings(args))
    cmd = getattr(args, "approvals_cmd", None) or "list"
    if cmd == "list":
        pending = agent.pending_approvals()
        if not pending:
            print("No pending approvals.")
            return 0
        for item in pending:
            state = agent.memory.get_pending(item["id"]) or {}
            pending_info = state.get("pending") or {}
            print(f"{item['id']}  session={item['session_id']}  tool={pending_info.get('tool_name')}  "
                  f"{pending_info.get('summary', '')}")
            if pending_info.get("tool_call"):
                print(f"      args: {json.dumps(pending_info['tool_call'].get('arguments', {}), ensure_ascii=False)}")
        return 0
    if cmd == "resolve":
        try:
            result = agent.resume(args.run_id, approved=not args.deny, always=args.always)
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        _print_result(result, verbose=args.verbose)
        return 0
    return 2


def cmd_resume(args: argparse.Namespace) -> int:
    # shorthand for `nimna approvals resolve RUN_ID`
    args.approvals_cmd = "resolve"
    return cmd_approvals(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil

    settings = _settings(args)
    ok = True

    def check(label: str, passed: bool, hint: str = ""):
        nonlocal ok
        icon = "✅" if passed else "❌"
        print(f"{icon} {label}" + (f" – {hint}" if hint and not passed else f" – {hint}" if hint else ""))
        if not passed:
            ok = False

    env_file = Path(getattr(args, "env_file", ".env"))
    if env_file.is_file():
        print(f"✅ env file {env_file} – found")
    else:
        # .env is optional – env vars may provide all config (especially in CI with MODEL_PROVIDER=mock)
        print(f"ℹ️  env file {env_file} – not found (using environment variables; create from .env.example if needed)")

    # provider key – show which env var is used (review §2)
    if settings.provider == "gemini":
        has_key = bool(settings.gemini_api_key)
        src = settings.gemini_key_source or "GEMINI_API_KEY"
        label = f"Gemini key: configured via {src}" if has_key else "Gemini key: not configured"
        hint = "" if has_key else "set GEMINI_API_KEY (preferred) or GOOGLE_API_KEY in .env – https://aistudio.google.com/apikey"
        check(label + f" for {settings.gemini_model}", has_key, hint)
        if has_key:
            check(f"  ↳ NVIDIA key: {'configured via ' + settings.openai_key_source if settings.openai_api_key else 'not configured'}", True)
        # live ping if key present and requested
        if has_key and not args.offline:
            try:
                from .providers.gemini import GeminiProvider
                p = GeminiProvider(settings.gemini_api_key, settings.gemini_model, timeout=15, temperature=0)
                from .providers.base import Message
                r = p.generate([Message.user("ping")], max_tokens=10)
                check("Gemini live ping", bool(r.text or r.tool_calls), f"response: {r.text[:60]}")
            except Exception as exc:
                # redact any secret that might appear in traceback
                msg = str(exc)
                # quick string redaction for long tokens
                import re
                msg = re.sub(r"sk-[A-Za-z0-9-_]{10,}", "***REDACTED***", msg)
                check("Gemini live ping", False, msg[:200])
    elif settings.provider == "openai":
        has_key = bool(settings.openai_api_key)
        src = settings.openai_key_source or "OPENAI_API_KEY"
        label = f"OpenAI/NVIDIA key: configured via {src}" if has_key else "OpenAI/NVIDIA key: not configured"
        hint = "" if has_key else f"set OPENAI_API_KEY or NVIDIA_API_KEY for {settings.openai_base_url}"
        check(label + f" for {settings.openai_model}", has_key, hint)
        if has_key:
            check(f"  ↳ Gemini key: {'configured via ' + settings.gemini_key_source if settings.gemini_api_key else 'not configured'}", True)
        if has_key and not args.offline:
            try:
                from .providers.openai_compat import OpenAICompatibleProvider
                p = OpenAICompatibleProvider(settings.openai_api_key, settings.openai_base_url, settings.openai_model, timeout=15)
                from .providers.base import Message
                r = p.generate([Message.user("ping")], max_tokens=10)
                check("OpenAI-compatible live ping", bool(r.text or r.tool_calls), f"response: {r.text[:60]}")
            except Exception as exc:
                import re
                msg = re.sub(r"sk-[A-Za-z0-9-_]{10,}", "***REDACTED***", str(exc))
                check("OpenAI-compatible live ping", False, msg[:200])
    else:
        check(f"provider={settings.provider}", True, "mock mode – no key needed")
        # still report key status for convenience
        check(f"  ↳ Gemini key: {'configured via ' + settings.gemini_key_source if settings.gemini_api_key else 'not configured'}", True)
        check(f"  ↳ NVIDIA key: {'configured via ' + settings.openai_key_source if settings.openai_api_key else 'not configured'}", True)

    # API access control (same validation create_app() runs before booting)
    from .api.security import SecurityConfig, SecurityConfigError
    try:
        check(f"API security: {SecurityConfig.from_settings(settings).describe()}", True)
    except SecurityConfigError as exc:
        check("API security", False, str(exc))

    check(f"workspace {settings.workspace_dir}", settings.workspace_dir.exists() or True, "will be created" if not settings.workspace_dir.exists() else "exists")
    try:
        settings.ensure_dirs()
        test = settings.workspace_dir / ".nimna-write-test"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        check("workspace writable", True)
    except Exception as exc:
        check("workspace writable", False, str(exc))

    # .env permissions – skip for .env.example which intentionally contains no secrets
    if env_file.is_file() and env_file.name != ".env.example":
        try:
            mode = env_file.stat().st_mode & 0o777
            # should be 600 (or 400), not world-readable
            check(f".env permissions {oct(mode)}", mode in (0o600, 0o400), "run: chmod 600 .env")
        except Exception as exc:
            check(".env permissions", False, str(exc))

    # sqlite
    try:
        from .memory.store import MemoryStore
        m = MemoryStore(settings.db_path if str(settings.db_path) != ":memory:" else ":memory:")
        m.ensure_session("doctor")
        m.close()
        check(f"SQLite {settings.db_path}", True)
        if str(settings.db_path) != ":memory:" and Path(settings.db_path).exists():
            try:
                mode = Path(settings.db_path).stat().st_mode & 0o777
                check(f"SQLite permissions {oct(mode)}", mode in (0o600, 0o640), "run: chmod 600 " + str(settings.db_path))
            except Exception as exc:
                check("SQLite permissions", False, str(exc))
    except Exception as exc:
        check(f"SQLite {settings.db_path}", False, str(exc))

    # docker
    has_docker = shutil.which("docker") is not None
    check(f"Docker (sandbox backend={settings.sandbox_backend})", has_docker or settings.sandbox_backend == "subprocess",
          "install Docker or set SANDBOX_BACKEND=subprocess" if not has_docker and settings.sandbox_backend == "docker" else "")

    # tools
    try:
        from .tools import default_registry
        reg = default_registry()
        check(f"tools ({len(reg)} registered)", len(reg) > 0)
    except Exception as exc:
        check("tools", False, str(exc))

    # skills
    try:
        from .skills.manager import SkillManager
        mgr = SkillManager(settings.skills_dir)
        from .tools import default_registry
        reg = default_registry()
        report = mgr.validate_all(registry_names=set(reg.names()))
        errs = [f"{k}: {v}" for k, v in report.items() if any(x.startswith("ERROR") for x in v)]
        check(f"skills ({len(mgr)} loaded)", not errs, "; ".join(errs) if errs else f"{len(mgr)} OK")
        warns = sum(1 for v in report.values() for x in v if x.startswith("references unknown"))
        if warns:
            print(f"  ↳ {warns} skill(s) reference unknown tools – see: nimna skills validate")
    except Exception as exc:
        check("skills", False, str(exc))

    # model schemas
    try:
        from .tools import default_registry
        reg = default_registry()
        for tool in reg.all():
            tool.spec()
        check("tool JSON schemas (Gemini/OpenAI compatible)", True)
    except Exception as exc:
        check("tool JSON schemas", False, str(exc))

    print()
    print("Doctor: " + ("✅ all checks passed" if ok else "❌ some checks failed – see above"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nimna", description="Reusable-skills agent")
    parser.add_argument("--env-file", default=".env", help="path to .env (default: .env)")
    parser.add_argument("--provider", choices=["gemini", "openai", "mock"], help="override MODEL_PROVIDER")
    parser.add_argument("--model", help="override model name for the selected provider")
    parser.add_argument("--no-verify", action="store_true", help="disable the verification pass")
    parser.add_argument("-v", "--verbose", action="store_true", help="print full run details as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="one-shot request")
    p_ask.add_argument("message", help="the user request (Arabic or English)")
    p_ask.add_argument("--session", help="reuse a session id for short-term memory")
    p_ask.set_defaults(func=cmd_ask)

    p_chat = sub.add_parser("chat", help="interactive REPL")
    p_chat.add_argument("--session")
    p_chat.set_defaults(func=cmd_chat)

    # `nimna skills` with optional subcommand defaults to list for ergonomics
    p_skills = sub.add_parser("skills", help="inspect installed skills", aliases=["skill"])
    p_skills.add_argument("skills_cmd", nargs="?", choices=["list", "show", "validate"], default="list",
                          help="sub-command (default: list)")
    p_skills.add_argument("name", nargs="?", help="skill name for 'show'")
    p_skills.set_defaults(func=cmd_skills)

    p_tools = sub.add_parser("tools", help="inspect tools", aliases=["tool"])
    p_tools.add_argument("tools_cmd", nargs="?", choices=["list"], default="list",
                         help="sub-command (default: list)")
    p_tools.set_defaults(func=cmd_tools)

    p_serve = sub.add_parser("serve", help="run the HTTP API + web UI")
    p_serve.add_argument("--host", help="override HOST")
    p_serve.add_argument("--port", type=int, help="override PORT")
    p_serve.set_defaults(func=cmd_serve)

    p_appr = sub.add_parser("approvals", help="handle pending approvals", aliases=["approval"])
    p_appr.add_argument("approvals_cmd", nargs="?", choices=["list", "resolve"], default="list",
                        help="sub-command (default: list)")
    p_appr.add_argument("run_id", nargs="?", help="run id for 'resolve'")
    p_appr.add_argument("--deny", action="store_true", help="deny instead of approve")
    p_appr.add_argument("--always", action="store_true", help="approve and auto-approve this tool for the run")
    p_appr.set_defaults(func=cmd_approvals)

    p_resume = sub.add_parser("resume", help="shorthand for 'approvals resolve'")
    p_resume.add_argument("run_id", help="run id to resume")
    p_resume.add_argument("--deny", action="store_true")
    p_resume.add_argument("--always", action="store_true")
    p_resume.set_defaults(func=cmd_resume)

    p_doctor = sub.add_parser("doctor", help="diagnose environment, keys, docker, workspace, db, skills")
    p_doctor.add_argument("--offline", action="store_true", help="skip live provider pings")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # validate that skills subcommand 'show' got a name
    if getattr(args, "command", None) == "skills" and getattr(args, "skills_cmd", None) == "show" and not getattr(args, "name", None):
        parser.error("skills show requires a skill name")
    if getattr(args, "command", None) == "approvals" and getattr(args, "approvals_cmd", None) == "resolve" and not getattr(args, "run_id", None):
        parser.error("approvals resolve requires a run_id")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
