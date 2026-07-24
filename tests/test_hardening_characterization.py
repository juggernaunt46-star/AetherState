from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.store import Store
from tools import capture_public_routes

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


@pytest.mark.parametrize(
    ("captured", "message"),
    [
        ([], "no APIRoute entries"),
        ([{"path": "/aether/status", "methods": []}], "no path/method obligations"),
    ],
    ids=["empty-discovery", "empty-retained-obligations"],
)
def test_capture_rejects_empty_contract_before_overwriting_output(
    monkeypatch, tmp_path, captured, message
) -> None:
    output = tmp_path / "public-routes.json"
    output.write_text("preserve this fixture", encoding="utf-8")
    monkeypatch.setattr(capture_public_routes, "capture_routes", lambda: captured)
    monkeypatch.setattr(
        capture_public_routes,
        "parse_args",
        lambda: argparse.Namespace(output=output),
    )

    with pytest.raises(ValueError, match=message):
        capture_public_routes.main()

    assert output.read_text(encoding="utf-8") == "preserve this fixture"


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
