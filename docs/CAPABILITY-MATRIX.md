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
| MCP gateway | implemented | `nimna/mcp/` (contract, headers, auth, transport, gateway, registry, naming) + `skills/mcp_servers/` | wired into the agent loop; tools are `confirm` and audited; proven against a real server over a socket; Unicode tool names/globs supported and evaluated in Arabic (`docs/arabic-evaluation.md`). Disabled by default (`MCP_ENABLED=false`); no third-party live server (G18) | `mcp` |
| External connectors | planned | — | no claim until least-privilege adapter exists | `connectors` |
| Postgres multi-replica source of truth | planned | — | SQLite is current canonical store | `memory/infra` |
| OpenTelemetry / Prometheus traces | partial | audit + metrics summary | no exporter claim | `observability` |
| Signed release / SBOM / SLSA | planned | — | release workflow must add artifacts | `release` |
| Arabic evaluation suite | planned | — | capability row reserved, no PASS claim | `evaluation` |

## Status vocabulary

`implemented` · `configured` · `available_opt_in` · `partial` · `planned` ·
`blocked` · `unknown` · `MOCKED` · `SKIPPED`.

When an external Browser Use account is not available, the correct result is
`AVAILABLE_OPT_IN` or `BLOCKED`, never `PASS production`.
