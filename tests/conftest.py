import shutil
from pathlib import Path

import pytest

from nimna.config import Settings
from nimna.core.agent import Agent
from nimna.core.approval import DeferToClient
from nimna.memory import MemoryStore
from nimna.providers import MockProvider
from nimna.skills import SkillManager
from nimna.tools import default_registry

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    shutil.copy(REPO / "workspace" / "sales.csv", ws / "sales.csv")
    (ws / "notes.txt").write_text("hello\nworld\n", encoding="utf-8")
    return ws


@pytest.fixture
def settings(workspace: Path) -> Settings:
    s = Settings.from_env(env_file=None)
    s.provider = "mock"
    s.verify = False
    s.workspace_dir = workspace
    s.skills_dir = REPO / "skills"
    s.db_path = Path(":memory:")
    return s


@pytest.fixture
def skills() -> SkillManager:
    return SkillManager(REPO / "skills")


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def agent(settings: Settings, skills: SkillManager, provider: MockProvider) -> Agent:
    return Agent(provider, skills, default_registry(), MemoryStore(":memory:"), settings,
                 approval_policy=DeferToClient(), workspace=settings.workspace_dir)
