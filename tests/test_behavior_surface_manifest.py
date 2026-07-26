from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/hardening/post-1.24/behavior-player-surface-manifest.json"
BASELINE_IDS = ROOT / "tests/fixtures/hardening/behavior-surface-ids-1.24.0.txt"
VALIDATOR = ROOT / "tools/validate_behavior_manifest.py"
STAGE_3_MERGE_TARGET = "55205f73c58da18a681212545c34e90ac63532a7"
MIGRATION_SURFACES = {
    "ledger.journal-replay",
    "records.claims-facts-events",
    "chat.lifecycle-retry-swipe-fork-reopen",
    "chat.living-character-continuity",
    "lessons.player",
    "lex.playerlex",
    "lex.worldlex",
    "memory.retrieval",
    "rpg.checks-and-settlement",
    "rpg.world-and-living-world",
    "rpg.hud-and-war-room",
    "console.inspection-and-privacy",
}


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_cumulative_and_bound_to_public_124() -> None:
    value = _load()
    assert value["schema"] == "aetherstate-behavior-player-surface/1"
    assert value["baseline"]["version"] == "1.24.0"
    assert value["baseline"]["commit"] == (
        "82b58277d7a1fb167434be0290d3dfd2bb3588e2"
    )
    assert value["merge_target"] == STAGE_3_MERGE_TARGET
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


def test_only_affected_surfaces_gain_stage3_migration_proof() -> None:
    value = _load()
    by_id = {entry["id"]: entry for entry in value["surfaces"]}
    assert MIGRATION_SURFACES <= by_id.keys()
    for surface_id, entry in by_id.items():
        selectors = {
            proof["selector"]
            for proof in entry["proofs"]
            if proof["kind"] == "pytest"
        }
        migration_selectors = {
            selector
            for selector in selectors
            if selector.startswith("tests/test_historical_database_upgrades.py::")
        }
        if surface_id in MIGRATION_SURFACES:
            assert migration_selectors
        else:
            assert not migration_selectors


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda value: value["baseline"].update({"version": "1.24.1"}),
            id="public-baseline-changed",
        ),
        pytest.param(
            lambda value: value.update(
                {"merge_target": value["baseline"]["commit"]}
            ),
            id="stage3-target-missing",
        ),
        pytest.param(
            lambda value: value["surfaces"].pop(0),
            id="surface-removed",
        ),
        pytest.param(
            lambda value: value["surfaces"][0].update(
                {"default_reachability": "background"}
            ),
            id="surface-narrowed",
        ),
        pytest.param(
            lambda value: value["surfaces"][0]["proofs"].pop(0),
            id="selector-removed",
        ),
        pytest.param(
            lambda value: value.update({"known_defects": ["HOLD relabeled"]}),
            id="stage2-hold-contract-relabelled",
        ),
    ],
)
def test_validator_rejects_non_cumulative_stage3_manifest_mutations(
    tmp_path: Path,
    mutation,
) -> None:
    value = copy.deepcopy(_load())
    mutation(value)
    candidate = tmp_path / "manifest.json"
    candidate.write_text(json.dumps(value), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--manifest",
            str(candidate),
            "--baseline-ids",
            str(BASELINE_IDS),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
