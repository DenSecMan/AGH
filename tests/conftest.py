import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    """Keep the user's real ~/.agh/.env keys out of every test."""
    monkeypatch.setenv("AGH_HOME", str(tmp_path_factory.mktemp("agh-home")))

from agh import release
from agh.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures"


def add_fixture(ws: Workspace, kind: str, name: str) -> Path:
    """Put a test-only element (tests/fixtures/<kind>/<name>) into the workspace, recorded as released: the
    starter ships no toy tool or demo connector, and the tests need something harmless to call."""
    target = ws.dir(kind) / name
    shutil.copytree(FIXTURES / kind / name, target)
    release.record(ws, target)
    return target


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.delenv("AGH_WORKSPACE", raising=False)
    monkeypatch.setenv("AGH_ALLOW_MOCK_EVALS", "1")  # no real model in tests; production refuses mock cases
    return Workspace.init(tmp_path / "ws")


def with_toys(workspace: Workspace) -> Workspace:
    """A module that needs the calculator tool and the open_meteo connector overrides `ws` with this."""
    add_fixture(workspace, "tools", "calculator")
    add_fixture(workspace, "connectors", "open_meteo")
    return workspace
