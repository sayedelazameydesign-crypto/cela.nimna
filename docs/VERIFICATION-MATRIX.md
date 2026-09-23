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
| G14 MCP contract | `pytest -q tests/test_mcp_gateway.py -k "build_request or parse_response or discover"` | offline | PASS | envelope/`_meta`/error-allocation assertions |
| G15 MCP header agreement | `pytest -q tests/test_mcp_gateway.py -k "header"` | offline | PASS | header/body mismatch + injection tests |
| G16 MCP transport binding | `pytest -q tests/test_mcp_gateway.py -k "sse or 202 or legacy"` | `httpx.MockTransport` | PASS / MOCKED | no session header, no initialize, no resumability |
| G17 MCP governance | `pytest -q tests/test_mcp_gateway.py -k "denied or approval or scope or audited"` | offline | PASS | policy denial + audit chain assertions |
| G18 MCP third-party live | explicit opt-in smoke against a real third-party MCP server | external account | BLOCKED | not run; enabling requires `MCP_ENABLED=true` + a server + credential |
| G19 MCP integration | `pytest -q tests/test_mcp_integration.py` | loopback socket, no internet | PASS | real HTTP server validates mirrored headers; `-32020` on mismatch |
| G20 MCP agent loop | `pytest -q tests/test_mcp_integration.py -k "agent"` | offline | PASS | scope → policy → approval → forward, plus denial and audit |

## Interpretation

- `PASS` = the named check executed and passed in the named environment.
- `MOCKED` = contract only; no external service or real browser was reached.
- `BLOCKED` = policy intentionally prevented execution (for example
  `MAX_SPEND_USD=0` or no API key).
- `UNKNOWN` is never silently promoted to `PASS`.

The Browser Use live gate must not run on ordinary pull requests: it can consume
credits and mutate external state. Run it manually with a dedicated project,
small bounded task, and a disposable workspace.
