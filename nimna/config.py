"""Runtime configuration.

Everything is read from environment variables (optionally seeded from a `.env`
file) so that API keys never live inside the code base.
"""
from __future__ import annotations

import os
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


@dataclass
class Settings:
    """All tunables of the agent. Build with :meth:`Settings.from_env`."""

    # model provider
    provider: str = "gemini"  # gemini | openai | mock
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    openai_base_url: str = "https://integrate.api.nvidia.com/v1"
    openai_api_key: str | None = None
    openai_model: str = "meta/llama-3.3-70b-instruct"
    temperature: float = 0.2
    request_timeout: float = 120.0

    # agent behaviour
    max_steps: int = 12
    max_skills: int = 3
    history_messages: int = 20
    verify: bool = True
    auto_approve: bool = False
    default_tools: list[str] = field(
        default_factory=lambda: ["load_skill", "memory_search", "list_files"]
    )
    tool_result_max_chars: int = 12000

    # paths
    skills_dir: Path = Path("skills")
    workspace_dir: Path = Path("workspace")
    db_path: Path = Path("data/nimna.db")

    # python sandbox
    sandbox_backend: str = "subprocess"  # subprocess | docker
    sandbox_image: str = "python:3.11-slim"
    sandbox_timeout: int = 20
    sandbox_memory_mb: int = 512

    # server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        openai_key = _env("OPENAI_API_KEY") or _env("NVIDIA_API_KEY")
        return cls(
            provider=(_env("MODEL_PROVIDER", "gemini") or "gemini").lower(),
            gemini_api_key=_env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
            openai_base_url=_env("OPENAI_BASE_URL", "https://integrate.api.nvidia.com/v1")
            or "https://integrate.api.nvidia.com/v1",
            openai_api_key=openai_key,
            openai_model=_env("OPENAI_MODEL", "meta/llama-3.3-70b-instruct")
            or "meta/llama-3.3-70b-instruct",
            temperature=_env_float("MODEL_TEMPERATURE", 0.2),
            request_timeout=_env_float("MODEL_TIMEOUT", 120.0),
            max_steps=_env_int("AGENT_MAX_STEPS", 12),
            max_skills=_env_int("AGENT_MAX_SKILLS", 3),
            history_messages=_env_int("AGENT_HISTORY_MESSAGES", 20),
            verify=_env_bool("AGENT_VERIFY", True),
            auto_approve=_env_bool("AGENT_AUTO_APPROVE", False),
            default_tools=_env_list(
                "AGENT_DEFAULT_TOOLS", ["load_skill", "memory_search", "list_files"]
            ),
            tool_result_max_chars=_env_int("AGENT_TOOL_RESULT_MAX_CHARS", 12000),
            skills_dir=Path(_env("SKILLS_DIR", "skills") or "skills"),
            workspace_dir=Path(_env("WORKSPACE_DIR", "workspace") or "workspace"),
            db_path=Path(_env("DB_PATH", "data/nimna.db") or "data/nimna.db"),
            sandbox_backend=(_env("SANDBOX_BACKEND", "subprocess") or "subprocess").lower(),
            sandbox_image=_env("SANDBOX_IMAGE", "python:3.11-slim") or "python:3.11-slim",
            sandbox_timeout=_env_int("SANDBOX_TIMEOUT", 20),
            sandbox_memory_mb=_env_int("SANDBOX_MEMORY_MB", 512),
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
        if str(self.db_path) != ":memory:":
            self.db_path = Path(self.db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
