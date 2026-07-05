"""Regression suite for the Venice 404 (live smoke, 2026-07-02): base URLs WITH path prefixes.

The proxy serves /v1/*; upstream.base_url includes the provider's own version segment.
Naive path joining produced /api/v1/v1/chat/completions -> 404 on Venice.
"""
from __future__ import annotations

import httpx
import pytest

from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.proxy import upstream_url
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream, Reply


@pytest.mark.parametrize("base,path,query,expected", [
    ("https://api.venice.ai/api/v1", "v1/chat/completions", "", "https://api.venice.ai/api/v1/chat/completions"),
    ("https://api.venice.ai/api/v1/", "v1/models", "", "https://api.venice.ai/api/v1/models"),
    ("https://api.openai.com/v1", "v1/chat/completions", "", "https://api.openai.com/v1/chat/completions"),
    ("http://localhost:11434/v1", "v1/models", "", "http://localhost:11434/v1/models"),
    ("https://host/api/v1", "v1", "", "https://host/api/v1"),
    ("https://host/api/v1", "chat/completions", "", "https://host/api/v1/chat/completions"),  # non-/v1 caller
    ("https://host/api/v1", "v1/models", "limit=5", "https://host/api/v1/models?limit=5"),
])
def test_upstream_url_mapping(base, path, query, expected):
    assert upstream_url(base, path, query) == expected


async def _proxy_client(base_url: str, mock: MockUpstream):
    cfg = Config()
    cfg.upstream.base_url = base_url
    upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
    app = create_app(cfg, client_factory=lambda: upstream_client, store=Store(":memory:"))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy"), upstream_client


async def test_venice_style_prefix_end_to_end():
    """ST -> proxy /v1/chat/completions -> upstream must see /api/v1/chat/completions (NOT /api/v1/v1/...)."""
    mock = MockUpstream()
    mock.enqueue(Reply(body=b'{"ok":true}'))
    client, uc = await _proxy_client("http://mock-upstream/api/v1", mock)
    resp = await client.post("/v1/chat/completions", json={"model": "glm-4.6", "messages": []})
    assert resp.status_code == 200
    assert mock.requests[0].path == "/api/v1/chat/completions"
    await client.aclose()
    await uc.aclose()


async def test_models_with_prefix():
    mock = MockUpstream()
    mock.enqueue(Reply(body=b'{"data":[]}'))
    client, uc = await _proxy_client("http://mock-upstream/api/v1", mock)
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    assert mock.requests[0].path == "/api/v1/models"
    await client.aclose()
    await uc.aclose()


async def test_unconfigured_upstream_gives_clear_502():
    mock = MockUpstream()
    client, uc = await _proxy_client("", mock)
    resp = await client.post("/v1/chat/completions", json={})
    assert resp.status_code == 502
    assert b"base_url is not configured" in resp.content
    assert mock.requests == []
    await client.aclose()
    await uc.aclose()
