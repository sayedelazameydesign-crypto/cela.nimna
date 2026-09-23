"""``run_command`` — the first Execution Fabric primitive (P1-T1).

Contract (docs/architecture/agent-platform-audit.md §9, user contract for P1-T1):

    parse → normalize → classify → policy → confirmation → execute

NOT ``subprocess.run()`` with an anomaly check afterwards: every deny path
below happens *before* a single byte is executed, and classification is a
*signal* for the policy layer — never the security boundary itself. The
boundaries are, in order: the capability flag (``SHELL_TOOL_ENABLED``), the
workspace jail, the policy gate, the confirmation gate, and the sandboxed
process (scrubbed env, pinned cwd, POSIX rlimits, byte-capped output,
kill-on-timeout process group).

Statuses (never a bare boolean):

    SUCCESS | NONZERO_EXIT | TIMEOUT | DENIED |
    CONFIRMATION_REQUIRED | POLICY_BLOCKED | SANDBOX_ERROR

Capability vocabulary (extensible): ``shell.execute`` today; the classifier
also emits ``shell.write`` / ``shell.network`` / ``shell.admin`` /
``shell.destructive`` / ``shell.escape`` signals which the default policy
denies. Future capabilities (``shell.read``, ``shell.process`` …) plug into
the same vocabulary without redesigning the runtime.

Honesty contract: every attempt is audited — ``shell_denied`` for
DENIED / POLICY_BLOCKED / CONFIRMATION_REQUIRED, ``shell_evidence`` for
executions — with sha256 hashes (``command_hash``, ``stdout_hash``,
``stderr_hash``), a filesystem delta, and **no environment values and no raw
command echoed** (only its hash and capability classes).

Two confirmation layers, both real:

* direct callers (suite/tests/API) pass ``explicit_consent=False`` →
  ``CONFIRMATION_REQUIRED`` until they explicitly consent;
* inside the agent loop the existing approval machinery
  (``PolicyEngine`` → ``ApprovalPolicy`` → suspend/resume) is the human
  confirmation and runs *before* the tool handler, so the handler executes
  with ``explicit_consent=True`` — the outer gate already consented.

Registration is gated: with ``SHELL_TOOL_ENABLED=false`` (the default) the
tool is **not registered at all** — code being present never implies the
agent can use it. Even if constructed directly, the executor returns
``DENIED``. Disabled means DENIED, never a fallback-execute.
"""
from __future__ import annotations

import enum
import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..base import Risk, ToolContext, ToolRegistry

TOOL_NAME = "run_command"
ENV_FLAG = "SHELL_TOOL_ENABLED"

MAX_COMMAND_CHARS = 4_000
MAX_STDIN_CHARS = 100_000
MAX_TIMEOUT_MS = 60_000
MIN_TIMEOUT_MS = 100
MAX_OUTPUT_BYTES = 100_000        # returned to the model (clipped)
MAX_HASH_INPUT_BYTES = 1_000_000  # evidence hashes cover up to 1MB per stream
MAX_DELTA_ENTRIES = 200
MAX_DELTA_FILE_BYTES = 256_000    # files bigger than this are listed, not hashed
MAX_ENV_ENTRIES = 16
MAX_ENV_VALUE_CHARS = 4_096

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ShellStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    NONZERO_EXIT = "NONZERO_EXIT"
    TIMEOUT = "TIMEOUT"
    DENIED = "DENIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    SANDBOX_ERROR = "SANDBOX_ERROR"


# --------------------------------------------------------------------------- #
# Request / Result (the P1-T1 contract)
# --------------------------------------------------------------------------- #
class ShellRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=MAX_COMMAND_CHARS,
                         description="Shell command to run inside the workspace jail.")
    cwd: str = Field(".", min_length=1, max_length=512,
                     description="Working directory, workspace-relative (never absolute).")
    timeout_ms: int = Field(10_000, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)
    env: dict[str, str] = Field(default_factory=dict,
                                description="Extra env vars (names only are ever logged).")
    stdin: str = Field("", max_length=MAX_STDIN_CHARS)
    confirmation: bool = Field(False,
                               description="Explicit human consent for direct callers.")


class ShellResult(BaseModel):
    status: ShellStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    timed_out: bool = False
    filesystem_delta: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""  # honest, human-readable reason for every non-executed status

    @property
    def ok(self) -> bool:
        """Ran and returned a verdict (SUCCESS/NONZERO_EXIT/TIMEOUT are verdicts)."""
        return self.status in {ShellStatus.SUCCESS, ShellStatus.NONZERO_EXIT, ShellStatus.TIMEOUT}


