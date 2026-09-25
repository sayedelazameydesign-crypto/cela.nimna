"""Isolated Python execution.

Two backends:

* ``subprocess`` – a fresh interpreter (``python -I``) with a scrubbed
  environment, working directory pinned to the workspace and POSIX resource
  limits (memory / CPU / file size / processes).  Good default for local use
  but *not* a security boundary against a hostile model – hence such calls
  require approval unless configured otherwise.
* ``docker`` – ``python:3.11-slim`` container with ``--network none``,
  memory/CPU/pids limits, all capabilities dropped and only the workspace
  mounted.  Strong isolation; treated as safe.
"""
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings

MAX_OUTPUT = 10_000


class SandboxResult(BaseModel):
    backend: str
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... output truncated, {len(text) - limit} more characters]"


def _make_limiter(memory_mb: int, cpu_seconds: int):
    if os.name != "posix":  # pragma: no cover - windows
        return None
    import resource

    def limiter() -> None:
        mem = memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass
        os.setsid()

    return limiter


def run_in_subprocess(code: str, workspace: Path, *, timeout: int, memory_mb: int) -> SandboxResult:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
    }
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-"],
            input=code,
            capture_output=True,
            text=True,
            cwd=str(workspace),
            env=env,
            timeout=timeout,
            preexec_fn=_make_limiter(memory_mb, timeout),
        )
        return SandboxResult(
            backend="subprocess", exit_code=proc.returncode, stdout=_clip(proc.stdout),
            stderr=_clip(proc.stderr), duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxResult(
            backend="subprocess", exit_code=None,
            stdout=_clip((exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            stderr=f"timed out after {timeout}s", timed_out=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def docker_available() -> bool:
    return shutil.which("docker") is not None


def run_in_docker(code: str, workspace: Path, *, timeout: int, memory_mb: int, image: str) -> SandboxResult:
    if not docker_available():
        return SandboxResult(backend="docker", exit_code=None, stdout="",
                             stderr="docker CLI not found; install Docker or set SANDBOX_BACKEND=subprocess")
    name = f"nimna-sbx-{uuid.uuid4().hex[:8]}"
    cmd = [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none",
        "--memory", f"{memory_mb}m", "--cpus", "1", "--pids-limit", "128",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-v", f"{workspace.resolve()}:/work", "-w", "/work",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "MPLBACKEND=Agg",
        image, "python", "-",
    ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, input=code, capture_output=True, text=True, timeout=timeout + 15)
        return SandboxResult(
            backend="docker", exit_code=proc.returncode, stdout=_clip(proc.stdout),
            stderr=_clip(proc.stderr), duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        return SandboxResult(backend="docker", exit_code=None, stdout="",
                             stderr=f"timed out after {timeout}s", timed_out=True,
                             duration_ms=int((time.perf_counter() - started) * 1000))


def run_python_code(code: str, settings: Settings, workspace: Path,
                    timeout: int | None = None) -> SandboxResult:
    timeout = min(timeout or settings.sandbox_timeout, 300)
    workspace.mkdir(parents=True, exist_ok=True)
    if settings.sandbox_backend == "docker":
        return run_in_docker(code, workspace, timeout=timeout, memory_mb=settings.sandbox_memory_mb,
                             image=settings.sandbox_image)
    return run_in_subprocess(code, workspace, timeout=timeout, memory_mb=settings.sandbox_memory_mb)
