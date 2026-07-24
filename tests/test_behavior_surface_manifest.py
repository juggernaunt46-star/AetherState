from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/hardening/post-1.24/behavior-player-surface-manifest.json"
BASELINE_IDS = ROOT / "tests/fixtures/hardening/behavior-surface-ids-1.24.0.txt"


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_cumulative_and_bound_to_public_124() -> None:
    value = _load()
    assert value["schema"] == "aetherstate-behavior-player-surface/1"
    assert value["baseline"]["version"] == "1.24.0"
    assert value["baseline"]["commit"] == (
        "82b58277d7a1fb167434be0290d3dfd2bb3588e2"
    )
    assert value["merge_target"] == value["baseline"]["commit"]
    assert value["cumulative"] is True
    ids = [entry["id"] for entry in value["surfaces"]]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))


def test_public_124_surface_ids_can_only_be_preserved_or_extended() -> None:
    current = {entry["id"] for entry in _load()["surfaces"]}
    baseline = {
        line.strip()
        for line in BASELINE_IDS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert baseline <= current


def test_every_surface_has_resolvable_automated_proof() -> None:
    for entry in _load()["surfaces"]:
        assert entry["preservation"] == "required"
        assert entry["default_reachability"] in {"direct", "configured", "background"}
        assert entry["proofs"]
        for proof in entry["proofs"]:
            assert proof["kind"] in {"pytest", "node", "workflow"}
            if proof["kind"] == "pytest":
                assert "::test_" in proof["selector"]
                assert (ROOT / proof["selector"].split("::", 1)[0]).is_file()
            elif proof["kind"] == "node":
                assert proof["command"][:1] == ["node"]
                assert (ROOT / proof["command"][1]).is_file()
            else:
                assert proof["gate"]
