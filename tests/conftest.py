import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.bridge.hub import BridgeHub  # noqa: E402
from agent.bridge.protocol import BridgePaths  # noqa: E402
from agent.storage.db import Database  # noqa: E402
from tools.fake_ea import FakeEA  # noqa: E402

KEY = "test-key"


@pytest.fixture
async def db():
    d = await Database(":memory:").open()
    yield d
    await d.close()


@pytest.fixture
def paths(tmp_path):
    p = BridgePaths(tmp_path / "bridge", "12345678_777001")
    p.ensure()
    return p


@pytest.fixture
def ea(paths):
    e = FakeEA(paths, KEY)
    e.start()
    return e


@pytest.fixture
async def hub(paths, db):
    return BridgeHub(paths, db, KEY)
