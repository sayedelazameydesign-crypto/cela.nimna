# Contributing to Nimna

Thank you for improving Nimna – a reusable SKILL.md agent!

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,charts]"
cp .env.example .env  # add GEMINI_API_KEY or OPENAI_API_KEY / NVIDIA_API_KEY
pytest                # 42+ offline tests, no key required
nimna doctor          # diagnose env / keys / docker / workspace / skills
nimna ask "حلّل sales.csv وأنشئ تقريرًا"
nimna serve           # http://localhost:8000
```

`MODEL_PROVIDER=mock nimna ask "..."` exercises the full loop with no network.

## Project layout

```
nimna/config.py            Settings.from_env, limits, ensure_dirs
nimna/providers/           ModelProvider abstraction (gemini / openai_compat / mock)
nimna/skills/              SKILL.md loader + SkillManager (+ keyword ranking)
nimna/tools/               typed ToolRegistry + jail + sandbox
nimna/memory/store.py      SQLite: sessions / memories / audit / pending_runs
nimna/core/agent.py        driver + loop guards + approvals + verification
nimna/api/                 FastAPI + RTL web UI
skills/*/SKILL.md          bundled skills (front matter + instructions)
tests/                     offline pytest suite
```

## Adding a skill

Create `skills/my_skill/SKILL.md`:

```yaml
---
name: my_skill
version: 1.0.0
description: ما الذي تفعله المهارة ومتى تُستخدم
triggers:
  - كلمة مفتاحية عربية
  - english keyword
allowed_tools:
  - read_file
  - write_report
risk_level: safe   # safe | confirm | restricted
tags: [docs]
---
## متى تُستخدم
...

## التعليمات
1. ...
```

 قواعد الفحص:

- `name` matches `^[a-z0-9][a-z0-9_-]{0,63}$`, `version` is semver-like, `description` required.
- `allowed_tools` must be registered (see `nimna skills validate`); unknown entries are warned and ignored at runtime.
- Keep the body under ~15 k characters; put long examples into `references/`.
- Run `nimna skills validate` and `pytest` before opening a PR.

## Adding a tool

1. Add a Pydantic `Params` model with `Field(ge=..., le=..., description=...)`.
2. Register with `@registry.tool("name", "description", Params, risk="safe"|"confirm", risk_fn=..., tags=[...])` under `nimna/tools/builtin/`.
3. Go through `ctx.resolve_path` for any filesystem path; respect `ctx.settings.max_file_bytes` / `max_write_bytes`.
4. Wire it in `nimna/tools/builtin/__init__.py: register_all`.
5. Add a test under `tests/` – use the `ctx` + `registry.execute` helpers.

## Security guidelines

- Never log or store API keys – use `redact_payload` and keep `.env` git-ignored.
- File tools must jail via `ToolContext.resolve_path` (no absolutes, no `..`, no symlink escape).
- Network tools must call `_assert_public_url` before and after redirects.
- `run_python` must go through `nimna/tools/sandbox.py`; Docker is the only considered isolation.

## Testing

```bash
pytest -q                 # all offline
pytest -k test_symlink    # single
MODEL_PROVIDER=mock pytest -v
nimna doctor --offline
```

The mock provider (`MockProvider`) lets you script exact model turns:
`provider.queue('{"skills":["csv_analysis"]}', ModelResponse(...), "final")`.

`pyproject.toml` already sets `addopts = "-q"`, so passing `-q` again yields `-qq`
and **silently drops the final `N passed` summary line** — use `pytest` (no `-q`) or
`pytest -vv` when you need names or counts.

### Assertions on output structure: prove them by mutation

When a test asserts *where* something appears (a warning banner before a table,
a header before a body, an order of rows), assert the **structure**, not a
specific string (see `_assert_banner_precedes_every_table` in
`tests/test_evaluate_arena.py`), and prove the test bites by breaking the code on
purpose once — move the block, run the test, watch it fail with a clear message,
restore. Mutation cycles have a trap: CPython validates `.pyc` by
(mtime at 1-second granularity, size), so a same-size edit restored within the
same second is executed from **stale bytecode** while `git diff` shows the correct
source. Recipe:

```bash
find . -name __pycache__ -type d -prune -exec rm -rf {} +   # drop existing caches first
python -B -m pytest tests/test_x.py     # -B: write no .pyc during the mutation run
# ...restore the file, then run once more with a clean cache
```

`-B` only stops *writing* bytecode; it does not stop *reading* an already-stale
file — hence the cache wipe. An isolated `git worktree add` avoids the issue
entirely at a higher cost.

### Scripted edits: print → assert → modify

Any script that pipes a computed value into an in-place edit (`sed -i`, codemods, count updates in `README.md`) must print the value and assert it (present, numeric, in range) *before* modifying. Never trust an empty variable: a missing `bc` once wiped every test count from `README.md` because `sed` ran with an empty `$TOTAL`. Prefer Python with `assert` over bare shell pipelines for documented files.

The same rule binds interactive commands, not just scripts: any state-changing command (git or otherwise) is preceded by a command that *shows* the state, and its output is *read* before executing. A `reset --soft` + chained `commit` once pushed a mass deletion because the stale index was never read. Concretely for git recovery: prefer `reset --mixed <parent>` (zeroes the index, no inherited surprises), `git add` explicit files (never bare `-A` after a restore), `git diff --cached <parent>` (read it), then commit.

## Pull requests

- Keep commits focused; `git push origin arena/<id>` on your arena branch.
- Assume `.git` evaporates between sessions: the worktree persists, unpushed objects may not. Every commit is pushed immediately — no dangling local commits across turns. Run `scripts/session_start.sh` first each session (report-only; it never recovers automatically).
- `gh pr edit` is broken in this environment (it queries the retired GraphQL `projectCards` field). Use REST instead: `gh api -X PATCH repos/{owner}/{repo}/pulls/{n} -F 'body=@file.md'`.
- Describe the skill/tool change, risk level, and include a `tests/` case.
- For user-visible changes, update `README.md`, `.env.example`, and `SECURITY.md` if needed.

## Code style

- Python 3.10+, type hints, `from __future__ import annotations` where useful.
- `pydantic` for validation, `httpx` for HTTP, no heavy ORMs.
- Favor small, well-named functions over comments.

## License

By contributing you agree your changes are licensed under the MIT License (see LICENSE).
