from __future__ import annotations

import httpx
import pytest

from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream


@pytest.fixture()
def mock_upstream() -> MockUpstream:
    return MockUpstream()


@pytest.fixture()
def cfg(tmp_path) -> Config:
    c = Config()
    c.upstream.base_url = "http://mock-upstream/v1"   # convention: base INCLUDES version segment
    # 2026-07-06: route tests that persist (connection/extraction/specialization) used to
    # write ./aetherstate-data/config.toml — running the suite from the repo root CLOBBERED
    # the developer's real config with mock values. Persist into the pytest tmp dir instead.
    c.server.data_dir = str(tmp_path)
    return c


@pytest.fixture()
async def proxy_app(mock_upstream: MockUpstream, cfg: Config):
    """The app object itself — tests reach app.state.jobs for drain() (P3 harness)."""
    upstream_transport = httpx.ASGITransport(app=mock_upstream)
    upstream_client = httpx.AsyncClient(transport=upstream_transport, base_url="http://mock-upstream")
    app = create_app(cfg, client_factory=lambda: upstream_client, store=Store(":memory:"))
    yield app
    await app.state.jobs.stop()
    await upstream_client.aclose()


@pytest.fixture()
async def client(proxy_app):
    """AetherState proxy wired to MockUpstream — fully in-process, no sockets (11 SS1)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy_app),
                                 base_url="http://proxy") as c:
        yield c
