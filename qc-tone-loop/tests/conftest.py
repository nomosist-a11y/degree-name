"""Shared fixtures. Every test runs against the fake unit in a throwaway home."""

import pytest

pytest.importorskip("pyquadcortex")

from qcsuite.audio import di as di_mod  # noqa: E402
from qcsuite.device import fake as fake_mod  # noqa: E402
from qcsuite.device.session import open_rig  # noqa: E402


@pytest.fixture(autouse=True)
def qchome(tmp_path, monkeypatch):
    """Keep ~/.qctone untouched: each test gets its own QCSUITE_HOME."""
    home = tmp_path / "qchome"
    monkeypatch.setenv("QCSUITE_HOME", str(home))
    monkeypatch.setenv("QCSUITE_INERT_PLUGINS", str(tmp_path / "no-inert-config.json"))
    monkeypatch.delenv("QCSUITE_READONLY", raising=False)
    monkeypatch.delenv("QCSUITE_LATENCY_MS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return home


@pytest.fixture
def fake():
    """A fast fake with the known faults switched off (tests that want them opt in)."""
    return fake_mod.FakeQuadCortex(fake_mod.FakeConfig(catalog_timeouts=0,
                                                       first_read_dropped=False))


@pytest.fixture
def rig(fake):
    with open_rig(mock=True, fake=fake) as r:
        yield r


@pytest.fixture
def device(rig):
    return rig.device


@pytest.fixture(scope="session")
def di():
    return di_mod.synth_di(4.0)


@pytest.fixture
def client(fake):
    from fastapi.testclient import TestClient
    from qcsuite.web.app import create_app
    with TestClient(create_app(mock=True, fake=fake)) as c:
        c.get("/")                      # as a browser would: the page sets the session cookie
        yield c
