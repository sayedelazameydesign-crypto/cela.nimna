# Capability Matrix

> الحالة ليست marketing. `implemented` تعني أن الكود موجود وله اختبار/دليل؛
> `configured` تعني أن الإعداد حاضر؛ `partial` لا يساوي production-ready.

| Capability | Status | Implementation | Evidence / test | Owner boundary |
|---|---|---|---|---|
| Mission runtime: bounded agent loop | implemented | `nimna/core/agent.py` | `tests/test_agent.py`, loop-limit tests | `core` |
| Progressive skills / scoped tools | implemented | `nimna/skills/`, `ToolRegistry` | `tests/test_skills.py`, `tests/test_hardening.py` | `skills/tools` |
| Typed tool arguments | implemented | `nimna/tools/base.py` | `tests/test_tools.py::test_validation_errors_are_returned_to_model` | `tools` |
| Approval pause/resume / TTL / session scope | implemented | `nimna/core/approval.py`, `memory/store.py` | approval and restart tests | `governance` |
| Rich risk vocabulary | implemented | `nimna/governance/policy.py` | policy unit tests / CI | `governance` |
| SQLite canonical memory | implemented | `nimna/memory/store.py` | memory and restart tests | `memory` |
| Optional vector retrieval | partial | `nimna/memory/qdrant.py` | vector health/fallback tests | `memory` |
| Model provider boundary | implemented | `nimna/providers/` | provider wire/parsing tests | `providers` |
| Capability-aware Model Registry | implemented | `nimna/models/registry.py` | registry/cost tests | `models` |
| Hard zero-spend model gate | implemented | `CostGuard`, `GovernedModelProvider` | unknown-price rejection test | `governance/models` |
| Audit events | implemented | `MemoryStore.audit_log` | audit assertions | `observability` |
| SHA-256 evidence chain | implemented | `nimna/provenance/hashchain.py` | evidence verification test | `evidence` |
| Runtime/repository fingerprint | implemented | `nimna/provenance/manifest.py` | `/api/provenance` | `provenance` |
| Failure/loop/runtime bounds | implemented | `core/agent.py`, settings | hardening tests | `runtime` |
| Host filesystem jail | implemented | `ToolContext.resolve_path` | symlink/traversal tests | `security` |
| Python subprocess sandbox | partial | `nimna/tools/sandbox.py` | subprocess tests; not a security boundary | `sandbox` |
| Docker sandbox | opt-in | compose profile / sandbox backend | deployment config; requires Docker | `sandbox` |
| Browser Use Cloud V4 client | available_opt_in | `nimna/browser/cloud_v4.py` | REST contract tests; no live key in CI | `browser` |
| Browser Use stop-in-finally | implemented | `managed_browser` | cleanup contract test | `browser/governance` |
| Browser Use governed tool | available_opt_in | `skills/browser_use/`, builtin tool | disabled by default; live test requires account | `tools/browser` |
| Local Computer Control | partial | `nimna/tools/builtin/computer.py` | simulated/offline tests | `computer` |
| Multi-agent swarm | partial | `nimna/core/planner_swarm.py`, `nimna/agents/` | offline path; not production isolation | `orchestrator` |
| MCP gateway | planned | — | no claim until contract exists | `mcp` |
| External connectors | planned | — | no claim until least-privilege adapter exists | `connectors` |
| Postgres multi-replica source of truth | planned | — | SQLite is current canonical store | `memory/infra` |
| OpenTelemetry / Prometheus traces | partial | audit + metrics summary | no exporter claim | `observability` |
| Signed release / SBOM / SLSA | planned | — | release workflow must add artifacts | `release` |
| Arabic evaluation suite | planned | — | capability row reserved, no PASS claim | `evaluation` |
| PR diff evaluation gate (Arena) | implemented | `scripts/evaluate_arena.py`, `.github/workflows/arena_diff_eval.yml` | `tests/test_evaluate_arena.py`; remote Arena API is opt-in (`SKIPPED` without key); LLM-as-a-Judge is opt-in via `OPENAI_API_KEY` on any OpenAI-compatible endpoint (`SKIPPED` without key, `ERROR` banner on failure, verdict advisory) | `evaluation/ci` |
| Arena task suite runner (P0.5) | implemented | `evals/tasks/` (10 tasks), `scripts/run_arena_suite.py` | `tests/test_arena_suite.py`; mock mode only — verdicts are `MOCKED` with a banner, live scoring + LLM judge arrive in P5 | `evaluation` |
| Shell execution primitive (P1-T1) | implemented | `nimna/tools/builtin/shell.py` (`run_command`), gated by `SHELL_TOOL_ENABLED=false` default | `tests/test_shell_tool.py` (22 adversarial + agent-flow tests; mutation-checked); 7 statuses, evidence in the hash chain, default-deny beyond `shell.execute` | `execution/governance` |
| Filesystem Observation & Delta (P1-T2) | implemented | `nimna/execution/observation.py` (standalone primitive: `WorkspaceObserver`, `ObservationScope`), wired into `shell.py` | `tests/test_observation.py` (15) + atomicity tests in `test_shell_tool.py` (timeout/failure keep side effects); content-hash based (no mtime), bounded, symlink-safe, re-verifiable (`verify_delta`); 4 mutation checks | `execution` |
| Deterministic Verification (P1-T3) | implemented | `nimna/execution/verification.py` (`DeterministicVerifier`, 8 check kinds), wired into the Arena suite (`verify:` task key) | `tests/test_verification.py` (18) + suite integration tests; PASS/FAIL/INCONCLUSIVE only — no LLM, empty/missing-evidence specs are INCONCLUSIVE never PASS; 3 mutation checks | `execution` |
| Checkpoint / Recovery (P1-T4) | implemented | `nimna/execution/recovery.py` (`Checkpoint`, `CheckpointStore`, `RecoveryManager`), wired into the Arena suite (one atomic checkpoint per shell run) | `tests/test_recovery.py` (17: restore/crash/duplicate/corrupt/stale/kill-switch/auth-expiry/evidence-mismatch/fingerprint) + suite integration tests; atomic persistence (tmp+fsync+os.replace+checksum), 7-state machine with legal-transition enforcement, 9 ordered resume gates, 6 mutation checks | `execution` |
| Tool Registry (P1-T5) | implemented | `nimna/execution/tool_registry.py` (`ToolDescriptor`, `ToolRegistry`, `invoke`, `EvidenceChain`), wired into the Arena suite (verifier `command` re-runs are gated invocations of `sandbox.command`) | `tests/test_tool_registry.py` (22: duplicate/invalid/NOT_FOUND/disabled/revoked/capability/policy/auth/schema-before-execution/output-violation/exception-honesty/deterministic-order/chained-evidence) + suite integration tests; separation enforced — resolver/policy/authorizer injected, default deny-all, REVOKED terminal; 8 mutation checks | `execution` |
| Capability + Policy (P1-T6) | implemented | `nimna/execution/policy.py` (`Policy`, `PolicyRule`, `CapabilityCatalog`, `Authorizer`, `adjudicate`), wired into the Arena suite (`arena-suite-policy@1.0.0` governs `sandbox.command`) | `tests/test_policy.py` (24: default-deny, exact/missing/extra/unknown capability, deny/allow/confirm, absent/expired/wrong-actor/wrong-tool/version-mismatch authorization, resource inside/outside + symlink escape through the T2 boundary, malformed policy/request/grant, revoked capability, amplification, determinism) ; resolver/policy/authorizer injected — the registry stays policy-free; no implicit capability inheritance | `execution/governance` |
| Agent Execution Gateway (P1-T7) | implemented | `nimna/execution/gateway.py` (`ExecutionGateway`, `InvocationContext`, `GatewayOutcome`), opt-in binding on `Agent` (`execution_gateway=...`, default None = legacy path) | `tests/test_gateway.py` (10: full-fabric scenario, four refusals stop before the handler, handler-error/verify-fail honesty, structural no-bypass with redacted discovery, real agent binding with a sabotaged legacy handler, legacy path unchanged); unified 13-field evidence record; M1–M10 mutation-checked | `execution` |
| Precise file edit over Fabric (T8): `edit_file` + `apply_patch` | implemented | `nimna/tools/builtin/edit.py` (one core, two doors: legacy pydantic tools + `register_fabric` descriptors; explicit failure on missing/ambiguous old_text; unified-diff subset, all-or-nothing hunk verification; 1MB hard cap; workspace jail; no disable flags) | `tests/test_edit_tools.py` (17: legacy semantics, jail/oversize/binary/devnull/rename/no-newline refusals, bound-agent routing through policy→authorization→evidence with sabotaged legacy door, POLICY_DENIED refusal, strict input-schema fail-closed) + surface via `skills/code_execution/SKILL.md` | `tools/fabric` |

## Status vocabulary

`implemented` · `configured` · `available_opt_in` · `partial` · `planned` ·
`blocked` · `unknown` · `MOCKED` · `SKIPPED`.

When an external Browser Use account is not available, the correct result is
`AVAILABLE_OPT_IN` or `BLOCKED`, never `PASS production`.
