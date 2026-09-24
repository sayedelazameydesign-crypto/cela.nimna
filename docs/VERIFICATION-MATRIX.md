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
| G18 Deterministic Verification | `pytest -q tests/test_verification.py` | offline (temp workspaces) | PASS/FAIL/INCONCLUSIVE (no LLM) · 8 check kinds incl. delta observation + sha re-verification + bounded command re-run · empty spec / missing evidence / denied command ⇒ INCONCLUSIVE (never PASS) · path escapes are FAILs · Arena tasks carry `verify:` blocks (code-01 6/6, code-05 5/5, verifier PASS) · PASS-fabrication, INCONCLUSIVE→PASS and verdict-ignored mutations are test-breaking | `nimna/execution/verification.py`, `tests/test_verification.py`, `tests/test_arena_suite.py`, `evals/tasks/` |

| G19 Checkpoint / Recovery | `pytest -q tests/test_recovery.py` | offline (temp stores) | atomic persistence (validate → tmp+fsync → os.replace → dir fsync; torn/tampered file ⇒ CorruptedCheckpoint never used) · 7-state machine, illegal transitions raise · 9 ordered resume gates (missing ⇒ refused 'never claimed', kill-switch ⇒ ABORTED, revoked/expired authorization ⇒ refused, evidence mismatch ⇒ refused, fingerprint change ⇒ REQUIRES_REOBSERVATION then confirm, idempotency ledger skips completed+verified) · duplicate resume refused · corrupted ⇒ RECOVERY_ERROR (never PASS) · Arena rows carry ckpt STATE + evidence head + fingerprint · all 6 mutations are test-breaking | `nimna/execution/recovery.py`, `tests/test_recovery.py`, `tests/test_arena_suite.py`, `scripts/run_arena_suite.py` |

| G20 Tool Registry | `pytest -q tests/test_tool_registry.py` | offline (in-memory) | register/replace (strict semver bump, REVOKED un-replaceable) / unregister / duplicate protection · deterministic sorted discovery + capability query · lifecycle table with REVOKED terminal · invocation gates in order: NOT_FOUND → DISABLED/REVOKED → input-schema (before execution) → capability → policy (default-deny) → authorization (default-deny) → execute → output-schema (violation = ok=False, executed=True) · handler exception = HANDLER_ERROR never PASS · hash-chained evidence, digests only, tamper-detecting · Arena verifier `command` re-runs are gated invocations (refusal ⇒ INCONCLUSIVE never PASS) · all 8 mutations are test-breaking | `nimna/execution/tool_registry.py`, `tests/test_tool_registry.py`, `tests/test_arena_suite.py`, `scripts/run_arena_suite.py` |

| G21 Capability + Policy | `pytest -q tests/test_policy.py` | offline (temp workspaces) | deterministic first-match policy (ALLOW/DENY/REQUIRE_CONFIRMATION with policy_id/version/matched_rule) · default-DENY unbreakable: unknown/revoked capability, no matching rule, malformed request/policy/grant, policy error (fail-closed) · no capability amplification: exact declared set, partial grant match authorizes nothing (M10-killing test) · resource scoping via the T2 boundary (resolve → containment; traversal/symlink escapes DENY) · authorization ≠ policy: absent/expired/wrong-actor/wrong-tool/version-mismatch ⇒ final DENY · REQUIRE_CONFIRMATION needs an explicit consent grant · same inputs ⇒ same decision · all 10 mutations are test-breaking | `nimna/execution/policy.py`, `tests/test_policy.py`, `tests/test_tool_registry.py`, `tests/test_arena_suite.py`, `scripts/run_arena_suite.py` |

| G22 Agent Execution Gateway | `pytest -q tests/test_gateway.py` | offline (temp workspaces) | single path: `ExecutionGateway.invoke` only — registry held private, discovery redacted (no handler leaves) · Policy=DENY ⇒ handler_called=False (missing-capability/policy/expired-auth/outside-workspace all stop before the handler; traversal caught at the context gate) · full fabric per invocation: Observe (T2 delta) → Verify (T3 verdict gates `ok`) → Checkpoint (T4: PASS ⇒ COMPLETED, FAIL ⇒ FAILED, INCONCLUSIVE ⇒ diagnosable) · unified 13-field evidence record, digests only, chain verifies · agent binding opt-in (default legacy path unchanged) proven with a sabotaged legacy handler · all 10 mutations are test-breaking | `nimna/execution/gateway.py`, `nimna/core/agent.py`, `tests/test_gateway.py`, `tests/test_arena_suite.py`, `scripts/run_arena_suite.py` |

| G23 Render blueprint (deploy contract) | `python scripts/check_render_blueprint.py` | offline (no Render account, no Docker build) | PASS · SKIP إذا لم يوجد `render.yaml` في الفرع — يفشل على: قيمة YAML غير مقتبسة (`off`/`true`/عدد)، `autoDeploy` المهجور، اسم متغير لا يقرؤه `nimna/`/`security/`، سرّ مضمّن في النص، `healthCheckPath` ليس مساراً معلناً، قرص دائم/maintenance على `plan: free` | `render.yaml`, `tests/test_render_blueprint.py` (13 اختباراً: الملف المصدَّق يمر وكل طفرة تُرفض), workflow `00-integrity.yml` — **PASS على `main` بعد دمج PR #16**: job `integrity` step 7 في run `35981257006` (`0310c63`) |

## Interpretation

- `PASS` = the named check executed and passed in the named environment.
- `MOCKED` = contract only; no external service or real browser was reached.
- `BLOCKED` = policy intentionally prevented execution (for example
  `MAX_SPEND_USD=0` or no API key).
- `UNKNOWN` is never silently promoted to `PASS`.

The Browser Use live gate must not run on ordinary pull requests: it can consume
credits and mutate external state. Run it manually with a dedicated project,
small bounded task, and a disposable workspace.
