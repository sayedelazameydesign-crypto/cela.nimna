# Verification Matrix

هذا الملف يفرق بين وجود الكود وبين قدرة مثبتة. الزمن والـcommit يضافان آلياً
في CI/evidence عند تشغيل الاختبار؛ لا نضع timestamp وهمياً في المستودع.

| Gate | Command / check | Environment | Expected status | Evidence |
|---|---|---|---|---|
| G0 Repository integrity | `python scripts/verify_capabilities.py` | offline | PASS | workflow `00-integrity.yml` |
| G1 Python compile | `python -m compileall -q nimna security` | offline | PASS | workflow `00-integrity.yml` |
| G2 Unit/integration | `pytest -q` | no provider key | PASS / MOCKED where applicable | `.github/workflows/ci.yml` |
| G3 Skills contract | `pytest -q tests/test_skills.py tests/test_tools.py` | offline | PASS | test report |
| G4 Security path/SSRF | `pytest -q tests/test_hardening.py tests/test_security_review2.py` | offline + isolated | PASS | test report |
| G5 Provider contract | `pytest -q tests/test_providers.py` | mocked HTTP | PASS / MOCKED | test report |
| G6 Cost gate | `pytest -q tests/test_agent_os_additions.py -k zero_budget` | offline | PASS | `CostGuard` test |
| G7 Evidence integrity | `pytest -q tests/test_agent_os_additions.py -k evidence` | SQLite `:memory:` | PASS | `/api/runs/{run_id}/evidence` |
| G8 API contract | `tests/test_api.py` | FastAPI TestClient | PASS | `/api/health`, `/api/models` |
| G9 Browser V4 auth | `pytest -q tests/test_agent_os_additions.py -k browser_v4_auth` | `httpx.MockTransport` | PASS / MOCKED | header + endpoint assertions |
| G10 Browser lifecycle | `pytest -q tests/test_agent_os_additions.py -k owned_browser` | `httpx.MockTransport` | PASS / MOCKED | PATCH stop assertion |
| G11 Browser live | explicit opt-in smoke with real key | external account | BLOCKED unless enabled | run id + provider evidence |
| G12 Production sandbox | Docker/microVM security review | deployment | PARTIAL until external sandbox | `SECURITY.md` |
| G13 Release integrity | manifest + lock + SBOM + provenance | CI/release | PLANNED | release workflow |
| G14 PR diff evaluation | `python scripts/evaluate_arena.py --diff_file changes.diff` | offline (static) / remote Arena opt-in / LLM judge opt-in | PASS (report) · benchmark SKIPPED unless `ARENA_API_URL`+`ARENA_API_KEY` · judge SKIPPED unless `OPENAI_API_KEY` (verdict advisory; `MOCKED`/`ERROR` get a top banner) | workflow `arena_diff_eval.yml`, `tests/test_evaluate_arena.py`, PR comment + artifact |
| G15 Arena task suite (agent benchmark) | `python scripts/run_arena_suite.py --mode mock` | offline (MockProvider) / live opt-in | report + ledger rows · verdicts `MOCKED` (banner, never PASS) · `SKIPPED` for missing capabilities (network/shell) · regression flag vs ledger | `evals/tasks/` (10), `evals/ledger/BASELINE-mock.md`, `tests/test_arena_suite.py` |
| G16 Shell execution primitive | `pytest -q tests/test_shell_tool.py` | offline (subprocess sandbox) | 7 statuses contract · DENIED when `SHELL_TOOL_ENABLED=false` · POLICY_BLOCKED for network/admin/destructive/escape classes pre-execution · CONFIRMATION_REQUIRED without consent · evidence events in the SHA-256 chain (`shell_evidence`/`shell_denied`) · DENIED→SUCCESS and CONFIRMATION_REQUIRED→SUCCESS mutations are test-breaking | `nimna/tools/builtin/shell.py`, `tests/test_shell_tool.py`, `skills/shell_execution/` |
| G17 Filesystem Observation/Delta | `pytest -q tests/test_observation.py` | offline (temp workspaces) | CREATED/MODIFIED/DELETED/RENAMED/UNCHANGED by content hash · bounded scope, outside-workspace refused · symlinks recorded never followed · TIMEOUT/FAILURE keep their side effects in the delta · `verify_delta` re-reads reality (tamper → mismatch) · suite re-verifies shell-produced artifacts against the delta · MODIFIED→UNCHANGED, DELETED→UNCHANGED, outside→allowed, timeout→empty-delta mutations are test-breaking | `nimna/execution/observation.py`, `tests/test_observation.py`, `tests/test_shell_tool.py`, `scripts/run_arena_suite.py` |

## Interpretation

- `PASS` = the named check executed and passed in the named environment.
- `MOCKED` = contract only; no external service or real browser was reached.
- `BLOCKED` = policy intentionally prevented execution (for example
  `MAX_SPEND_USD=0` or no API key).
- `UNKNOWN` is never silently promoted to `PASS`.

The Browser Use live gate must not run on ordinary pull requests: it can consume
credits and mutate external state. Run it manually with a dedicated project,
small bounded task, and a disposable workspace.