# --------------------------------------------------------------------------- #
# parse → normalize → classify  (signals, never authorization)
# --------------------------------------------------------------------------- #
NETWORK_VERBS = {"curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp",
                 "ftp", "telnet", "ping", "ping6", "dig", "nslookup", "host", "rsync"}
ADMIN_VERBS = {"sudo", "su", "doas", "chown", "chmod", "mount", "umount", "systemctl",
               "service", "shutdown", "reboot", "poweroff", "halt", "useradd", "userdel",
               "usermod", "groupadd", "passwd", "crontab", "insmod", "modprobe", "docker",
               "kubectl", "apt", "apt-get", "yum", "dnf", "brew"}
DESTRUCTIVE_VERBS = {"rm", "rmdir", "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4",
                     "mkfs.vfat", "dd", "shred", "fdisk", "parted", "wipefs", "truncate"}
SHELL_INTERPRETERS = {"sh", "bash", "dash", "zsh", "ksh", "powershell", "pwsh", "cmd"}
SENSITIVE_PATH_PARTS = ("/etc/", "/proc/", "/sys/", "/dev/", "/root/", "/.ssh/",
                        "id_rsa", "id_ed25519", "authorized_keys", ".env", ".aws", ".gnupg",
                        "/var/log/", "/boot/")
_REDIRECTION_RE = re.compile(r"(>>|>&|2>&1|2>|&>|>|<)")


class CommandClassification:
    def __init__(self) -> None:
        self.capabilities: set[str] = {"shell.execute"}
        self.notes: list[str] = []

    def add(self, capability: str, note: str) -> None:
        self.capabilities.add(capability)
        self.notes.append(note)

    @property
    def blocked_classes(self) -> list[str]:
        """Capabilities the default policy denies (everything beyond execute/write)."""
        return sorted(self.capabilities & {
            "shell.network", "shell.admin", "shell.destructive", "shell.escape",
        })


def _norm_abs(token: str) -> str:
    try:
        return str(PurePosixPath(token))
    except Exception:  # pragma: no cover - defensive
        return token


def _path_is_escape(token: str) -> bool:
    """Absolute paths, ``..`` traversals and sensitive locations are escape signals.

    The command runs with cwd pinned inside the workspace; anything that names
    the outside world (absolute path, traversal, sensitive dir) is treated as
    an escape *signal* and denied by policy — the jail/limits stay the real
    boundary.
    """
    raw = token.strip("'\"")
    if not raw or raw.startswith("-") or "$" in raw:
        # "$VAR" values are unknown at classify time → conservatively not an
        # escape by *path*, other classes still apply.
        return False
    if raw.startswith("/") or raw.startswith("~"):
        lowered = raw.lower()
        if any(part in lowered for part in SENSITIVE_PATH_PARTS):
            return True
        return True  # any absolute/home path: outside the relative workspace
    parts = PurePosixPath(raw).parts
    if ".." in parts:
        return True
    lowered = raw.lower()
    return any(part in lowered for part in SENSITIVE_PATH_PARTS)


