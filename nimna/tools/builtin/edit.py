"""T8 — Precise file edit over the unified path (Fabric-first).

Audit contract (``docs/architecture/agent-platform-audit.md`` rows 129/366):

* ``edit_file(path, old_text, new_text, expect_once=True)`` — explicit failure
  when ``old_text`` is missing OR ambiguous (repeated); never a silent
  first-match replace.
* ``apply_patch(diff)`` — unified-diff subset (``---/+++/@@`` with `` ``/-/+
  lines), all-or-nothing: every hunk is verified against the current content
  BEFORE anything is written; any mismatch/malformation = explicit error and
  zero writes.
* hard 1 MiB cap on every file read or written here (``EDIT_MAX_BYTES``).
* workspace jail: every path — including paths inside a patch — resolves
  inside the workspace or is refused (same semantics as ``files.py``).

One core, two doors (T7.1 contract):
* legacy door — ``register(registry)`` pydantic tools (confirm risk);
* Fabric door — ``register_fabric(exec_registry, ...)`` descriptors with the
  SAME core functions, so a gateway-bound agent gets them through
  policy → authorization → executor → evidence (hash-chained), and an
  unregistered tool stays refused (NOT_IN_GATEWAY).
No disable flag for any safety check here — removing one IS the regression.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import BaseModel, Field

from ..base import ToolContext, ToolError, ToolRegistry

EDIT_MAX_BYTES = 1_000_000          # audit row 366: 1MB file limit — hard cap
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --------------------------------------------------------------------------- #
# core — pure functions shared by BOTH doors (no ToolContext dependency)
# --------------------------------------------------------------------------- #
def _read_text(path, where: str) -> str:
    if not path.exists() or not path.is_file():
        raise ToolError(f"{where}: file not found: {path.name}")
    size = path.stat().st_size
    if size > EDIT_MAX_BYTES:
        raise ToolError(f"{where}: file exceeds the {EDIT_MAX_BYTES}-byte edit cap ({size} bytes)")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{where}: binary content is not editable (utf-8 decode failed)") from exc


def _resolve_inside_jail(ctx: ToolContext, raw: str):
    resolved = ctx.resolve_path(raw)
    try:
        inside = resolved.resolve().is_relative_to(ctx.workspace.resolve())
    except (ValueError, OSError) as exc:
        raise ToolError(f"path escapes the workspace jail: {raw!r}") from exc
    if not inside:
        raise ToolError(f"path escapes the workspace jail: {raw!r}")
    return resolved


def apply_edit(workspace_path, old_text: str, new_text: str, expect_once: bool = True) -> dict:
    """Replace old_text→new_text in an existing file. Explicit failures:
    missing (0 hits), ambiguous (>1 hits with expect_once), identical texts,
    oversize result. Returns an evidence dict — never file content."""
    if old_text == new_text:
        raise ToolError("edit_file: old_text and new_text are identical — nothing to do")
    text = _read_text(workspace_path, "edit_file")
    hits = text.count(old_text)
    if hits == 0:
        raise ToolError("edit_file: old_text not found in target file (0 occurrences)")
    if hits > 1 and expect_once:
        raise ToolError(f"edit_file: old_text is ambiguous ({hits} occurrences); "
                        "refine it or pass expect_once=false to replace every occurrence")
    new = text.replace(old_text, new_text) if expect_once else text.replace(old_text, new_text)
    encoded = new.encode("utf-8")
    if len(encoded) > EDIT_MAX_BYTES:
        raise ToolError(f"edit_file: result exceeds the {EDIT_MAX_BYTES}-byte cap")
    workspace_path.write_text(new, encoding="utf-8")
    return {"status": "EDITED", "path": workspace_path.name,
            "replaced": 1 if expect_once else hits,
            "bytes": len(encoded)}


def _patch_paths(header_line: str, prefix_strip: bool = True) -> str:
    raw = header_line[4:].strip().split("\t")[0]
    if raw == "/dev/null":
        return "/dev/null"
    return raw[2:] if prefix_strip and raw[:2] in ("a/", "b/") else raw


def apply_unified_diff(workspace_root, diff_text: str, resolve: Callable[[str], Any]) -> dict:
    """Strict unified-diff subset, ALL-OR-NOTHING: parse every file-section and
    verify every hunk against current on-disk content first; only then write.
    Any malformation / context mismatch / jail escape ⇒ error, ZERO writes.
    Creation/deletion sections (/dev/null) are refused explicitly in v1."""
    if not diff_text or not diff_text.strip():
        raise ToolError("apply_patch: empty diff")
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sections, i, n = [], 0, len(lines)
    while i < n:
        if not lines[i].startswith("--- "):
            raise ToolError(f"apply_patch: malformed patch at line {i + 1} "
                            f"(expected '--- ' header, got {lines[i][:32]!r})")
        if i + 1 >= n or not lines[i + 1].startswith("+++ "):
            raise ToolError(f"apply_patch: '--- ' without '+++ ' at line {i + 1}")
        old_p, new_p = _patch_paths(lines[i]), _patch_paths(lines[i + 1])
        if "/dev/null" in (old_p, new_p):
            raise ToolError("apply_patch: creation/deletion sections (/dev/null) "
                            "are not supported — use write_file/delete_file")
        if old_p != new_p:
            raise ToolError(f"apply_patch: rename sections are not supported "
                            f"({old_p!r} → {new_p!r})")
        i += 2
        hunks: list[dict] = []
        while i < n and lines[i].startswith("@@"):
            m = _HUNK_RE.match(lines[i])
            i += 1
            body: list[tuple[str, str]] = []
            while i < n and (lines[i].startswith((" ", "+", "-"))
                             or lines[i].startswith(_NO_NEWLINE_MARKER)):
                if lines[i].startswith(_NO_NEWLINE_MARKER):
                    raise ToolError("apply_patch: '\\ No newline' markers are not supported")
                body.append((lines[i][0], lines[i][1:]))
                i += 1
            hunks.append({"old_start": int(m.group(1)), "body": body})
        if not hunks:
            raise ToolError(f"apply_patch: section for {new_p!r} has no @@ hunks")
        sections.append({"path": new_p, "hunks": hunks})

    staged: list[tuple[Any, str, int]] = []
    for section in sections:
        target = resolve(section["path"])            # jail check — refusal before any read
        text = _read_text(target, "apply_patch")
        src = text.split("\n")
        if text.endswith("\n"):
            src.pop()                                 # keep line semantics without the EOF marker
        out, offset, applied = list(src), 0, 0
        for idx, hunk in enumerate(section["hunks"], 1):
            old_seq = [text for kind, text in hunk["body"] if kind in (" ", "-")]
            new_seq = [text for kind, text in hunk["body"] if kind in (" ", "+")]
            pos = hunk["old_start"] - 1 + offset
            if pos < 0 or pos + len(old_seq) > len(out):
                raise ToolError(f"apply_patch: hunk #{idx} of {section['path']!r} "
                                "points outside the current file")
            if out[pos:pos + len(old_seq)] != old_seq:
                raise ToolError(f"apply_patch: hunk #{idx} of {section['path']!r} "
                                "failed to apply (context mismatch) — nothing was written")
            out[pos:pos + len(old_seq)] = new_seq
            offset += len(new_seq) - len(old_seq)
            applied += 1
        result = "\n".join(out) + ("\n" if text.endswith("\n") else "")
        encoded = result.encode("utf-8")
        if len(encoded) > EDIT_MAX_BYTES:
            raise ToolError(f"apply_patch: patched {section['path']!r} exceeds the "
                            f"{EDIT_MAX_BYTES}-byte cap — nothing was written")
        staged.append((target, result, applied))
    for target, result, _ in staged:                 # writes happen ONLY after full validation
        target.write_text(result, encoding="utf-8")
    return {"status": "PATCHED", "files": sorted(s[0].name for s in staged),
            "hunks": sum(s[2] for s in staged)}


# --------------------------------------------------------------------------- #
# legacy door — pydantic tools on the classic registry (confirm risk)
# --------------------------------------------------------------------------- #
class EditFileParams(BaseModel):
    path: str = Field(..., min_length=1, max_length=512)
    old_text: str = Field(..., min_length=1)
    new_text: str = Field(...)
    expect_once: bool = Field(True, description="True ⇒ exactly one occurrence required")


class ApplyPatchParams(BaseModel):
    diff: str = Field(..., min_length=1, max_length=4 * EDIT_MAX_BYTES)


def register(registry: ToolRegistry) -> None:
    @registry.tool(
        "edit_file",
        "Precise in-file replace: old_text must occur exactly once (expect_once) — "
        "explicit failure on missing/ambiguous match. Workspace jail + 1MB cap.",
        EditFileParams, risk="confirm", tags=["files", "edit"],
    )
    def edit_file(params: EditFileParams, ctx: ToolContext):
        target = _resolve_inside_jail(ctx, params.path)
        return apply_edit(target, params.old_text, params.new_text, params.expect_once)

    @registry.tool(
        "apply_patch",
        "Apply a unified diff inside the workspace. All-or-nothing: every hunk is "
        "verified before anything is written. No /dev/null, no renames, 1MB cap.",
        ApplyPatchParams, risk="confirm", tags=["files", "edit"],
    )
    def apply_patch(params: ApplyPatchParams, ctx: ToolContext):
        return apply_unified_diff(ctx.workspace, params.diff,
                                  resolve=lambda raw: _resolve_inside_jail(ctx, raw))


# --------------------------------------------------------------------------- #
# Fabric door — descriptors for the execution registry (T7.1 unified path)
# --------------------------------------------------------------------------- #
def register_fabric(exec_registry, *, workspace_root, resolve: Callable[[str], Any],
                    version: str = "1.0.0") -> None:
    """Register edit_file/apply_patch on the EXECUTION registry so a
    gateway-bound agent routes them through policy→authorization→evidence.
    ``resolve`` must enforce the jail (it receives the raw path)."""
    exec_registry.register(_descriptor(
        "edit_file",
        {"type": "object",
         "properties": {"path": {"type": "string", "minLength": 1},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                        "expect_once": {"type": "boolean"}},
         "required": ["path", "old_text", "new_text"],
         "additionalProperties": False},
        {"type": "object",
         "properties": {"status": {"type": "string"}, "path": {"type": "string"},
                        "replaced": {"type": "integer"}, "bytes": {"type": "integer"}},
         "required": ["status", "path", "replaced"]},
        lambda args: apply_edit(resolve(args["path"]), args["old_text"],
                                args.get("new_text", ""), args.get("expect_once", True)),
        version))

    exec_registry.register(_descriptor(
        "apply_patch",
        {"type": "object",
         "properties": {"diff": {"type": "string", "minLength": 1}},
         "required": ["diff"], "additionalProperties": False},
        {"type": "object",
         "properties": {"status": {"type": "string"}, "files": {"type": "array"},
                        "hunks": {"type": "integer"}},
         "required": ["status", "files"]},
        lambda args: apply_unified_diff(workspace_root, args["diff"], resolve=resolve),
        version))


def _descriptor(tool_id: str, input_schema: dict, output_schema: dict,
                handler: Callable[[dict], dict], version: str):
    from ...execution.tool_registry import ToolDescriptor
    return ToolDescriptor(
        tool_id=tool_id, version=version, input_schema=input_schema,
        output_schema=output_schema, capabilities=(), side_effects=("filesystem",),
        risk_level="MEDIUM", handler=handler)
