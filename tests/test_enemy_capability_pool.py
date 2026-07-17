from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from aetherstate import worldlex
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.enemy_capability_pool import (
    ENEMY_ADAPTER_CONTRACT,
    ENEMY_ADAPTER_ID,
    ENEMY_MECHANICS_SCHEMA,
    ENEMY_RECEIPT_ID,
    MECHANICS_FIELDS,
    EnemyCapabilityPoolError,
    compile_enemy_candidates,
    compile_enemy_capability_bundle,
    reconstruct_enemy_kit,
    seal_enemy_hp_receipt_admission,
    validate_enemy_capability_bundle,
    validate_enemy_candidates,
    validate_enemy_mechanics,
)
from aetherstate.enemy_kits import build_enemy_kit


ROOT = Path(__file__).resolve().parents[1]
WORLD_ID = "world_0123456789abcdef0123456789abcdef"
GENERATED_ROWS = [
    json.loads(line)
    for line in (ROOT / "corpus" / "enemy-capabilities" / "generated-kits.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
UNSUPPORTED = json.loads(
    (ROOT / "corpus" / "enemy-capabilities" / "unsupported-concepts.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _refs(kit: dict, prefix: str) -> dict[str, str]:
    return {move["id"]: f"{prefix}:{move['id']}" for move in kit["moves"]}


def _compile_full(kit: dict, subject_id: str = "enemy.fixture") -> tuple[dict, dict]:
    candidates = compile_enemy_candidates(kit, world_id=WORLD_ID)
    assignments = _refs(kit, "assignment")
    eligibility = _refs(kit, "eligibility")
    admissions = _refs(kit, "admission")
    receipt_admission = seal_enemy_hp_receipt_admission(
        admissions, authority_ref="code:test-enemy-opposition-hp"
    )
    bundle = compile_enemy_capability_bundle(
        candidates,
        subject_id=subject_id,
        assignment_refs=assignments,
        eligibility_refs=eligibility,
        receipt_admission=receipt_admission,
    )
    return candidates, bundle


def _refingerprint_kit(kit: dict) -> dict:
    result = copy.deepcopy(kit)
    result.pop("fingerprint", None)
    fingerprint = hashlib.blake2b(_canonical(result), digest_size=10).hexdigest()
    return {**result, "fingerprint": fingerprint}


def test_every_preserved_enemy_kit_round_trips_exactly_from_runtime_bundle():
    assert len(GENERATED_ROWS) == 270
    for row in GENERATED_ROWS:
        kit = row["kit"]
        candidates, bundle = _compile_full(kit, f"enemy.{row['case_id']}")

        assert candidates["schema"] == "enemy-capability-candidates/1"
        assert all(item["schema"] == "capability-definition/1" for item in bundle["definitions"])
        assert all(item["kind"] == "enemy_move" for item in bundle["definitions"])
        assert all(item["schema"] == ENEMY_MECHANICS_SCHEMA for item in bundle["mechanics"])
        assert _canonical(reconstruct_enemy_kit(bundle)) == _canonical(kit), row["case_id"]


@pytest.mark.parametrize("case", UNSUPPORTED, ids=[case["id"] for case in UNSUPPORTED])
def test_unsupported_devices_never_enter_executable_mechanics(case: dict):
    kit = build_enemy_kit("Boundary Carrier", "standard", case["armament"], {})
    unarmed = build_enemy_kit("Boundary Carrier", "standard", "", {})
    _, bundle = _compile_full(kit)
    runtime_refs = {
        member["definition"]["fingerprint"] for member in bundle["pools"]["runtime"]["members"]
    }
    executable_moves = [
        snapshot["move"]
        for snapshot in bundle["mechanics"]
        if snapshot["definition_ref"]["fingerprint"] in runtime_refs
    ]

    assert kit["moves"] == unarmed["moves"]
    assert executable_moves
    assert all(move["delivery"] != case["armament"] for move in executable_moves)


def test_same_facts_make_the_same_candidates_pools_and_bundle_fingerprint():
    kit = GENERATED_ROWS[0]["kit"]
    candidates_a = compile_enemy_candidates(kit, world_id=WORLD_ID)
    candidates_b = compile_enemy_candidates(copy.deepcopy(kit), world_id=WORLD_ID)
    assignments = _refs(kit, "assignment")
    eligibility = _refs(kit, "eligibility")
    admissions = _refs(kit, "admission")
    def reverse(mapping: dict[str, str]) -> dict[str, str]:
        return dict(reversed(list(mapping.items())))

    bundle_a = compile_enemy_capability_bundle(
        candidates_a,
        subject_id="enemy.deterministic",
        assignment_refs=assignments,
        eligibility_refs=eligibility,
        receipt_admission=seal_enemy_hp_receipt_admission(
            admissions, authority_ref="code:test-enemy-opposition-hp"
        ),
    )
    bundle_b = compile_enemy_capability_bundle(
        candidates_b,
        subject_id="enemy.deterministic",
        assignment_refs=reverse(assignments),
        eligibility_refs=reverse(eligibility),
        receipt_admission=seal_enemy_hp_receipt_admission(
            reverse(admissions), authority_ref="code:test-enemy-opposition-hp"
        ),
    )

    assert candidates_a["fingerprint"] == candidates_b["fingerprint"]
    assert bundle_a == bundle_b
    assert bundle_a["fingerprint"] == bundle_b["fingerprint"]


def test_pool_stages_can_narrow_but_never_add_an_absent_definition():
    kit = next(row["kit"] for row in GENERATED_ROWS if len(row["kit"]["moves"]) == 4)
    candidates = compile_enemy_candidates(kit, world_id=WORLD_ID)
    ids = [move["id"] for move in kit["moves"]]
    assignments = {move_id: f"assignment:{move_id}" for move_id in ids[:3]}
    eligibility = {move_id: f"eligibility:{move_id}" for move_id in ids[:2]}
    admissions = {ids[0]: f"admission:{ids[0]}"}
    bundle = compile_enemy_capability_bundle(
        candidates,
        subject_id="enemy.narrowing",
        assignment_refs=assignments,
        eligibility_refs=eligibility,
        receipt_admission=seal_enemy_hp_receipt_admission(
            admissions, authority_ref="code:test-enemy-opposition-hp"
        ),
    )

    assert [len(bundle["pools"][stage]["members"]) for stage in (
        "world_library", "assigned", "spawn_eligible", "runtime"
    )] == [4, 3, 2, 1]
    with pytest.raises(EnemyCapabilityPoolError, match="absent"):
        compile_enemy_capability_bundle(
            candidates,
            subject_id="enemy.forged",
            assignment_refs={**assignments, "invented_move": "assignment:forged"},
            eligibility_refs=eligibility,
            receipt_admission=seal_enemy_hp_receipt_admission(
                admissions, authority_ref="code:test-enemy-opposition-hp"
            ),
        )


def test_role_and_tier_metadata_never_mint_a_missing_definition():
    original = next(row["kit"] for row in GENERATED_ROWS if len(row["kit"]["moves"]) == 4)
    removed_id = original["moves"][-1]["id"]
    reduced = copy.deepcopy(original)
    reduced["tier"] = "boss"
    reduced["role_axis"] = "artillery"
    reduced["moves"] = reduced["moves"][:-1]
    reduced = _refingerprint_kit(reduced)

    candidates, bundle = _compile_full(reduced, "enemy.metadata-does-not-grant")
    compiled_ids = {snapshot["move"]["id"] for snapshot in candidates["mechanics"]}

    assert removed_id not in compiled_ids
    assert compiled_ids == {move["id"] for move in reduced["moves"]}
    assert len(bundle["definitions"]) == len(reduced["moves"])


def test_forged_mechanics_definition_ref_and_receipt_admission_fail_closed():
    kit = GENERATED_ROWS[0]["kit"]
    candidates, bundle = _compile_full(kit)

    mechanics = copy.deepcopy(candidates["mechanics"][0])
    mechanics["move"]["damage"] += 1
    with pytest.raises(EnemyCapabilityPoolError, match="fingerprint"):
        validate_enemy_mechanics(mechanics)

    rehashed_mechanics = copy.deepcopy(candidates)
    snapshot = rehashed_mechanics["mechanics"][0]
    snapshot["move"]["damage"] += 1
    snapshot["fingerprint"] = content_fingerprint({
        key: value for key, value in snapshot.items() if key != "fingerprint"
    })
    rehashed_mechanics["fingerprint"] = content_fingerprint({
        key: value for key, value in rehashed_mechanics.items() if key != "fingerprint"
    })
    with pytest.raises(EnemyCapabilityPoolError, match="reconstruct"):
        validate_enemy_candidates(rehashed_mechanics)

    forged_ref = copy.deepcopy(candidates)
    forged_ref["mechanics"][0]["definition_ref"]["fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises((EnemyCapabilityPoolError, ValueError)):
        validate_enemy_candidates(forged_ref)

    forged_admission = copy.deepcopy(bundle["receipt_admission"])
    first = next(iter(forged_admission["refs"]))
    forged_admission["refs"][first] = "admission:forged"
    with pytest.raises(EnemyCapabilityPoolError, match="fingerprint"):
        compile_enemy_capability_bundle(
            candidates,
            subject_id="enemy.forged-admission",
            assignment_refs=_refs(kit, "assignment"),
            eligibility_refs=_refs(kit, "eligibility"),
            receipt_admission=forged_admission,
        )

    forged_bundle = copy.deepcopy(bundle)
    forged_bundle["pools"]["runtime"]["members"][0]["admission_ref"] = "admission:forged"
    with pytest.raises((EnemyCapabilityPoolError, ValueError)):
        validate_enemy_capability_bundle(forged_bundle)


def test_adapter_snapshot_covers_every_enemy_hp_runtime_field_and_receipt_is_distinct():
    assert ENEMY_ADAPTER_ID != ENEMY_RECEIPT_ID
    assert ENEMY_ADAPTER_CONTRACT.adapter_id == ENEMY_ADAPTER_ID
    assert ENEMY_ADAPTER_CONTRACT.receipt_id == ENEMY_RECEIPT_ID
    assert ENEMY_ADAPTER_CONTRACT.consumed_fields == MECHANICS_FIELDS
    _, bundle = _compile_full(GENERATED_ROWS[1]["kit"])
    assert all(set(snapshot["move"]) == set(MECHANICS_FIELDS) for snapshot in bundle["mechanics"])


def test_worldlex_core_remains_domain_neutral_and_contains_no_enemy_combat_contract():
    source = inspect.getsource(worldlex)

    assert "enemy-kit/1" not in source
    assert "enemy-hp-move-adapter/1" not in source
    assert "enemy-opposition-hp/1" not in source
    assert "kit_size" not in source
    assert "pending_intent" not in source
