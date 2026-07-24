from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs/hardening/post-1.24/architecture-baseline.json"
BASELINE = "82b58277d7a1fb167434be0290d3dfd2bb3588e2"


def test_architecture_baseline_is_diagnostic_and_public_bound() -> None:
    value = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert value["schema"] == "aetherstate-architecture-baseline/1"
    assert value["source"]["commit"] == BASELINE
    assert value["thresholds"] == {}
    assert set(value["central_modules"]) == {
        "src/aetherstate/state.py",
        "src/aetherstate/pipeline.py",
        "src/aetherstate/control.py",
        "src/aetherstate/store.py",
    }
    assert value["metrics"]["python_modules"] > 0
    assert value["metrics"]["python_physical_lines"] > 0
    assert value["metrics"]["broad_exception_handlers"] > 0
    assert value["metrics"]["route_decorators"] > 0
    assert value["metrics"]["state_dispatch_branches"] > 0
