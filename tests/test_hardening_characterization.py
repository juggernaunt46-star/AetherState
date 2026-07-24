from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from fastapi.routing import APIRoute

from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.store import Store

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "tests/fixtures/hardening/public-routes-1.24.0.json"


def _routes() -> set[tuple[str, str]]:
    store = Store(":memory:")
    try:
        app = create_app(Config(), store=store)
        return {
            (route.path, method)
            for route in _api_routes(app.routes)
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
        }
    finally:
        store.close()


def _api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested_routes = getattr(getattr(route, "original_router", None), "routes", ())
        yield from _api_routes(nested_routes)


def test_public_124_routes_remain_reachable() -> None:
    frozen = json.loads(ROUTES.read_text(encoding="utf-8"))
    expected = {
        (row["path"], method)
        for row in frozen["routes"]
        for method in row["methods"]
    }
    assert expected
    discovered = _routes()
    assert discovered
    assert expected <= discovered


async def test_status_shape_remains_compatible(client) -> None:
    response = await client.get("/aether/status")
    assert response.status_code == 200
    value = response.json()
    required_types = {
        "name": str,
        "version": str,
        "mode": str,
        "degradation": str,
        "config_source": str,
        "upstream_configured": bool,
        "sessions": int,
        "data_dir": str,
        "telemetry": str,
    }
    assert all(type(value[key]) is kind for key, kind in required_types.items())
