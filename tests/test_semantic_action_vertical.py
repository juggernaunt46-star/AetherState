"""RED contract for one canonical sentence interpretation at the Tier0 boundary.

These tests intentionally exercise the production Tier0 caller.  They do not permit combat
opening, check resolution, or settlement to recover identity by reparsing the sentence: one
recognition-only meaning receipt must be committed before the versioned ActionFrame, and every
sentence-derived rule operation must refer to that exact contextual snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
import random

import pytest

from aetherstate import compose, tier0
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.semantic import ACTION_FRAME_SCHEMA
from aetherstate.semantic_binding import (
    validate_meaning_binding,
    validate_world_alignment,
)
from aetherstate.state import apply_delta, current_state, empty_state
from aetherstate.store import Store
from aetherstate.worldlex import CONTEXT_FRAME_SCHEMA, ContextFrame


MEANING_RECEIPT_SCHEMA = "semantic-fabric-meaning-receipt/1"

ROOT_V_SENTENCE = (
    "I use Rope-Dart Meridian Pierce to send the steel dart around Iven's polehammer shaft, "
    "wrench his guard off line, and drive the weighted tail into his ribs."
)

MOONBLADE_SENTENCE = (
    "I use Rope-Dart Meridian Pierce to whip the steel dart around Nera's moonblade hilt and "
    "drive the weighted tail into her sternum."
)

MIRROR_SHIELD_SENTENCE = (
    "I use Sunspoke Chain-Reversal to whip the starwire around Vosk's mirror shield rim, "
    "tear it aside, and drive the weighted sun-hook into his chest."
)

INSPECTION_SENTENCE = (
    "I use Kiln-Song Vibrometry to inspect Iven's polehammer shaft for hidden stress fractures."
)

NEGATED_ROOT_V_SENTENCE = ROOT_V_SENTENCE.replace("I use", "I do not use", 1)

AMBIGUOUS_OWNER_SENTENCE = (
    "I use Rope-Dart Meridian Pierce to send the steel dart around Iven's polehammer shaft and "
    "drive the weighted tail into his ribs."
)

IVEN = {
    "eid": "toll_marshal_iven",
    "kind": "npc",
    "name": "Toll-Marshal Iven",
    "present": True,
    "aliases": ["Iven"],
    "role": "An opening polehammer-and-buckler warden.",
}

NERA = {
    "eid": "glass_warden_nera",
    "kind": "npc",
    "name": "Glass Warden Nera",
    "present": True,
    "aliases": ["Nera"],
    "role": "A glass warden carrying a moonblade and buckler.",
}

VOSK = {
    "eid": "gate_harrier_vosk",
    "kind": "npc",
    "name": "Gate-Harrier Vosk",
    "present": True,
    "aliases": ["Vosk"],
    "role": "A hostile forked-spear and mirror-shield gate warden.",
}

NORTH_IVEN = {
    "eid": "north_marshal_iven",
    "kind": "npc",
    "name": "North Marshal Iven",
    "present": True,
    "aliases": [],
    "role": "A northern marshal carrying a polehammer.",
}

SOUTH_IVEN = {
    "eid": "south_marshal_iven",
    "kind": "npc",
    "name": "South Marshal Iven",
    "present": True,
    "aliases": [],
    "role": "A southern marshal carrying a polehammer.",
}


def _rpg_cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.intent_floor = True
    cfg.specialization.foe_floor = True
    cfg.specialization.war_room = True
    return cfg


def _state(*people: Mapping[str, object]) -> dict:
    state = empty_state()
    state["entities"]["sava_orr"] = {
        "kind": "player",
        "name": "Sava Orr",
        "present": True,
        "aliases": [],
    }
    for person in people:
        row = dict(person)
        eid = str(row.pop("eid"))
        state["entities"][eid] = row
    state["player"] = {
        "sava_orr": {
            "eid": "sava_orr",
            "stats": {"DEX": 16, "CUN": 14, "STR": 12},
            "skills": {
                "rope_dart_meridian_pierce": 4,
                "kiln_song_vibrometry": 3,
            },
            "abilities": [],
            "resources": {
                "spoolcharge": {"name": "Spoolcharge", "max": 300, "cur": 260},
            },
            "_resource_cost_policy": "strict/1",
            "defs": {
                "skills": {
                    "rope_dart_meridian_pierce": {
                        "name": "Rope-Dart Meridian Pierce",
                        "keyed_stat": "DEX",
                        "governs": ["send", "wrench", "drive", "whip"],
                        "cost": {"spoolcharge": 6},
                    },
                    "kiln_song_vibrometry": {
                        "name": "Kiln-Song Vibrometry",
                        "keyed_stat": "CUN",
                        "governs": ["inspect", "study", "read resonance"],
                        "cost": {"spoolcharge": 4},
                    },
                },
            },
        },
    }
    return state


def _run(source_text: str, state: dict, *, assistant: str = ""):
    messages = []
    if assistant:
        messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": source_text})
    return tier0.run(
        {"messages": messages},
        "new_turn",
        False,
        state,
        _rpg_cfg(),
        random.Random(7),
    )


def _committed_semantics(result, source_text: str) -> tuple[dict, list[dict]]:
    meaning_commits = [
        (index, op) for index, op in enumerate(result.rule_ops)
        if op.get("op") == "semantic_meaning_commit"
    ]
    frame_commits = [
        (index, op) for index, op in enumerate(result.rule_ops)
        if op.get("op") == "semantic_frame_commit"
    ]
    binding_commits = [
        (index, op) for index, op in enumerate(result.rule_ops)
        if op.get("op") == "semantic_binding_commit"
    ]
    alignment_commits = [
        (index, op) for index, op in enumerate(result.rule_ops)
        if op.get("op") == "semantic_world_alignment_commit"
    ]
    assert len(meaning_commits) == 1, (
        "expected one semantic_meaning_commit, got "
        f"{len(meaning_commits)}; rule ops={[op.get('op') for op in result.rule_ops]}"
    )
    assert frame_commits, (
        "expected at least one semantic_frame_commit; rule ops="
        f"{[op.get('op') for op in result.rule_ops]}"
    )
    meaning_index, meaning_op = meaning_commits[0]
    assert meaning_index < min(index for index, _op in frame_commits)
    meaning = meaning_op.get("meaning")
    assert isinstance(meaning, dict)
    assert meaning["schema"] == MEANING_RECEIPT_SCHEMA
    meaning_payload = {
        key: value for key, value in meaning.items() if key != "fingerprint"
    }
    assert meaning["fingerprint"] == content_fingerprint(meaning_payload)
    assert meaning["source_fingerprint"] == content_fingerprint(source_text)
    assert isinstance(meaning["matches"], list)
    assert all(
        "matched_phrase" not in match
        and match["recognized"] is True
        and match["authorized"] is False
        and match["executable"] is False
        for match in meaning["matches"]
    )

    assert len(binding_commits) == len(frame_commits)
    assert max(index for index, _op in binding_commits) \
        < min(index for index, _op in frame_commits)
    if alignment_commits:
        assert max(index for index, _op in binding_commits) \
            < min(index for index, _op in alignment_commits)
        assert max(index for index, _op in alignment_commits) \
            < min(index for index, _op in frame_commits)
    bindings = {
        commit["binding"]["fingerprint"]: validate_meaning_binding(
            commit["binding"], meaning_receipt=meaning,
        )
        for _index, commit in binding_commits
    }
    alignments = {
        commit["alignment"]["fingerprint"]: validate_world_alignment(
            commit["alignment"]
        )
        for _index, commit in alignment_commits
    }

    frames = []
    for _index, commit in frame_commits:
        frame = commit.get("frame")
        assert isinstance(frame, dict)
        assert frame["schema"] == ACTION_FRAME_SCHEMA
        fingerprint = frame["fingerprint"]
        payload = {key: value for key, value in frame.items() if key != "fingerprint"}
        assert fingerprint == content_fingerprint(payload)
        assert frame["meaning_ref"] == meaning["fingerprint"]
        assert frame["fabric_fingerprint"] == meaning["fabric_fingerprint"]
        binding = bindings[frame["meaning_binding_ref"]]
        assert frame["event_node_id"] == binding["event_node_id"]
        assert binding["meaning_ref"] == meaning["fingerprint"]
        assert set(frame["world_alignment_refs"]) <= set(alignments)
        assert all(
            alignments[ref]["recognition_ref"] == frame["meaning_binding_ref"]
            for ref in frame["world_alignment_refs"]
        )

        context = ContextFrame.from_dict(frame["context_frame"])
        assert frame["context_frame"]["schema"] == CONTEXT_FRAME_SCHEMA
        assert context.frame_id == frame["frame_id"]
        assert context.source_fingerprint == meaning["source_fingerprint"]
        assert context.genre_ids == tuple(meaning["genre_ids"])
        assert 0 <= context.span_start < context.span_end <= len(source_text)
        assert context.polarity == frame["polarity"]
        assert context.modality == frame["modality"]
        assert context.time_scope == frame["time_scope"]
        assert context.quoted is False
        for row in frame["evidence"]:
            assert context.span_start <= row["start"] < row["end"] <= context.span_end
        frames.append(frame)
    return meaning, frames


def _committed_frame(result, source_text: str) -> tuple[dict, str]:
    _meaning, frames = _committed_semantics(result, source_text)
    assert len(frames) == 1, f"expected one frame, got {[f['frame_id'] for f in frames]}"
    return frames[0], str(frames[0]["fingerprint"])


def _assert_source_evidence(frame: dict, source_text: str, *phrases: str) -> None:
    evidence = frame["evidence"]
    assert isinstance(evidence, list) and evidence
    spans = set()
    for row in evidence:
        assert isinstance(row, Mapping)
        start = row.get("start", row.get("span_start"))
        end = row.get("end", row.get("span_end"))
        assert isinstance(start, int) and isinstance(end, int)
        assert 0 <= start < end <= len(source_text)
        spans.add((start, end))
    for phrase in phrases:
        start = source_text.index(phrase)
        assert (start, start + len(phrase)) in spans, (
            f"missing exact evidence span for {phrase!r}: {sorted(spans)}"
        )


def _assert_frame(
    frame: dict,
    source_text: str,
    *,
    action_class: str,
    target_entity_id: str | None,
    target_name: str | None,
    possessed_object: str,
    linguistic_possessor_id: str | None,
    possessed_object_instance_id: str | None,
    possessed_object_owner_id: str | None,
    possessed_object_part: str,
    target_locus: str,
    target_locus_owner_id: str | None,
    polarity: str = "positive",
    ambiguity: set[str] | None = None,
    evidence_phrases: tuple[str, ...],
    actor_id: str = "sava_orr",
    capability_id: str | None = None,
) -> None:
    required = {
        "actor_id",
        "capability_id",
        "action_class",
        "target_entity_id",
        "target_name",
        "possessed_object",
        "linguistic_possessor_id",
        "possessed_object_instance_id",
        "possessed_object_owner_id",
        "possessed_object_part",
        "target_locus",
        "target_locus_owner_id",
        "polarity",
        "modality",
        "time_scope",
        "evidence",
        "ambiguity",
    }
    assert required <= set(frame)
    assert frame["actor_id"] == actor_id
    expected_capability = capability_id or (
        "rope_dart_meridian_pierce"
        if action_class == "weapon_attack"
        else "kiln_song_vibrometry"
    )
    assert frame["capability_id"] == expected_capability
    assert frame["action_class"] == action_class
    assert frame["target_entity_id"] == target_entity_id
    assert frame["target_name"] == target_name
    assert frame["possessed_object"] == possessed_object
    assert frame["linguistic_possessor_id"] == linguistic_possessor_id
    assert frame["possessed_object_instance_id"] == possessed_object_instance_id
    assert frame["possessed_object_owner_id"] == possessed_object_owner_id
    assert frame["possessed_object_part"] == possessed_object_part
    assert frame["target_locus"] == target_locus
    assert frame["target_locus_owner_id"] == target_locus_owner_id
    assert frame["polarity"] == polarity
    assert frame["modality"] == "actual"
    assert frame["time_scope"] == "current"
    assert set(frame["ambiguity"]) == (ambiguity or set())
    _assert_source_evidence(frame, source_text, *evidence_phrases)


def _assert_all_sentence_ops_refer_to(result, fingerprint: str | set[str]) -> None:
    # Turn-level time passage is produced for every narrative turn and does not interpret this
    # sentence. Meaning/frame commits establish evidence and are not their own descendants.
    allowed = {fingerprint} if isinstance(fingerprint, str) else set(fingerprint)
    derived = [
        op for op in result.rule_ops
        if op.get("op") not in {
            "semantic_meaning_commit", "semantic_binding_commit",
            "semantic_world_alignment_commit", "semantic_frame_commit",
            "clock_tick", "time_advance",
        }
    ]
    assert derived
    for op in derived:
        assert op.get("_semantic_frame_ref") in allowed, op
    for op in result.rule_ops:
        if op.get("op") in {
            "semantic_meaning_commit", "semantic_binding_commit",
            "semantic_world_alignment_commit", "semantic_frame_commit",
            "clock_tick", "time_advance", "stagnation",
        }:
            assert "_semantic_frame_ref" not in op, op


def _ops(result, name: str) -> list[dict]:
    return [op for op in result.rule_ops if op.get("op") == name]


def test_root_v_exact_sentence_commits_one_frame_used_by_every_tier0_mechanic():
    result = _run(
        ROOT_V_SENTENCE,
        _state(IVEN),
        assistant="Toll-Marshal Iven braces behind his polehammer and buckler.",
    )

    frame, fingerprint = _committed_frame(result, ROOT_V_SENTENCE)
    _assert_frame(
        frame,
        ROOT_V_SENTENCE,
        action_class="weapon_attack",
        target_entity_id="toll_marshal_iven",
        target_name="Toll-Marshal Iven",
        possessed_object="polehammer",
        linguistic_possessor_id="toll_marshal_iven",
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="shaft",
        target_locus="ribs",
        target_locus_owner_id="toll_marshal_iven",
        evidence_phrases=(
            "I",
            "Rope-Dart Meridian Pierce",
            "Iven",
            "polehammer",
            "shaft",
            "ribs",
        ),
    )

    checks = _ops(result, "check")
    assert len(checks) == 1
    assert checks[0]["skill"] == "rope_dart_meridian_pierce"
    assert checks[0]["char"] == "sava_orr"
    assert checks[0]["_cost"] == {"spoolcharge": 6}
    assert [(op["char"], op["_cid"]) for op in _ops(result, "combatant_spawn")] == [
        ("toll_marshal_iven", "toll_marshal_iven"),
    ]
    assert [op["target"] for op in _ops(result, "combatant_hp")] == ["toll_marshal_iven"]
    _assert_all_sentence_ops_refer_to(result, fingerprint)


def test_productive_blade_compound_uses_the_same_frame_contract_without_a_token_exception():
    result = _run(
        MOONBLADE_SENTENCE,
        _state(NERA),
        assistant="Glass Warden Nera angles her moonblade across the narrow passage.",
    )

    frame, fingerprint = _committed_frame(result, MOONBLADE_SENTENCE)
    _assert_frame(
        frame,
        MOONBLADE_SENTENCE,
        action_class="weapon_attack",
        target_entity_id="glass_warden_nera",
        target_name="Glass Warden Nera",
        possessed_object="moonblade",
        linguistic_possessor_id="glass_warden_nera",
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="hilt",
        target_locus="sternum",
        target_locus_owner_id="glass_warden_nera",
        evidence_phrases=(
            "Rope-Dart Meridian Pierce",
            "Nera",
            "moonblade",
            "hilt",
            "sternum",
        ),
    )
    assert [(op["char"], op["_cid"]) for op in _ops(result, "combatant_spawn")] == [
        ("glass_warden_nera", "glass_warden_nera"),
    ]
    assert [op["target"] for op in _ops(result, "combatant_hp")] == ["glass_warden_nera"]
    _assert_all_sentence_ops_refer_to(result, fingerprint)


def test_spaced_defensive_gear_compound_binds_owner_object_part_and_locus():
    state = _state(VOSK)
    player = state["player"]["sava_orr"]
    player["skills"]["sunspoke_chain_reversal"] = 4
    player["defs"]["skills"]["sunspoke_chain_reversal"] = {
        "name": "Sunspoke Chain-Reversal",
        "keyed_stat": "DEX",
        "governs": ["whip", "tear", "drive"],
        "cost": {"spoolcharge": 5},
    }
    result = _run(
        MIRROR_SHIELD_SENTENCE,
        state,
        assistant="Gate-Harrier Vosk raises his mirror shield behind a forked spear.",
    )

    frame, fingerprint = _committed_frame(result, MIRROR_SHIELD_SENTENCE)
    _assert_frame(
        frame,
        MIRROR_SHIELD_SENTENCE,
        action_class="weapon_attack",
        target_entity_id="gate_harrier_vosk",
        target_name="Gate-Harrier Vosk",
        possessed_object="mirror shield",
        linguistic_possessor_id="gate_harrier_vosk",
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="rim",
        target_locus="chest",
        target_locus_owner_id="gate_harrier_vosk",
        evidence_phrases=(
            "Sunspoke Chain-Reversal",
            "Vosk",
            "mirror shield",
            "rim",
            "chest",
        ),
        capability_id="sunspoke_chain_reversal",
    )
    checks = _ops(result, "check")
    assert len(checks) == 1
    assert checks[0]["skill"] == "sunspoke_chain_reversal"
    assert checks[0]["_cost"] == {"spoolcharge": 5}
    assert [(op["char"], op["_cid"]) for op in _ops(result, "combatant_spawn")] == [
        ("gate_harrier_vosk", "gate_harrier_vosk"),
    ]
    assert [op["target"] for op in _ops(result, "combatant_hp")] == [
        "gate_harrier_vosk",
    ]
    _assert_all_sentence_ops_refer_to(result, fingerprint)


def test_noncombat_possessed_object_inspection_pays_its_check_without_opening_combat():
    result = _run(
        INSPECTION_SENTENCE,
        _state(IVEN),
        assistant="Toll-Marshal Iven presents the polehammer for inspection.",
    )

    frame, fingerprint = _committed_frame(result, INSPECTION_SENTENCE)
    _assert_frame(
        frame,
        INSPECTION_SENTENCE,
        action_class="inspection",
        target_entity_id="toll_marshal_iven",
        target_name="Toll-Marshal Iven",
        possessed_object="polehammer",
        linguistic_possessor_id="toll_marshal_iven",
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="shaft",
        target_locus="",
        target_locus_owner_id=None,
        evidence_phrases=(
            "Kiln-Song Vibrometry",
            "Iven",
            "polehammer",
            "shaft",
        ),
    )
    checks = _ops(result, "check")
    assert len(checks) == 1
    assert checks[0]["skill"] == "kiln_song_vibrometry"
    assert checks[0]["_cost"] == {"spoolcharge": 4}
    assert not _ops(result, "combatant_spawn")
    assert not _ops(result, "combatant_hp")
    _assert_all_sentence_ops_refer_to(result, fingerprint)


def test_negated_attack_commits_negative_identity_but_executes_no_mechanic():
    result = _run(
        NEGATED_ROOT_V_SENTENCE,
        _state(IVEN),
        assistant="Toll-Marshal Iven waits behind his polehammer and buckler.",
    )

    frame, _fingerprint = _committed_frame(result, NEGATED_ROOT_V_SENTENCE)
    _assert_frame(
        frame,
        NEGATED_ROOT_V_SENTENCE,
        action_class="weapon_attack",
        target_entity_id="toll_marshal_iven",
        target_name="Toll-Marshal Iven",
        possessed_object="polehammer",
        linguistic_possessor_id="toll_marshal_iven",
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="shaft",
        target_locus="ribs",
        target_locus_owner_id="toll_marshal_iven",
        polarity="negative",
        evidence_phrases=(
            "I",
            "Rope-Dart Meridian Pierce",
            "Iven",
            "polehammer",
            "shaft",
            "ribs",
        ),
    )
    assert not any(
        op.get("op") in {
            "check", "master_tick", "combatant_spawn", "combatant_hp", "scene_set",
            "effect_add",
        }
        for op in result.rule_ops
    )


def test_ambiguous_possessive_owner_commits_candidates_and_abstains_before_cost_or_combat():
    result = _run(
        AMBIGUOUS_OWNER_SENTENCE,
        _state(NORTH_IVEN, SOUTH_IVEN),
        assistant="North Marshal Iven and South Marshal Iven level matching polehammers.",
    )

    frame, _fingerprint = _committed_frame(result, AMBIGUOUS_OWNER_SENTENCE)
    _assert_frame(
        frame,
        AMBIGUOUS_OWNER_SENTENCE,
        action_class="weapon_attack",
        target_entity_id=None,
        target_name=None,
        possessed_object="polehammer",
        linguistic_possessor_id=None,
        possessed_object_instance_id=None,
        possessed_object_owner_id=None,
        possessed_object_part="shaft",
        target_locus="",
        target_locus_owner_id=None,
        ambiguity={"north_marshal_iven", "south_marshal_iven"},
        evidence_phrases=(
            "Rope-Dart Meridian Pierce",
            "Iven",
            "polehammer",
            "shaft",
        ),
    )
    assert not any(
        op.get("op") in {
            "check", "master_tick", "combatant_spawn", "combatant_hp", "scene_set",
            "effect_add",
        }
        for op in result.rule_ops
    )


def test_four_worlds_share_one_recognition_but_bind_context_and_item_authority_separately():
    ungrounded = _state()

    unique_foreign_item = _state(IVEN)
    unique_foreign_item["items"]["sava_polehammer"] = {
        "name": "polehammer",
        "owner": "sava_orr",
        "loc": "gear:main_hand",
    }

    ambiguous = _state(NORTH_IVEN, SOUTH_IVEN)

    exact_item = _state(IVEN)
    exact_item["items"]["iven_polehammer"] = {
        "name": "polehammer",
        "owner": "toll_marshal_iven",
        "loc": "gear:main_hand",
    }

    worlds = {
        "ungrounded": ungrounded,
        "unique_foreign_item": unique_foreign_item,
        "ambiguous": ambiguous,
        "exact_item": exact_item,
    }
    receipts: dict[str, dict] = {}
    frames: dict[str, dict] = {}
    for label, state in worlds.items():
        result = _run(ROOT_V_SENTENCE, state)
        meaning, committed_frames = _committed_semantics(result, ROOT_V_SENTENCE)
        assert len(committed_frames) == 1
        receipts[label] = meaning
        frames[label] = committed_frames[0]

    assert all(receipt == receipts["ungrounded"] for receipt in receipts.values())
    assert "referent.possession.genitive" in {
        match["concept_id"] for match in receipts["ungrounded"]["matches"]
    }

    assert frames["ungrounded"]["target_entity_id"] is None
    assert frames["ungrounded"]["linguistic_possessor_id"] is None
    assert frames["ungrounded"]["possessed_object_instance_id"] is None
    assert frames["ungrounded"]["possessed_object_owner_id"] is None

    assert frames["unique_foreign_item"]["target_entity_id"] == "toll_marshal_iven"
    assert frames["unique_foreign_item"]["linguistic_possessor_id"] \
        == "toll_marshal_iven"
    assert frames["unique_foreign_item"]["possessed_object_instance_id"] is None
    assert frames["unique_foreign_item"]["possessed_object_owner_id"] is None

    assert frames["ambiguous"]["target_entity_id"] is None
    assert frames["ambiguous"]["linguistic_possessor_id"] is None
    assert frames["ambiguous"]["possessed_object_instance_id"] is None
    assert frames["ambiguous"]["possessed_object_owner_id"] is None
    assert set(frames["ambiguous"]["ambiguity"]) == {
        "north_marshal_iven", "south_marshal_iven",
    }

    assert frames["exact_item"]["target_entity_id"] == "toll_marshal_iven"
    assert frames["exact_item"]["linguistic_possessor_id"] == "toll_marshal_iven"
    assert frames["exact_item"]["possessed_object_instance_id"] == "iven_polehammer"
    assert frames["exact_item"]["possessed_object_owner_id"] == "toll_marshal_iven"


def test_two_current_clauses_keep_utility_and_attack_settlement_on_their_own_frames():
    source = f"{INSPECTION_SENTENCE} {ROOT_V_SENTENCE}"
    result = _run(
        source,
        _state(IVEN),
        assistant="Toll-Marshal Iven presents the polehammer, then braces for the strike.",
    )

    _meaning, frames = _committed_semantics(result, source)
    assert len(frames) == 2
    by_capability = {frame["capability_id"]: frame for frame in frames}
    inspection = by_capability["kiln_song_vibrometry"]
    attack = by_capability["rope_dart_meridian_pierce"]
    assert inspection["action_class"] == "inspection"
    assert attack["action_class"] == "weapon_attack"
    assert inspection["context_frame"]["span_end"] <= source.index(ROOT_V_SENTENCE)
    assert attack["context_frame"]["span_start"] == source.index(ROOT_V_SENTENCE)

    checks = {op["skill"]: op for op in _ops(result, "check")}
    assert set(checks) == {"kiln_song_vibrometry", "rope_dart_meridian_pierce"}
    assert checks["kiln_song_vibrometry"]["_semantic_frame_ref"] \
        == inspection["fingerprint"]
    assert checks["rope_dart_meridian_pierce"]["_semantic_frame_ref"] \
        == attack["fingerprint"]

    spawns = _ops(result, "combatant_spawn")
    assert len(spawns) == 1
    assert spawns[0]["_semantic_frame_ref"] == attack["fingerprint"]
    damage = _ops(result, "combatant_hp")
    assert len(damage) == 1
    assert damage[0]["target"] == "toll_marshal_iven"
    assert damage[0]["_semantic_frame_ref"] == attack["fingerprint"]
    _assert_all_sentence_ops_refer_to(
        result, {inspection["fingerprint"], attack["fingerprint"]},
    )


@pytest.mark.parametrize(
    ("source", "expected_modality", "expected_time_scope"),
    [
        (
            ROOT_V_SENTENCE.replace("I use", "Tomorrow I use", 1),
            "possible",
            "future",
        ),
        (
            ROOT_V_SENTENCE.replace("I use", "Yesterday I used", 1),
            "actual",
            "past",
        ),
        (
            ROOT_V_SENTENCE.replace("I use", "I plan to use", 1)[:-1] + " tomorrow.",
            "possible",
            "future",
        ),
    ],
    ids=("future", "past", "plan"),
)
def test_noncurrent_or_planned_attack_is_interpreted_but_not_actionable(
    source: str,
    expected_modality: str,
    expected_time_scope: str,
):
    result = _run(
        source,
        _state(IVEN),
        assistant="Toll-Marshal Iven waits behind his polehammer and buckler.",
    )

    frame, _fingerprint = _committed_frame(result, source)
    assert frame["capability_id"] == "rope_dart_meridian_pierce"
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["polarity"] == "positive"
    assert frame["modality"] == expected_modality
    assert frame["time_scope"] == expected_time_scope
    assert not any(
        op.get("op") in {
            "check", "master_tick", "combatant_spawn", "combatant_hp", "scene_set",
            "effect_add",
        }
        for op in result.rule_ops
    )


def test_question_clause_is_nonactionable_without_poisoning_the_next_actual_clause():
    question = (
        "Do I use Kiln-Song Vibrometry to inspect Iven's polehammer shaft?"
    )
    source = f"{question} {ROOT_V_SENTENCE}"
    result = _run(
        source,
        _state(IVEN),
        assistant="Toll-Marshal Iven presents the polehammer, then braces for the strike.",
    )

    _meaning, frames = _committed_semantics(result, source)
    assert len(frames) == 2
    by_capability = {frame["capability_id"]: frame for frame in frames}
    question_frame = by_capability["kiln_song_vibrometry"]
    action_frame = by_capability["rope_dart_meridian_pierce"]
    assert question_frame["action_class"] == "inspection"
    assert question_frame["modality"] == "question"
    assert question_frame["time_scope"] == "current"
    assert action_frame["action_class"] == "weapon_attack"
    assert action_frame["modality"] == "actual"
    assert action_frame["time_scope"] == "current"
    assert question_frame["context_frame"]["span_end"] <= source.index(ROOT_V_SENTENCE)
    assert action_frame["context_frame"]["span_start"] == source.index(ROOT_V_SENTENCE)

    checks = _ops(result, "check")
    assert [op["skill"] for op in checks] == ["rope_dart_meridian_pierce"]
    assert checks[0]["_semantic_frame_ref"] == action_frame["fingerprint"]
    assert all(
        op["_semantic_frame_ref"] == action_frame["fingerprint"]
        for op in _ops(result, "combatant_spawn") + _ops(result, "combatant_hp")
    )


def test_explicit_check_is_projected_through_the_same_command_frame_before_mechanics():
    source = (
        "((aether.check rope_dart_meridian_pierce at Iven)) "
        "I send the steel dart around Iven's polehammer shaft and drive the weighted tail "
        "into his ribs."
    )
    result = _run(
        source,
        _state(IVEN),
        assistant="Toll-Marshal Iven braces behind his polehammer and buckler.",
    )

    frame, fingerprint = _committed_frame(result, source)
    assert frame["actor_id"] == "sava_orr"
    assert frame["capability_id"] == "rope_dart_meridian_pierce"
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["modality"] == "command"
    assert frame["polarity"] == "positive"
    assert frame["time_scope"] == "current"
    assert any(row["kind"] == "command" for row in frame["evidence"])

    checks = _ops(result, "check")
    assert len(checks) == 1
    assert checks[0]["skill"] == "rope_dart_meridian_pierce"
    assert checks[0]["_cost"] == {"spoolcharge": 6}
    assert checks[0]["_semantic_frame_ref"] == fingerprint
    assert [op["char"] for op in _ops(result, "combatant_spawn")] == [
        "toll_marshal_iven"
    ]
    assert [op["target"] for op in _ops(result, "combatant_hp")] == [
        "toll_marshal_iven"
    ]
    _assert_all_sentence_ops_refer_to(result, fingerprint)


def test_explicit_utility_check_at_a_person_cannot_become_combat_damage():
    source = (
        "((aether.check kiln_song_vibrometry at Iven)) "
        "I inspect Iven's polehammer shaft for hidden stress fractures."
    )
    result = _run(
        source,
        _state(IVEN),
        assistant="Toll-Marshal Iven holds the polehammer out for inspection.",
    )

    frame, fingerprint = _committed_frame(result, source)
    assert frame["capability_id"] == "kiln_song_vibrometry"
    assert frame["action_class"] == "inspection"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["modality"] == "command"
    checks = _ops(result, "check")
    assert len(checks) == 1
    assert checks[0]["_semantic_frame_ref"] == fingerprint
    assert not _ops(result, "combatant_spawn")
    assert not _ops(result, "combatant_hp")


def test_frame_without_reachable_receipts_fails_closed_in_the_mandatory_header():
    result = _run(
        ROOT_V_SENTENCE,
        _state(IVEN),
        assistant="Toll-Marshal Iven braces behind his polehammer and buckler.",
    )
    frame, fingerprint = _committed_frame(result, ROOT_V_SENTENCE)
    state = _state(IVEN)
    state["meta"]["turn"] = 1
    state["semantic_frames"] = [{"turn": 1, "frame": frame}]
    state["_fresh_checks"] = [{
        "op": "check",
        "skill": "rope_dart_meridian_pierce",
        "tier": "success",
        "_semantic_frame_ref": fingerprint,
    }]

    header = compose.render_header(state, _rpg_cfg())

    assert "NARRATOR REALIZATION UNAVAILABLE" in header
    assert "CANONICAL ACTION:" not in header
    assert "success" not in header
    assert ROOT_V_SENTENCE not in header


def test_capability_tie_is_committed_with_null_selection_and_bounded_evidence():
    state = _state()
    player = state["player"]["sava_orr"]
    player["skills"] = {"rune_lore": 1, "forensic_sight": 1}
    player["defs"] = {"skills": {
        "rune_lore": {
            "name": "Rune Lore", "keyed_stat": "CUN", "governs": ["examine"],
        },
        "forensic_sight": {
            "name": "Forensic Sight", "keyed_stat": "CUN", "governs": ["examine"],
        },
    }}
    source = "I examine the unfamiliar sigil."

    result = _run(source, state)
    frame, _fingerprint = _committed_frame(result, source)

    assert frame["capability_id"] is None
    assert set(frame["ambiguity"]) == {"rune_lore", "forensic_sight"}
    capability_evidence = {
        row["value"] for row in frame["evidence"] if row["kind"] == "capability"
    }
    assert capability_evidence == {"rune_lore", "forensic_sight"}
    assert not _ops(result, "check")
    assert not _ops(result, "combatant_spawn")
    assert not _ops(result, "combatant_hp")
    assert [op["op"] for op in result.rule_ops[:3]] == [
        "semantic_meaning_commit", "semantic_binding_commit", "semantic_frame_commit",
    ]


@pytest.mark.parametrize(
    ("source", "assertion", "modality", "time_scope"),
    [
        (
            "I report that I use Rope-Dart Meridian Pierce to strike Iven.",
            "testimony", "actual", "current",
        ),
        (
            "I remember that I use Rope-Dart Meridian Pierce to strike Iven.",
            "remembered", "actual", "current",
        ),
        (
            "I believe that I use Rope-Dart Meridian Pierce to strike Iven.",
            "believed", "actual", "current",
        ),
        (
            "According to Iven, I use Rope-Dart Meridian Pierce to strike Iven.",
            "testimony", "actual", "current",
        ),
        (
            "I ask whether I use Rope-Dart Meridian Pierce to strike Iven.",
            "quoted", "question", "current",
        ),
        (
            "I promise that I will use Rope-Dart Meridian Pierce to strike Iven.",
            "attributed", "possible", "future",
        ),
    ],
    ids=("report", "memory", "belief", "according-to", "ask-whether", "promise"),
)
def test_structurally_embedded_actions_are_retained_but_never_settled(
    source: str,
    assertion: str,
    modality: str,
    time_scope: str,
):
    result = _run(source, _state(IVEN))
    frame, _fingerprint = _committed_frame(result, source)
    binding = _ops(result, "semantic_binding_commit")[0]["binding"]

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["modality"] == modality
    assert frame["time_scope"] == time_scope
    assert binding["mechanic_disposition"] == "recognition_only"
    assert ("assertion_context", assertion) in {
        (constraint["dimension"], constraint["value"])
        for constraint in binding["constraints"]
    }
    assert not any(
        op.get("op") in {
            "check", "master_tick", "combatant_spawn", "combatant_hp", "scene_set",
            "effect_add",
        }
        for op in result.rule_ops
    )


def test_incomplete_embedding_holds_and_bare_if_remains_a_hypothesis():
    incomplete = "I report I use Rope-Dart Meridian Pierce to strike Iven."
    held = _run(incomplete, _state(IVEN))
    held_binding = _ops(held, "semantic_binding_commit")[0]["binding"]
    assert held_binding["mechanic_disposition"] == "hold_unresolved"
    assert ("assertion_context", "unresolved") in {
        (row["dimension"], row["value"]) for row in held_binding["constraints"]
    }
    assert not _ops(held, "check")

    hypothetical = "If I use Rope-Dart Meridian Pierce to strike Iven."
    described = _run(hypothetical, _state(IVEN))
    frame, _fingerprint = _committed_frame(described, hypothetical)
    binding = _ops(described, "semantic_binding_commit")[0]["binding"]
    assert frame["modality"] == "hypothetical"
    assert binding["mechanic_disposition"] == "recognition_only"
    assert not _ops(described, "check")


def test_quoted_action_is_recognized_in_raw_meaning_but_has_no_performed_frame():
    source = '"I use Rope-Dart Meridian Pierce to strike Iven."'
    result = _run(source, _state(IVEN))

    assert result.semantic_turn is not None
    assert result.semantic_turn.compiled_meaning is not None
    assert "action.weapon_attack" in {
        match.concept_id for match in result.semantic_turn.compiled_meaning.for_lex("action")
    }
    assert not _ops(result, "semantic_frame_commit")
    assert not _ops(result, "check")
    assert not _ops(result, "combatant_spawn")


def test_event_boundaries_prevent_target_and_action_lending():
    mara = {
        "eid": "mara_voss",
        "kind": "npc",
        "name": "Mara Voss",
        "present": True,
        "aliases": ["Mara"],
        "role": "An observing investigator.",
    }
    source = (
        "I use Rope-Dart Meridian Pierce to strike Iven, but I inspect Mara."
    )
    result = _run(source, _state(IVEN, mara))
    frame, _fingerprint = _committed_frame(result, source)

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["context_frame"]["span_end"] <= source.index("but")
    assert all(row.get("value") != "mara_voss" for row in frame["evidence"])


def test_repeated_capability_across_independent_events_produces_disjoint_frames():
    mara = {
        "eid": "mara_voss",
        "kind": "npc",
        "name": "Mara Voss",
        "present": True,
        "aliases": ["Mara"],
        "role": "A second opponent.",
    }
    source = (
        "I use Rope-Dart Meridian Pierce to strike Iven, but I use "
        "Rope-Dart Meridian Pierce to strike Mara."
    )
    result = _run(source, _state(IVEN, mara))
    _meaning, frames = _committed_semantics(result, source)

    assert len(frames) == 2
    assert [frame["target_entity_id"] for frame in frames] == [
        "toll_marshal_iven", "mara_voss",
    ]
    assert frames[0]["context_frame"]["span_end"] \
        <= frames[1]["context_frame"]["span_start"]
    assert frames[0]["meaning_binding_ref"] != frames[1]["meaning_binding_ref"]


def test_while_gerund_stays_inside_action_but_renewed_subject_is_bounded():
    direct = "I use Rope-Dart Meridian Pierce while striking Iven."
    result = _run(direct, _state(IVEN))
    frame, _fingerprint = _committed_frame(result, direct)
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "toll_marshal_iven"
    assert frame["context_frame"]["span_end"] == direct.index(".")

    mara = {
        "eid": "mara_voss",
        "kind": "npc",
        "name": "Mara Voss",
        "present": True,
        "aliases": ["Mara"],
    }
    observed = "I use Rope-Dart Meridian Pierce to strike Iven while Mara watches."
    bounded = _run(observed, _state(IVEN, mara))
    observed_frame, _fingerprint = _committed_frame(bounded, observed)
    assert observed_frame["target_entity_id"] == "toll_marshal_iven"
    assert observed_frame["context_frame"]["span_end"] <= observed.index("while")


def test_tier0_commits_all_four_possessive_alignment_results_without_common_mode_ownership():
    worlds = {
        "uncheckable": _state(IVEN),
        "unresolved": _state(IVEN),
        "false": _state(IVEN),
        "positive": _state(IVEN),
    }
    worlds["uncheckable"].pop("items", None)
    worlds["unresolved"]["items"] = {
        "polehammer_b": {"name": "polehammer", "owner": "toll_marshal_iven"},
        "polehammer_a": {"name": "polehammer", "owner": "toll_marshal_iven"},
    }
    worlds["false"]["items"] = {
        "mara_polehammer": {"name": "polehammer", "owner": "mara_voss"},
    }
    worlds["positive"]["items"] = {
        "iven_polehammer": {"name": "polehammer", "owner": "toll_marshal_iven"},
    }

    for expected, state in worlds.items():
        result = _run(ROOT_V_SENTENCE, state)
        frame, _fingerprint = _committed_frame(result, ROOT_V_SENTENCE)
        alignment = _ops(result, "semantic_world_alignment_commit")[0]["alignment"]
        assert alignment["status"] == expected
        if expected == "positive":
            assert frame["possessed_object_instance_id"] == "iven_polehammer"
            assert frame["possessed_object_owner_id"] == "toll_marshal_iven"
        else:
            assert frame["possessed_object_instance_id"] is None
            assert frame["possessed_object_owner_id"] is None


def test_nonexecuting_semantics_cannot_drive_time_or_departure_heuristics():
    source = (
        "I report that the next morning I use Rope-Dart Meridian Pierce to strike Iven, "
        "and Iven leaves."
    )
    result = _run(source, _state(IVEN))
    binding = _ops(result, "semantic_binding_commit")[0]["binding"]

    assert binding["mechanic_disposition"] == "recognition_only"
    assert not _ops(result, "time_advance")
    assert not _ops(result, "presence")


def test_supplied_opening_assessment_is_shared_without_reparsing(monkeypatch):
    assessment = tier0.CombatOpeningAssessment(
        matched=False,
        target=None,
        prompt_signal=False,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("combat-opening interpretation ran more than once")

    monkeypatch.setattr(tier0, "combat_opening_assessment", forbidden)
    result = tier0.run(
        {"messages": [{"role": "user", "content": INSPECTION_SENTENCE}]},
        "new_turn",
        False,
        _state(IVEN),
        _rpg_cfg(),
        random.Random(7),
        opening_assessment=assessment,
    )

    assert _ops(result, "check")
    assert _ops(result, "semantic_meaning_commit")
    assert _ops(result, "semantic_frame_commit")


def test_root_v_frame_admits_worldlex_enemy_and_replays_exactly_after_reopen(tmp_path):
    cfg = _rpg_cfg()
    path = tmp_path / "root-v-semantic.sqlite3"
    store = Store(path)
    sid, branch = store.create_session(external_id="root-v-semantic")
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "world_identity_set", "world_id": "world_" + "7" * 32},
        {"op": "entity_add", "name": "Sava Orr", "kind": "player"},
        {"op": "player_seed", "entity": "Sava Orr", "card": {
            "stats": {"DEX": 16, "CUN": 14, "STR": 12},
            "skills": {
                "rope_dart_meridian_pierce": 4,
                "kiln_song_vibrometry": 3,
            },
            "abilities": [],
            "resources": {
                "hp": {"name": "HP", "max": 30, "cur": 30},
                "spoolcharge": {"name": "Spoolcharge", "max": 300, "cur": 260},
            },
            "defs": {"skills": {
                "rope_dart_meridian_pierce": {
                    "name": "Rope-Dart Meridian Pierce",
                    "keyed_stat": "DEX",
                    "governs": ["send", "wrench", "drive", "whip"],
                    "cost": {"spoolcharge": 6},
                },
                "kiln_song_vibrometry": {
                    "name": "Kiln-Song Vibrometry",
                    "keyed_stat": "CUN",
                    "governs": ["inspect", "study", "read resonance"],
                    "cost": {"spoolcharge": 4},
                },
            }},
        }},
        {"op": "entity_add", "name": "Toll-Marshal Iven", "kind": "npc"},
        {"op": "set_attribute", "entity": "Toll-Marshal Iven", "key": "role",
         "value": "An opening polehammer-and-buckler warden."},
        {"op": "presence", "entity": "Toll-Marshal Iven", "present": True},
    ], "genesis", cfg)
    assert len(seeded.applied) == 6, seeded.quarantined

    state = current_state(store, branch)
    result = tier0.run(
        {"messages": [
            {"role": "assistant",
             "content": "Toll-Marshal Iven braces behind his polehammer and buckler."},
            {"role": "user", "content": ROOT_V_SENTENCE},
        ]},
        "new_turn",
        False,
        state,
        cfg,
        random.Random(7),
        turn=1,
    )
    settled = apply_delta(store, sid, branch, 1, result.rule_ops, "rule", cfg)
    assert not settled.quarantined

    combatants = settled.state["combat"]["combatants"]
    row = next(item for item in combatants.values()
               if item.get("eid") == "toll_marshal_iven")
    assert row["kit_source"] == "worldlex-runtime-pool"
    assert row["capability_pool"]["pools"]["runtime"]["members"]
    assert all(member["executable"] is True
               for member in row["capability_pool"]["pools"]["runtime"]["members"])
    intent = settled.state["combat"]["pending_intent"]
    assert intent["actor"] == row["id"]
    assert settled.state["player"]["sava_orr"]["resources"]["spoolcharge"]["cur"] == 254
    assert settled.state["semantic_frames"][0]["frame"]["target_entity_id"] \
        == "toll_marshal_iven"

    header = compose.render_header(settled.state, cfg)
    assert header.count("NARRATOR REALIZATION narrator-realization/1") == 1
    assert '"capability_id":"rope_dart_meridian_pierce"' in header
    assert '"target_entity_id":"toll_marshal_iven"' in header
    assert '"target_locus":"ribs"' in header
    assert "NARRATOR REALIZATION UNAVAILABLE" not in header
    assert ROOT_V_SENTENCE not in header
    intent_line = next(
        line for line in header.splitlines()
        if line.startswith("[ENEMY INTENT enemy-intent/1]")
    )
    assert header.index("NARRATOR REALIZATION narrator-realization/1") \
        < header.index("[ENEMY INTENT enemy-intent/1]")
    assert "Exact complete enemy prose:" in intent_line
    assert intent_line.count(intent["tell"]) == 1
    assert intent["sensory"] not in intent_line
    assert all(opening not in intent_line for opening in intent["counterplay"])
    assert "does not advance, disrupt, alter, deflect, interrupt" in intent_line
    assert "The future intent remains unchanged and pending" in intent_line
    assert "not passive readiness, a reset, or a skipped enemy turn" in intent_line
    assert "Player's response window" in intent_line
    assert "The attack is committed; this is the moment to answer it before impact." in intent_line
    assert "Fictional openings" not in intent_line

    packet, _kept = compose.compose(
        {"messages": [{"role": "user", "content": ROOT_V_SENTENCE}]},
        settled.state,
        cfg,
        None,
        "new_turn",
    )
    assert packet is not None
    current = next(
        message["content"] for message in packet["messages"]
        if isinstance(message, dict) and message.get("role") == "user"
    )
    assert "SETTLED ATTACK + FIRST INTENT OUTPUT LIMIT" in current
    assert "FIRST-INTENT OUTPUT SHAPE" not in current
    assert "does not advance, disrupt, alter, deflect, interrupt, cancel" in current
    assert "not resetting, waiting, or losing its turn" in current
    assert "Player's response window" in current

    exact = current_state(store, branch)
    store.close()
    reopened = Store(path)
    try:
        assert current_state(reopened, branch) == exact
    finally:
        reopened.close()
