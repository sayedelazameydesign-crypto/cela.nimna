"""Workspace file tools (jailed to the workspace directory)."""
import fnmatch
import os
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..base import Risk, ToolContext, ToolError, ToolRegistry

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html",
    ".css", ".xml", ".log", ".ini", ".toml", ".cfg", ".sql", ".sh", ".env", ".rst",
}


def _is_probably_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class ListFilesParams(BaseModel):
    path: str = Field(".", description="Directory inside the workspace (relative path).")
    pattern: str = Field("*", description="Glob pattern to filter names, e.g. '*.csv'.")
    recursive: bool = Field(False, description="Recurse into sub-directories.")
    max_entries: int = Field(200, ge=1, le=2000)


class ReadFileParams(BaseModel):
    path: str = Field(..., description="Text file inside the workspace (relative path).")
    max_chars: int = Field(8000, ge=100, le=100000, description="Maximum characters to return.")
    offset: int = Field(0, ge=0, description="Character offset to start from (for paging).")


class FileInfoParams(BaseModel):
    path: str = Field(..., description="File or directory inside the workspace.")


class WriteFileParams(BaseModel):
    path: str = Field(..., description="Destination path inside the workspace.")
    content: str = Field(..., description="Full text content to write.")
    overwrite: bool = Field(False, description="Set true to replace an existing file (needs approval).")


class DeleteFileParams(BaseModel):
    path: str = Field(..., description="File inside the workspace to delete.")


def _write_risk(params: BaseModel, ctx: ToolContext) -> Risk:
    assert isinstance(params, WriteFileParams)
    try:
        target = ctx.resolve_path(params.path)
    except ToolError:
        return "safe"  # validation will fail later anyway
    return "confirm" if target.exists() else "safe"


def register(registry: ToolRegistry) -> None:
    @registry.tool("list_files", "List files and folders inside the workspace directory.",
                   ListFilesParams, tags=["files"])
    def list_files(params: ListFilesParams, ctx: ToolContext):
        root = ctx.resolve_path(params.path, must_exist=True)
        if not root.is_dir():
            raise ToolError(f"'{params.path}' is not a directory")
        entries = []
        iterator = root.rglob("*") if params.recursive else root.iterdir()
        for entry in sorted(iterator):
            try:
                rel = entry.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if not fnmatch.fnmatch(entry.name, params.pattern):
                continue
            # ensure each entry is still inside the workspace jail (symlink may point elsewhere)
            try:
                ctx.resolve_path(ctx.display_path(entry))
            except ToolError:
                continue
            stat = entry.stat()
            entries.append({
                "path": ctx.display_path(entry),
                "type": "dir" if entry.is_dir() else "file",
                "size": stat.st_size if entry.is_file() else None,
            })
            if len(entries) >= params.max_entries:
                break
        return {"directory": ctx.display_path(root), "count": len(entries), "entries": entries}

    @registry.tool("read_file", "Read a UTF-8 text file from the workspace (paged with offset/max_chars).",
                   ReadFileParams, tags=["files"])
    def read_file(params: ReadFileParams, ctx: ToolContext):
        target = ctx.resolve_path(params.path, must_exist=True)
        if not target.is_file():
            raise ToolError(f"'{params.path}' is not a file")
        raw = target.read_bytes()
        if len(raw) > ctx.settings.max_file_bytes:
            raise ToolError(f"file is larger than {ctx.settings.max_file_bytes // 1_000_000} MB; use run_python or csv tools instead")
        if target.suffix.lower() not in TEXT_EXTENSIONS and not _is_probably_text(raw[:4096]):
            raise ToolError("file appears to be binary; only text files can be read")
        text = raw.decode("utf-8", errors="replace")
        chunk = text[params.offset: params.offset + params.max_chars]
        return {
            "path": ctx.display_path(target),
            "total_chars": len(text),
            "offset": params.offset,
            "returned_chars": len(chunk),
            "has_more": params.offset + params.max_chars < len(text),
            "content": chunk,
        }

    @registry.tool("file_info", "Get size, type, modification time and line count of a workspace path.",
                   FileInfoParams, tags=["files"])
    def file_info(params: FileInfoParams, ctx: ToolContext):
        target = ctx.resolve_path(params.path, must_exist=True)
        stat = target.stat()
        info = {
            "path": ctx.display_path(target),
            "type": "dir" if target.is_dir() else "file",
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(timespec="seconds"),
            "extension": target.suffix.lower(),
        }
        if target.is_file() and stat.st_size < ctx.settings.max_file_bytes:
            raw = target.read_bytes()
            if _is_probably_text(raw[:4096]):
                info["lines"] = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
        return info

    @registry.tool("write_file", "Create a text file in the workspace. Overwriting an existing file requires approval.",
                   WriteFileParams, risk_fn=_write_risk, tags=["files", "write"])
    def write_file(params: WriteFileParams, ctx: ToolContext):
        if len(params.content.encode("utf-8")) > ctx.settings.max_write_bytes:
            raise ToolError(f"content too large ({len(params.content.encode('utf-8'))} bytes); max is {ctx.settings.max_write_bytes} bytes")
        target = ctx.resolve_path(params.path)
        if target.exists() and not params.overwrite:
            raise ToolError(f"'{params.path}' already exists; set overwrite=true (requires approval)")
        if target.is_dir():
            raise ToolError(f"'{params.path}' is a directory")
        # re-check jail after resolving parent (handles symlink dirs)
        target.parent.mkdir(parents=True, exist_ok=True)
        # verify the final path still resolves inside workspace after parent creation
        resolved = target.resolve()
        root = ctx.workspace.resolve()
        if resolved != root and root not in resolved.parents:
            raise ToolError("write would escape the workspace")
        target.write_text(params.content, encoding="utf-8")
        return {"written": ctx.display_path(target), "bytes": len(params.content.encode("utf-8"))}

    @registry.tool("delete_file", "Delete a file inside the workspace. Always requires approval.",
                   DeleteFileParams, risk="confirm", tags=["files", "destructive"])
    def delete_file(params: DeleteFileParams, ctx: ToolContext):
        target = ctx.resolve_path(params.path, must_exist=True)
        if target.is_dir():
            raise ToolError("refusing to delete a directory; delete files individually")
        os.remove(target)
        return {"deleted": ctx.display_path(target)}
