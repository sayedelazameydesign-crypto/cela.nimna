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
    ROOT / "docs" / "mcp-gateway.md",
    ROOT / "nimna" / "models" / "registry.py",
    ROOT / "nimna" / "provenance" / "hashchain.py",
    ROOT / "nimna" / "browser" / "cloud_v4.py",
    ROOT / "nimna" / "mcp" / "contract.py",
    ROOT / "nimna" / "mcp" / "headers.py",
    ROOT / "nimna" / "mcp" / "transport.py",
    ROOT / "nimna" / "mcp" / "gateway.py",
    ROOT / "tests" / "test_mcp_gateway.py",
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

    print(
        f"integrity PASS: {len(REQUIRED_FILES)} capability files and Python syntax verified "
        f"(MCP revision {targeted})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
