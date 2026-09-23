"""Offline repository integrity gate for the capability/verification contract."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    ROOT / "docs" / "CAPABILITY-MATRIX.md",
    ROOT / "docs" / "VERIFICATION-MATRIX.md",
    ROOT / "docs" / "browser_use_v4.md",
    ROOT / "docs" / "arabic-evaluation.md",
    ROOT / "docs" / "mcp-gateway.md",
    ROOT / "nimna" / "models" / "registry.py",
    ROOT / "nimna" / "provenance" / "hashchain.py",
    ROOT / "nimna" / "browser" / "cloud_v4.py",
    ROOT / "nimna" / "mcp" / "contract.py",
    ROOT / "nimna" / "mcp" / "headers.py",
    ROOT / "nimna" / "mcp" / "transport.py",
    ROOT / "nimna" / "mcp" / "gateway.py",
    ROOT / "nimna" / "mcp" / "naming.py",
    ROOT / "nimna" / "mcp" / "registry.py",
    ROOT / "skills" / "mcp_servers" / "SKILL.md",
    ROOT / "tests" / "test_mcp_gateway.py",
    ROOT / "tests" / "test_mcp_integration.py",
    ROOT / "tests" / "mcp_mock_server.py",
    ROOT / "tests" / "test_arabic_evaluation.py",
    ROOT / "tests" / "test_mcp_security_invariants.py",
)


def main() -> int:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        print("missing required capability files:", ", ".join(missing), file=sys.stderr)
        return 1
    try:
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8", errors="replace").split("\x00")
    except Exception:
        tracked = []
    if any(Path(path).name in {".env", ".env.local", ".env.production"} for path in tracked if path):
        print("refusing to pass: plaintext environment file is tracked", file=sys.stderr)
        return 1
    for path in (ROOT / "nimna", ROOT / "security"):
        for source in path.rglob("*.py"):
            try:
                ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            except SyntaxError as exc:
                print(f"syntax error in {source}: {exc}", file=sys.stderr)
                return 1
    browser_doc = (ROOT / "docs" / "browser_use_v4.md").read_text(encoding="utf-8")
    required_phrases = (
        "X-Browser-Use-API-Key",
        "PATCH /api/v4/browsers/{id}",
        "Retry-After",
        "GPT-6 Astra",
        "BROWSER_USE_MAX_SPEND_USD=0",
    )
    missing_phrases = [phrase for phrase in required_phrases if phrase not in browser_doc]
    if missing_phrases:
        print("browser V4 doc missing: " + ", ".join(missing_phrases), file=sys.stderr)
        return 1

    # The MCP doc must name the wire contract it documents, so the prose cannot
    # drift away from the headers the code actually sends.
    mcp_doc = (ROOT / "docs" / "mcp-gateway.md").read_text(encoding="utf-8")
    mcp_phrases = (
        "MCP-Protocol-Version",
        "Mcp-Method",
        "Mcp-Name",
        "Mcp-Param-",
        "=?base64?",
        "Mcp-Session-Id",
        "MCP_ENABLED=false",
        "server/discover",
    )
    missing_mcp = [phrase for phrase in mcp_phrases if phrase not in mcp_doc]
    if missing_mcp:
        print("MCP doc missing: " + ", ".join(missing_mcp), file=sys.stderr)
        return 1

    # Read the targeted revision out of the source instead of importing nimna,
    # so this gate stays dependency-free, and require the doc to state it.
    contract_source = (ROOT / "nimna" / "mcp" / "contract.py").read_text(encoding="utf-8")
    match = re.search(r'^PROTOCOL_VERSION\s*=\s*"([^"]+)"', contract_source, re.MULTILINE)
    if not match:
        print("cannot read PROTOCOL_VERSION from nimna/mcp/contract.py", file=sys.stderr)
        return 1
    targeted = match.group(1)
    if targeted not in mcp_doc:
        print(
            f"MCP doc does not state the targeted protocol revision {targeted}",
            file=sys.stderr,
        )
        return 1
    # The targeted revision is stateless. Naming the removed methods in
    # REMOVED_METHODS is expected and useful; declaring session machinery or an
    # initialize handshake is not.
    for pattern, label in (
        (r"^\s*HEADER_SESSION", "a session header constant"),
        (r"^def initialize\b", "an initialize handshake"),
    ):
        if re.search(pattern, contract_source, re.MULTILINE):
            print(f"MCP contract reintroduces removed machinery: {label}", file=sys.stderr)
            return 1

    # The capability row claims the gateway is wired into the agent loop. Check
    # that wiring still exists, so the claim cannot outlive the code.
    agent_source = (ROOT / "nimna" / "core" / "agent.py").read_text(encoding="utf-8")
    if "attach_gateway" not in agent_source:
        print("MCP claim unbacked: agent.py no longer attaches the gateway", file=sys.stderr)
        return 1
    if "fnmatch" not in agent_source:
        print("MCP claim unbacked: allowed_tools glob expansion is gone", file=sys.stderr)
        return 1
    registry_source = (ROOT / "nimna" / "mcp" / "registry.py").read_text(encoding="utf-8")
    if 'risk="confirm"' not in registry_source:
        print(
            "MCP safety invariant broken: remote tools must be registered as confirm",
            file=sys.stderr,
        )
        return 1
    skill = (ROOT / "skills" / "mcp_servers" / "SKILL.md").read_text(encoding="utf-8")
    if "mcp__*__*" not in skill:
        print("MCP claim unbacked: the mcp_servers skill no longer grants mcp__*__*", file=sys.stderr)
        return 1

    # The execution-point gate. Registration alone is not enough: a remote tool
    # whose risk is wrong would let the governed handler assert consent for the
    # user, so the agent must re-raise any MCP-tagged tool to `confirm` at the
    # moment it decides to run something.
    # Definition-level, not substring: a renamed or commented-out version would
    # satisfy `"MCP_TAG in tool.tags" in source` while being exactly the bug this
    # check exists to catch.
    if not re.search(r"MCP_TAG\s+in\s+tool\.tags", agent_source):
        print(
            "MCP safety invariant broken: the agent no longer forces confirm for "
            "MCP-tagged tools at the execution point",
            file=sys.stderr,
        )
        return 1

    # Consent must be evidenced, not asserted. The handler passes
    # `explicit_consent=True` to the gateway on the strength of the run's
    # approval ledger, so both halves of that handshake have to stay: the agent
    # writes the ledger and the handler refuses without it.
    if "APPROVED_KEY" not in agent_source:
        print(
            "MCP safety invariant broken: the agent no longer records which remote "
            "tools cleared its gate, so the handler cannot evidence consent",
            file=sys.stderr,
        )
        return 1
    if "APPROVED_KEY" not in registry_source or "without an approval" not in registry_source:
        print(
            "MCP safety invariant broken: the governed handler no longer refuses a "
            "remote tool that has no approval recorded for the call",
            file=sys.stderr,
        )
        return 1

    naming_source = (ROOT / "nimna" / "mcp" / "naming.py").read_text(encoding="utf-8")
    # Unicode support is a stated capability: tool names and glob patterns from
    # an Arabic-language server must survive. A regression to an ASCII-only
    # escape would silently exclude them.
    for required, label in (
        ("namespaced_tool_name", "the local-name mapping"),
        ("encode_name_pattern", "glob-pattern encoding"),
        ("encode_tool_name_element", "the name-escaping step"),
    ):
        if not re.search(rf"^def {required}\(", naming_source, re.MULTILINE):
            print(f"naming contract broken: {label} ({required}) is gone", file=sys.stderr)
            return 1
    # The budget and the hash fallback are what stop two long names collapsing
    # onto one local name, which would let one remote tool shadow another.
    for required, label in (
        ("MAX_LOCAL_TOOL_NAME", "the local name budget"),
        ("hashlib", "the truncation fingerprint"),
    ):
        if required not in naming_source:
            print(f"naming contract broken: {label} ({required}) is gone", file=sys.stderr)
            return 1
    arabic_suite = (ROOT / "tests" / "test_arabic_evaluation.py").read_text(encoding="utf-8")
    for required, label in (
        ("ARABIC_MCP_REQUEST", "an Arabic request fixture"),
        ("namespaced_tool_name", "a Unicode tool name"),
        ("encode_name_pattern", "a Unicode glob pattern"),
        ("verify_audit_chain", "audit-chain verification"),
    ):
        if required not in arabic_suite:
            print(f"Arabic evaluation suite lost {label} ({required})", file=sys.stderr)
            return 1
    invariants = (ROOT / "tests" / "test_mcp_security_invariants.py").read_text(encoding="utf-8")
    for required, label in (
        ("test_sse_notifications_are_recorded_with_their_run_id", "the run_id attribution tripwire"),
        ("test_agent_refuses_a_remote_tool_that_is_registered_safe", "the forced-confirm tripwire"),
        ("test_integrity_gate_rejects_a_remote_tool_registered_below_confirm", "this gate's own tripwire"),
        ("test_handler_called_directly_cannot_grant_itself_consent", "the consent-bypass tripwire"),
        ("def test_local_names_cannot_collide", "the name-collision tripwire"),
    ):
        if required not in invariants:
            print(f"security invariant test missing: {label} ({required})", file=sys.stderr)
            return 1

    print(
        f"integrity PASS: {len(REQUIRED_FILES)} capability files and Python syntax verified "
        f"(MCP revision {targeted})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
