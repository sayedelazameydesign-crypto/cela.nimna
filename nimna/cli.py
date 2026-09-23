"""Command line interface.

    nimna chat                      interactive REPL (approvals asked inline)
    nimna ask "..."                 one-shot request
    nimna skills list|show|validate
    nimna tools list
    nimna serve [--port 8000]       FastAPI server + web UI
    nimna approvals list|resolve    handle approvals created through the API
"""
import argparse
import json
import sys
import uuid
from typing import Optional

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
    print(result.reply or result.error or "(no reply)")
    meta = []
    if result.skills_used:
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
    result = agent.run(args.message, session_id=args.session)
    _print_result(result, verbose=args.verbose)
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

    settings = _settings(args)
    manager = SkillManager(settings.skills_dir)
    if args.skills_cmd == "list":
        for meta in manager.list():
            print(f"{meta.name:20s} v{meta.version:8s} {meta.description}")
            if meta.allowed_tools:
                print(f"{'':20s} tools: {', '.join(meta.allowed_tools)}")
        for name, error in manager.errors.items():
            print(f"{name:20s} ERROR: {error}", file=sys.stderr)
        return 0
    if args.skills_cmd == "show":
        try:
            skill = manager.get(args.name)
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(json.dumps(skill.meta.model_dump(), ensure_ascii=False, indent=2))
        print("\n" + skill.instructions)
        return 0
    if args.skills_cmd == "validate":
        report = manager.validate_all()
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

    settings = _settings(args)
    app = create_app(settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port,
                log_level=settings.log_level.lower())
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    from .bootstrap import build_agent

    agent = build_agent(_settings(args))
    if args.approvals_cmd == "list":
        for item in agent.pending_approvals():
            state = agent.memory.get_pending(item["id"]) or {}
            pending = state.get("pending") or {}
            print(f"{item['id']}  session={item['session_id']}  tool={pending.get('tool_name')}  "
                  f"{pending.get('summary', '')}")
        return 0
    if args.approvals_cmd == "resolve":
        try:
            result = agent.resume(args.run_id, approved=not args.deny, always=args.always)
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 1
        _print_result(result, verbose=args.verbose)
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nimna", description="Reusable-skills agent")
    parser.add_argument("--env-file", default=".env", help="path to .env (default: .env)")
    parser.add_argument("--provider", choices=["gemini", "openai", "mock"], help="override MODEL_PROVIDER")
    parser.add_argument("--model", help="override model name for the selected provider")
    parser.add_argument("--no-verify", action="store_true", help="disable the verification pass")
    parser.add_argument("-v", "--verbose", action="store_true", help="print full run details as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="one-shot request")
    p_ask.add_argument("message")
    p_ask.add_argument("--session", help="reuse a session id for short-term memory")
    p_ask.set_defaults(func=cmd_ask)

    p_chat = sub.add_parser("chat", help="interactive REPL")
    p_chat.add_argument("--session")
    p_chat.set_defaults(func=cmd_chat)

    p_skills = sub.add_parser("skills", help="inspect installed skills")
    skills_sub = p_skills.add_subparsers(dest="skills_cmd", required=True)
    skills_sub.add_parser("list")
    p_show = skills_sub.add_parser("show")
    p_show.add_argument("name")
    skills_sub.add_parser("validate")
    p_skills.set_defaults(func=cmd_skills)

    p_tools = sub.add_parser("tools", help="inspect tools")
    tools_sub = p_tools.add_subparsers(dest="tools_cmd", required=True)
    tools_sub.add_parser("list")
    p_tools.set_defaults(func=cmd_tools)

    p_serve = sub.add_parser("serve", help="run the HTTP API + web UI")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_appr = sub.add_parser("approvals", help="handle pending approvals")
    appr_sub = p_appr.add_subparsers(dest="approvals_cmd", required=True)
    appr_sub.add_parser("list")
    p_res = appr_sub.add_parser("resolve")
    p_res.add_argument("run_id")
    p_res.add_argument("--deny", action="store_true")
    p_res.add_argument("--always", action="store_true")
    p_appr.set_defaults(func=cmd_approvals)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
