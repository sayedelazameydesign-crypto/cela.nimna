"""Runtime configuration.

Everything is read from environment variables (optionally seeded from a `.env`
file) so that API keys never live inside the code base.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def load_dotenv(path: str | os.PathLike = ".env", *, override: bool = False) -> bool:
    """Minimal `.env` loader (KEY=VALUE, `#` comments, optional quotes).

    Returns True when a file was found and parsed.
    """
    p = Path(path)
    if not p.is_file():
        return False
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        # strip inline comments for unquoted values
        if value and value[0] not in "\"'" and " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value
    return True


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_list(name: str, default: Iterable[str]) -> list[str]:
    value = _env(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _key_source(primary: str, fallback: str) -> tuple[str | None, str | None]:
    """Return (value, source_name) respecting priority."""
    for name in (primary, fallback):
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip(), name
    return None, None


@dataclass
class Settings:
    """All tunables of the agent. Build with :meth:`Settings.from_env`."""

    # model provider
    provider: str = "gemini"  # gemini | openai | mock
    gemini_api_key: str | None = None
    gemini_key_source: str | None = None  # which env var supplied it
    gemini_model: str = "gemini-2.5-flash"
    openai_base_url: str = "https://integrate.api.nvidia.com/v1"
    openai_api_key: str | None = None
    openai_key_source: str | None = None
    openai_model: str = "meta/llama-3.3-70b-instruct"
    temperature: float = 0.2
    request_timeout: float = 120.0
    # token budget forwarded to the model (where the provider supports it)
    max_response_tokens: int = 4096

    # agent behaviour – hard limits that kill runaway loops
    max_steps: int = 12
    max_tool_calls: int = 30
    max_runtime_seconds: int = 300
    max_consecutive_failures: int = 5
    max_skills: int = 3
    history_messages: int = 20
    verify: bool = True
    auto_approve: bool = False
    default_tools: list[str] = field(
        default_factory=lambda: ["load_skill", "memory_search", "list_files"]
    )
    tool_result_max_chars: int = 12000

    # input / file size caps
    max_user_message_chars: int = 20000
    max_file_bytes: int = 5_000_000
    max_write_bytes: int = 2_000_000

    # paths
    skills_dir: Path = Path("skills")
    workspace_dir: Path = Path("workspace")
    db_path: Path = Path("data/nimna.db")

    # python sandbox – subprocess is safe default (asks for approval); docker needs opt-in profile
    sandbox_backend: str = "subprocess"  # docker | subprocess
    sandbox_image: str = "python:3.11-slim"
    sandbox_timeout: int = 20
    sandbox_memory_mb: int = 512

    # infra — Vision Gateway cache + scaling
    redis_url: str | None = None
    vision_cache_ttl: int = 600

    # server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        # Gemini: GEMINI_API_KEY takes precedence over GOOGLE_API_KEY
        gemini_key, gemini_src = _key_source("GEMINI_API_KEY", "GOOGLE_API_KEY")
        # OpenAI/NVIDIA: OPENAI_API_KEY takes precedence over NVIDIA_API_KEY
        openai_key, openai_src = _key_source("OPENAI_API_KEY", "NVIDIA_API_KEY")
        return cls(
            provider=(_env("MODEL_PROVIDER", "gemini") or "gemini").lower(),
            gemini_api_key=gemini_key,
            gemini_key_source=gemini_src,
            gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
            openai_base_url=_env("OPENAI_BASE_URL", "https://integrate.api.nvidia.com/v1")
            or "https://integrate.api.nvidia.com/v1",
            openai_api_key=openai_key,
            openai_key_source=openai_src,
            openai_model=_env("OPENAI_MODEL", "meta/llama-3.3-70b-instruct")
            or "meta/llama-3.3-70b-instruct",
            temperature=_env_float("MODEL_TEMPERATURE", 0.2),
            request_timeout=_env_float("MODEL_TIMEOUT", 120.0),
            max_response_tokens=_env_int("AGENT_MAX_RESPONSE_TOKENS", 4096),
            max_steps=_env_int("AGENT_MAX_STEPS", 12),
            max_tool_calls=_env_int("AGENT_MAX_TOOL_CALLS", 30),
            max_runtime_seconds=_env_int("AGENT_MAX_RUNTIME_SECONDS", 300),
            max_consecutive_failures=_env_int("AGENT_MAX_CONSECUTIVE_FAILURES", 5),
            max_skills=_env_int("AGENT_MAX_SKILLS", 3),
            history_messages=_env_int("AGENT_HISTORY_MESSAGES", 20),
            verify=_env_bool("AGENT_VERIFY", True),
            auto_approve=_env_bool("AGENT_AUTO_APPROVE", False),
            default_tools=_env_list(
                "AGENT_DEFAULT_TOOLS", ["load_skill", "memory_search", "list_files"]
            ),
            tool_result_max_chars=_env_int("AGENT_TOOL_RESULT_MAX_CHARS", 12000),
            max_user_message_chars=_env_int("AGENT_MAX_USER_MESSAGE_CHARS", 20000),
            max_file_bytes=_env_int("AGENT_MAX_FILE_BYTES", 5_000_000),
            max_write_bytes=_env_int("AGENT_MAX_WRITE_BYTES", 2_000_000),
            skills_dir=Path(_env("SKILLS_DIR", "skills") or "skills"),
            workspace_dir=Path(_env("WORKSPACE_DIR", "workspace") or "workspace"),
            db_path=Path(_env("DB_PATH", "data/nimna.db") or "data/nimna.db"),
            sandbox_backend=(_env("SANDBOX_BACKEND", "subprocess") or "subprocess").lower(),
            sandbox_image=_env("SANDBOX_IMAGE", "python:3.11-slim") or "python:3.11-slim",
            sandbox_timeout=_env_int("SANDBOX_TIMEOUT", 20),
            sandbox_memory_mb=_env_int("SANDBOX_MEMORY_MB", 512),
            redis_url=_env("REDIS_URL", None),
            vision_cache_ttl=_env_int("VISION_CACHE_TTL", 600),
            host=_env("HOST", "0.0.0.0") or "0.0.0.0",
            port=_env_int("PORT", 8000),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )

    # -- helpers ---------------------------------------------------------
    @property
    def model_name(self) -> str:
        if self.provider == "gemini":
            return self.gemini_model
        if self.provider == "openai":
            return self.openai_model
        return "mock"

    def ensure_dirs(self) -> None:
        self.workspace_dir = Path(self.workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        # restrict workspace to owner only (best-effort, no-op on Windows)
        try:
            self.workspace_dir.chmod(0o700)
        except Exception:
            pass
        if str(self.db_path) != ":memory:":
            self.db_path = Path(self.db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.db_path.parent.chmod(0o700)
                if self.db_path.exists():
                    self.db_path.chmod(0o600)
            except Exception:
                pass
        # also tighten .env if it exists
        try:
            env = Path(".env")
            if env.is_file():
                env.chmod(0o600)
        except Exception:
            pass
