# Security policy

## Reporting a vulnerability
Please email the maintainers or open a private security advisory on GitHub.
Do **not** file a public issue for sensitive reports. We aim to acknowledge
within 72 hours and to ship a fix within 14 days.

## Keys and secrets
- Never commit `.env`.  It is git-ignored. Copy `.env.example` instead.
- Keys are read only from environment variables (`GEMINI_API_KEY`,
  `OPENAI_API_KEY` / `NVIDIA_API_KEY`). They are never written to logs,
  audit payloads, or the model history – `nimna/tools/base.py: redact_payload`
  and `nimna/memory/store.py` scrub `api_key`, `secret`, `password`, `token`
  before persistence.
- `nimna doctor --offline` verifies the setup without pinging the provider.

## Filesystem isolation
- Every tool that touches a path goes through `ToolContext.resolve_path`
  (see `nimna/tools/base.py`). Absolute paths are rejected; `..` traversals,
  symlink escapes, and null bytes are blocked – the resolved target must stay
  inside `WORKSPACE_DIR`.
- `list_files` skips dot-files and drops entries whose symlink target would
  escape the workspace.
- `read_file` refuses files larger than `AGENT_MAX_FILE_BYTES` (default 5 MB);
  `write_file` / `write_report` enforce `AGENT_MAX_WRITE_BYTES` (2 MB).
- The workspace and SQLite directory are created with restrictive defaults;
  run `nimna doctor` to check writability.

## Network isolation
- `fetch_url` / `web_search` call `_assert_public_url` (see
  `nimna/tools/builtin/web.py`): only `http`/`https`, literal private IPs and
  hostnames that resolve to private / loopback / link-local / reserved /
  multicast / unspecified addresses are rejected. Local suffixes (`.local`,
  `.internal`, `.localhost`) and `localhost` / `0.0.0.0` / `::1` are blocked.
  Redirects are followed hop-by-hop with the same checks (max 5 hops).
- The search tool never contacts the user's private network directly; it only
  queries `html.duckduckgo.com`.

## Code execution
- `SANDBOX_BACKEND=docker` (the default, see `.env.example`) runs
  `run_python` in a `python:3.11-slim` container with `--network none`,
  `--cap-drop ALL`, `--security-opt no-new-privileges`, memory/cpu/pids limits,
  and only the workspace mounted (`nimna/tools/sandbox.py`).
- `SANDBOX_BACKEND=subprocess` uses a scrubbed environment, `cwd=workspace`,
  and POSIX `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_FSIZE` / `RLIMIT_NPROC`,
  but it is **not** a security boundary – the `run_python` tool is then
  `confirm` (requires approval) and `nimna doctor` warns.
- No tool ever executes code without going through the sandbox module.

## Approvals
- Risk `confirm` tools (`delete_file`, overwriting `write_file`/`write_report`,
  `run_python` in subprocess mode, etc.) suspend the run and require an
  explicit decision. The CLI asks `[y/n/a]`; the API returns
  `status=awaiting_approval` with `{approval_id, tool_name, arguments, description}`
  and `POST /api/approvals/{id}` resolves it. Overwrites render as
  `هذه العملية ستكتب فوق: workspace/...` in both CLI and web UI.
- Denied calls feed `{"error":"denied"}` back to the model; the agent is
  instructed never to retry a denied tool.

## Limits that stop runaway loops
`nimna/config.py` (all env-overridable):

```
AGENT_MAX_STEPS=12
AGENT_MAX_TOOL_CALLS=30
AGENT_MAX_RUNTIME_SECONDS=300
AGENT_MAX_RESPONSE_TOKENS=4096
AGENT_MAX_CONSECUTIVE_FAILURES=5
```

The agent also stops on: repeated identical tool calls (≥3), repeated
identical model text (≥3), unknown/forbidden tool, or validation failures.

## Skill loading
- Adding a folder under `skills/` does not auto-execute. The planner only sees
  the catalog (`name`/`description`/`triggers`). The body is loaded via
  `load_skill` (or at request start) and widened tools stay scoped to that run.
- `nimna skills validate` (and `SKILL.md`'s `risk_level`) flag skills that
  reference unknown tools or are marked `restricted`.

## Updates
Use `docker compose pull && docker compose up -d` or `pip install -U .`.
Pin `SANDBOX_IMAGE` if you need reproducibility.
