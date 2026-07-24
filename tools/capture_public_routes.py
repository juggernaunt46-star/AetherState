from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from fastapi.routing import APIRoute

from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.store import Store

SCHEMA = "aetherstate-public-routes/1"
BASELINE_VERSION = "1.24.0"
BASELINE_COMMIT = "82b58277d7a1fb167434be0290d3dfd2bb3588e2"
IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})


def _api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested_routes = getattr(getattr(route, "original_router", None), "routes", ())
        yield from _api_routes(nested_routes)


def capture_routes() -> list[dict[str, object]]:
    store = Store(":memory:")
    try:
        app = create_app(Config(), store=store)
        routes = [
            {
                "path": route.path,
                "methods": sorted(set(route.methods) - IMPLICIT_METHODS),
            }
            for route in _api_routes(app.routes)
        ]
    finally:
        store.close()
    return sorted(routes, key=lambda route: (route["path"], route["methods"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    output = parse_args().output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "baseline": {
                    "version": BASELINE_VERSION,
                    "commit": BASELINE_COMMIT,
                },
                "routes": capture_routes(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