def _split_pipeline_segments(command: str) -> list[list[str]]:
    """Tokenize per pipeline segment (``a | b > c`` → [[a], [b]]) honoring quotes."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # unbalanced quotes are themselves suspicious, but keep parsing signals
        tokens = command.split()
    segments: list[list[str]] = [[]]
    pending_redirect = False
    for token in tokens:
        if _REDIRECTION_RE.fullmatch(token):
            pending_redirect = True
            continue
        if token == "|":
            segments.append([])
            pending_redirect = False
            continue
        if pending_redirect:
            pending_redirect = False
            continue  # redirect target token — analysed separately below
        segments[-1].append(token)
    return segments


def classify_command(command: str) -> CommandClassification:
    """Parse → normalize → classify. Emits capability signals; decides nothing."""
    classification = CommandClassification()
    lowered = command.lower()

    # pipes into an interpreter: curl … | sh — network-fed execution
    if re.search(r"\|\s*(sudo\s+)?(ba|z|da)?sh\b", lowered) or re.search(
        r"\|\s*(powershell|pwsh|cmd)\b", lowered
    ):
        classification.add("shell.network", "pipe into a shell interpreter")
        classification.add("shell.escape", "remote code execution pattern")

    for segment in _split_pipeline_segments(command):
        if not segment:
            continue
        verb = segment[0].split("/", 1)[-1]  # /bin/rm == rm
        verb = verb.lower()
        if verb in NETWORK_VERBS:
            classification.add("shell.network", f"network verb: {verb}")
        if verb in ADMIN_VERBS:
            classification.add("shell.admin", f"admin verb: {verb}")
        if verb in DESTRUCTIVE_VERBS:
            classification.add("shell.destructive", f"destructive verb: {verb}")
        if verb in {"rm"} and any(flag in {"-r", "-rf", "-fr", "--recursive"} for flag in segment[1:]):
            classification.add("shell.destructive", "rm -r* (recursive delete)")
        if verb in {"tee", "cp", "mv", "dd", "install"}:
            classification.add("shell.write", f"write verb: {verb}")

    # redirection / write targets and any path-like token: escape analysis
    for token in shlex_split_safe(command):
        if token in {"|", ">", ">>", "<", "2>", "2>&1", "&>", "&&", "||"}:
            continue
        if _path_is_escape(token):
            classification.add("shell.escape", f"outside-workspace path signal")
            break

    # command substitution writing outside (defensive signal)
    if re.search(r"\$\(\s*(>/|>>/|tee\s+/)", command) or re.search(r"`\s*>/`, ", command):
        classification.add("shell.escape", "substitution write outside workspace")
    return classification


def shlex_split_safe(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


# --------------------------------------------------------------------------- #
# Filesystem delta
# --------------------------------------------------------------------------- #
def _snapshot(workspace: Path) -> dict[str, Optional[str]]:
    state: dict[str, Optional[str]] = {}
    count = 0
    for path in sorted(workspace.rglob("*")):
        if count >= MAX_DELTA_ENTRIES:
            break
        if not path.is_file():
            continue
        rel = str(path.relative_to(workspace))
        try:
            if path.stat().st_size > MAX_DELTA_FILE_BYTES:
                state[rel] = None  # too big to hash — presence only
            else:
                state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            count += 1
        except OSError:
            state[rel] = None
    return state


def _filesystem_delta(before: dict[str, Optional[str]], after: dict[str, Optional[str]]) -> list[dict[str, Any]]:
    delta: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is None and new is not None:
            delta.append({"change": "added", "path": path, "sha256": new})
        elif new is None and old is not None:
            delta.append({"change": "removed", "path": path, "sha256_old": old})
        else:
            delta.append({"change": "modified", "path": path,
                          "sha256_old": old, "sha256": new})
        if len(delta) >= MAX_DELTA_ENTRIES:
            break
    return delta


# --------------------------------------------------------------------------- #
# Executor — the ordered pipeline
# --------------------------------------------------------------------------- #
def _audit(memory: Any, session_id: Optional[str], run_id: Optional[str],
           event: str, payload: dict[str, Any]) -> None:
    if memory is None:
        return
    try:
        memory.log(session_id, run_id, event, payload)
    except Exception:  # pragma: no cover - auditing must never crash execution
        pass


def _scrub_env(request_env: dict[str, str], workspace: Path) -> tuple[dict[str, str], list[str]]:
    """Minimal env + caller extras. Values are never logged, only key names."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
    }
    accepted: list[str] = []
    for key, value in (request_env or {}).items():
        if len(accepted) >= MAX_ENV_ENTRIES:
            break
        if not _ENV_KEY_RE.fullmatch(key) or key.startswith(("LD_", "IFS")):
            continue
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE_CHARS:
            continue
        env[key] = value
        accepted.append(key)
    return env, sorted(accepted)


def _clip_bytes(data: bytes, limit: int) -> tuple[str, int, bool]:
    total = len(data)
    clipped = total > limit
    return data[:limit].decode("utf-8", errors="replace"), total, clipped


def _hash_stream(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data[:MAX_HASH_INPUT_BYTES]).hexdigest()


