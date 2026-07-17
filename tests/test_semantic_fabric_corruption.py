"""Independent resealed-corpus gates for code-consumed Semantic Fabric fields."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from aetherstate.semantic_fabric import SemanticFabric, SemanticFabricError
from tools.build_semantic_fabric import build as build_semantic_fabric


ROOT = Path(__file__).resolve().parents[1]


def _copy_corpora(tmp_path: Path) -> Path:
    copied_root = tmp_path / "repo"
    corpus = copied_root / "corpus"
    corpus.mkdir(parents=True)
    shutil.copytree(ROOT / "corpus" / "semantic-fabric", corpus / "semantic-fabric")
    shutil.copytree(ROOT / "corpus" / "capability-glossary", corpus / "capability-glossary")
    return copied_root


def _mutate_action_inspect(copied_root: Path, mutate) -> None:
    source_path = copied_root / "corpus" / "semantic-fabric" / "source" / "base.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    entries = source["packs"]["action"]["entries"]
    inspect = next(row for row in entries if row["concept_id"] == "action.inspect")
    mutate(inspect)
    source_path.write_text(
        json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def test_resealed_action_inspect_cannot_become_weapon_attack(tmp_path: Path) -> None:
    copied_root = _copy_corpora(tmp_path)
    _mutate_action_inspect(
        copied_root,
        lambda entry: entry["features"].update(action_class="weapon_attack"),
    )

    with pytest.raises(SemanticFabricError, match="consumed ActionLex action_class contract"):
        build_semantic_fabric(copied_root)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("authorized", True),
        ("executable", True),
        ("mechanic_scope_allowed", True),
        ("receipt_id", "enemy-opposition-hp/1"),
        ("settlement_op", "combatant_hp"),
        ("damage_amount", 2),
    ],
)
def test_resealed_action_features_cannot_self_assert_authority_or_mechanics(
    tmp_path: Path,
    key: str,
    value: Any,
) -> None:
    copied_root = _copy_corpora(tmp_path)
    _mutate_action_inspect(
        copied_root,
        lambda entry: entry["features"].update({key: value}),
    )

    with pytest.raises(SemanticFabricError, match="authority/mechanic feature keys"):
        build_semantic_fabric(copied_root)


def test_recognition_surface_extension_preserves_action_contract(tmp_path: Path) -> None:
    copied_root = _copy_corpora(tmp_path)
    _mutate_action_inspect(
        copied_root,
        lambda entry: entry["terms"].append("resonance audit"),
    )

    build_semantic_fabric(copied_root)
    fabric = SemanticFabric.load(copied_root / "corpus" / "semantic-fabric")
    matches = [
        match
        for match in fabric.translate("I resonance audit the polehammer.", lex_ids=("action",)).matches
        if match.concept_id == "action.inspect"
    ]

    assert len(matches) == 1
    assert matches[0].features["action_class"] == "inspection"
    payload = matches[0].as_dict()
    assert payload["authorized"] is False
    assert payload["executable"] is False