def _resolve_cwd(workspace_root: Path, rel: str) -> Path:
    """Same jail philosophy as ToolContext.resolve_path, for direct callers."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or not str(rel).strip():
        raise ValueError(f"cwd escapes the workspace: {rel!r}")
    target = (workspace_root / rel).resolve()
    root = workspace_root.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"cwd escapes the workspace: {rel!r}")
    if target.is_symlink():
        raise ValueError(f"cwd is a symlink: {rel!r}")
    return target


def _spawn(command: str, cwd: Path, env: dict[str, str], stdin_text: str,
           timeout_s: float, memory_mb: int) -> tuple[bytes, bytes, Optional[int], bool, int]:
    """Run under /bin/sh -c with a new process group; kill the group on timeout."""
    from ..sandbox import _make_limiter  # reuse the exact POSIX rlimits of the python sandbox

    started = time.perf_counter()
    preexec = _make_limiter(memory_mb, max(int(timeout_s), 1))
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=str(cwd), env=env,
        stdin=subprocess.PIPE if stdin_text else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # _make_limiter already calls os.setsid() in preexec — a second
        # setsid (start_new_session=True) would fail with EPERM.
        preexec_fn=preexec,
    )
    timed_out = False
    try:
        out, err = proc.communicate(input=stdin_text.encode("utf-8") if stdin_text else None,
                                    timeout=timeout_s)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        out, err = proc.communicate()
    duration_ms = int((time.perf_counter() - started) * 1000)
    return out or b"", err or b"", exit_code, timed_out, duration_ms


def execute_shell(request: ShellRequest, *, settings: Any, workspace_root: Path,
                  explicit_consent: bool = False,
                  memory: Any = None, session_id: Optional[str] = None,
                  run_id: Optional[str] = None) -> ShellResult:
    """Full P1-T1 pipeline. Every deny happens BEFORE execution; everything is audited."""
    command_hash = "sha256:" + hashlib.sha256(request.command.strip().encode("utf-8")).hexdigest()

    # 0) capability flag — disabled means DENIED, never fallback-execute
    if not getattr(settings, "shell_tool_enabled", False):
        result = ShellResult(status=ShellStatus.DENIED,
                             reason="shell capability is disabled (SHELL_TOOL_ENABLED=false)")
        _audit(memory, session_id, run_id, "shell_denied", _denial_payload(request, command_hash, result))
        return result

    # 1) parse → normalize → classify (signals only)
    classification = classify_command(request.command)

    # 2) cwd jail (before any execution)
    try:
        cwd_path = _resolve_cwd(workspace_root, request.cwd)
    except ValueError as exc:
        result = ShellResult(status=ShellStatus.POLICY_BLOCKED,
                             reason=f"cwd-escape: {exc}",
                             evidence=_base_evidence(request, command_hash, classification, "policy"))
        _audit(memory, session_id, run_id, "shell_denied", result.evidence | {"status": result.status, "reason": result.reason})
        return result

    # 3) policy over classification — default-deny beyond execute/write
    blocked = classification.blocked_classes
    if blocked:
        result = ShellResult(
            status=ShellStatus.POLICY_BLOCKED,
            reason="default-deny policy blocked capability classes: " + ", ".join(blocked)
                   + " (classification is a signal; the sandbox is the boundary)",
            evidence=_base_evidence(request, command_hash, classification, "policy"),
        )
        _audit(memory, session_id, run_id, "shell_denied", result.evidence | {"status": result.status, "reason": result.reason})
        return result

    # 4) confirmation gate (direct callers; the agent loop consents via its
    #    own approval machinery BEFORE the handler runs)
    if not (explicit_consent or request.confirmation):
        result = ShellResult(status=ShellStatus.CONFIRMATION_REQUIRED,
                             reason="human confirmation required before shell execution",
                             evidence=_base_evidence(request, command_hash, classification, "confirmation"))
        _audit(memory, session_id, run_id, "shell_denied", result.evidence | {"status": result.status, "reason": result.reason})
        return result

    # 5) sandboxed execution
    workspace_root.mkdir(parents=True, exist_ok=True)
    cwd_path.mkdir(parents=True, exist_ok=True)
    env, env_keys = _scrub_env(request.env, workspace_root)
    timeout_s = min(max(request.timeout_ms, MIN_TIMEOUT_MS), MAX_TIMEOUT_MS) / 1000.0
    max_out = int(getattr(settings, "shell_max_output_bytes", MAX_OUTPUT_BYTES) or MAX_OUTPUT_BYTES)
    before = _snapshot(workspace_root)
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        out_bytes, err_bytes, exit_code, timed_out, duration_ms = _spawn(
            request.command, cwd_path, env, request.stdin, timeout_s,
            memory_mb=int(getattr(settings, "sandbox_memory_mb", 512)),
        )
    except OSError as exc:
        result = ShellResult(status=ShellStatus.SANDBOX_ERROR,
                             reason=f"sandbox failed to start: {exc.__class__.__name__}",
                             duration_ms=0,
                             evidence=_base_evidence(request, command_hash, classification, "sandbox"))
        _audit(memory, session_id, run_id, "shell_denied", result.evidence | {"status": result.status, "reason": result.reason})
        return result

    after = _snapshot(workspace_root)
    delta = _filesystem_delta(before, after)
    stdout_text, stdout_total, stdout_clipped = _clip_bytes(out_bytes, max_out)
    stderr_text, stderr_total, stderr_clipped = _clip_bytes(err_bytes, max_out)

    if timed_out:
        status = ShellStatus.TIMEOUT
    elif exit_code == 0:
        status = ShellStatus.SUCCESS
    else:
        status = ShellStatus.NONZERO_EXIT

    evidence = _base_evidence(request, command_hash, classification, "approved")
    evidence.update({
        "started_at": started_at,
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "stdout_hash": _hash_stream(out_bytes),
        "stderr_hash": _hash_stream(err_bytes),
        "stdout_bytes": stdout_total,
        "stderr_bytes": stderr_total,
        "stdout_clipped": stdout_clipped,
        "stderr_clipped": stderr_clipped,
        "filesystem_delta": delta,
        "env_keys": env_keys,
        "backend": "subprocess",
        "timed_out": timed_out,
        "status": status.value,
    })
    result = ShellResult(
        status=status,
        stdout=stdout_text,
        stderr=stderr_text,
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        filesystem_delta=delta,
        evidence=evidence,
        reason="" if status is not ShellStatus.TIMEOUT else f"timed out after {request.timeout_ms} ms",
    )
    _audit(memory, session_id, run_id, "shell_evidence", evidence)
    return result


def _base_evidence(request: ShellRequest, command_hash: str,
                   classification: CommandClassification, authorization: str) -> dict[str, Any]:
    return {
        "tool": TOOL_NAME,
        "command_hash": command_hash,   # the raw command is never echoed
        "cwd": request.cwd,
        "policy": "default-deny",
        "authorization": authorization,
        "capabilities": sorted(classification.capabilities),
        "classification_notes": classification.notes[:12],
    }


def _denial_payload(request: ShellRequest, command_hash: str, result: ShellResult) -> dict[str, Any]:
    return {
        "tool": TOOL_NAME,
        "command_hash": command_hash,
        "cwd": request.cwd,
        "policy": "default-deny",
        "status": result.status.value,
        "reason": result.reason,
    }


# --------------------------------------------------------------------------- #
# Tool registration — gated by SHELL_TOOL_ENABLED (default: not registered).
# Named ``run_command``: ``shell_execute`` is already taken by the computer
# pack (bash inside the isolated desktop container); this is the local
# workspace-jail primitive.
# --------------------------------------------------------------------------- #
class ShellParams(BaseModel):
    """What the *model* may pass. ``env`` and ``confirmation`` are intentionally
    absent: env injection is not a model capability, and consent comes from the
    human approval machinery, not from the model."""
    command: str = Field(..., min_length=1, max_length=MAX_COMMAND_CHARS,
                         description="Shell command to run inside the workspace (no network, "
                                     "no paths outside the workspace, no sudo/rm -rf).")
    cwd: str = Field(".", min_length=1, max_length=512,
                     description="Workspace-relative working directory.")
    timeout_ms: int = Field(10_000, ge=MIN_TIMEOUT_MS, le=MAX_TIMEOUT_MS)
    stdin: str = Field("", max_length=10_000)
    purpose: str = Field("", description="One line: what this command does (shown to the user).")


def _enabled_from_env() -> bool:
    return (os.getenv(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"})


def shell_enabled(settings: Any = None) -> bool:
    if settings is not None and hasattr(settings, "shell_tool_enabled"):
        return bool(settings.shell_tool_enabled)
    return _enabled_from_env()


def register(registry: ToolRegistry) -> None:
    """Register ``run_command`` only when the capability flag is on."""
    if not _enabled_from_env():
        return  # disabled → not registered at all; never a silent fallback

    @registry.tool(
        TOOL_NAME,
        "Execute a shell command inside the workspace jail (sandboxed: scrubbed env, "
        "resource limits, output capped, kill on timeout). No network, no absolute "
        "paths, no admin/destructive verbs — those are denied by policy before "
        "execution. Returns stdout/stderr/exit_code/filesystem_delta/evidence.",
        ShellParams, risk="confirm", tags=["shell", "execution"],
    )
    def run_command(params: ShellParams, ctx: ToolContext):
        # The agent's approval machinery (PolicyEngine + ApprovalPolicy with
        # suspend/resume) is the human confirmation and has already consented
        # when this handler runs — hence explicit_consent=True here.
        request = ShellRequest(
            command=params.command, cwd=params.cwd, timeout_ms=params.timeout_ms,
            stdin=params.stdin, confirmation=True,
        )
        result = execute_shell(
            request, settings=ctx.settings, workspace_root=ctx.workspace,
            explicit_consent=True, memory=ctx.memory,
            session_id=ctx.session_id, run_id=ctx.run_id,
        )
        payload = result.model_dump()
        payload["ok"] = result.ok
        return payload
