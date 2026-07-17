from __future__ import annotations

import copy
import json
import random

import pytest

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.playerlex_recognition import merge_playerlex_proposal
from aetherstate.semantic import SemanticTurn
from aetherstate.semantic_atlas import load_default_semantic_atlas
from aetherstate.semantic_binding import action_classes_for_matches
from aetherstate.semantic_fabric import (
    load_default_semantic_fabric,
    validate_compiled_meaning,
    validate_compiled_meaning_receipt,
)
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import empty_state
from aetherstate.store import Store


SOURCE = "I invoke Night Fold."


def _service(store: Store):
    from aetherstate.playerlex import PlayerLex

    return PlayerLex(
        store.db,
        load_default_semantic_atlas(),
        store.apply_guard(),
    )


def _approve_night_fold(service):
    return service.approve(
        kind="alias",
        surface="Night Fold",
        lex_id="capability",
        concept_id="skill.stealth",
    )


def _merge(service, source: str = SOURCE):
    fabric = load_default_semantic_fabric()
    base = fabric.translate(source)
    return base, merge_playerlex_proposal(
        base,
        service.propose(source),
        fabric=fabric,
        source_text=source,
    )


def test_night_fold_enters_compiled_meaning_without_source_prose_in_replay_receipt():
    store = Store(":memory:")
    service = _service(store)
    approved = _approve_night_fold(service)

    base, meaning = _merge(service)
    overlay = [match for match in meaning.matches if match.surface_baseline == "playerlex"]
    assert not any(match.concept_id == "skill.stealth" for match in base.matches)
    assert len(overlay) == 1
    match = overlay[0]
    start = SOURCE.index("Night Fold")
    assert (match.start, match.end, match.matched_phrase) == (
        start,
        start + len("Night Fold"),
        "Night Fold",
    )
    assert (match.lex_id, match.concept_id) == ("capability", "skill.stealth")
    assert match.entry_fingerprint == approved["concept"]["meaning_fingerprint"]
    assert match.features["meaning_fingerprint"] == approved["concept"]["meaning_fingerprint"]
    approval_ref = f"playerlex.{approved['entry_id'].removeprefix('playerlex_')}.r1"
    assert approval_ref in match.source_ids

    row = next(item for item in meaning.as_dict()["matches"] if item["surface_baseline"] == "playerlex")
    assert row["recognized"] is True
    assert row["authorized"] is False
    assert row["executable"] is False
    assert row["requires_context_binding"] is True
    validate_compiled_meaning(meaning.as_dict())

    receipt = meaning.receipt_dict()
    validate_compiled_meaning_receipt(receipt)
    receipt_match = next(item for item in receipt["matches"] if item["surface_baseline"] == "playerlex")
    assert (receipt_match["lex_id"], receipt_match["concept_id"]) == (
        "capability",
        "skill.stealth",
    )
    encoded = json.dumps(receipt, ensure_ascii=False)
    assert "Night Fold" not in encoded
    assert SOURCE not in encoded
    assert approved["entry_id"] not in encoded
    assert approval_ref in encoded


def test_playerlex_overlay_does_not_create_candidates_checks_or_world_authority():
    store = Store(":memory:")
    service = _service(store)
    _approve_night_fold(service)
    calls: list[str] = []

    def propose(text: str):
        calls.append(text)
        return service.propose(text)

    cfg = Config()
    cfg.specialization.name = "rpg"
    baseline = tier0.run(
        {"messages": [{"role": "user", "content": SOURCE}]},
        "new_turn",
        False,
        empty_state(),
        cfg,
        random.Random(7),
    )
    overlaid = tier0.run(
        {"messages": [{"role": "user", "content": SOURCE}]},
        "new_turn",
        False,
        empty_state(),
        cfg,
        random.Random(7),
        recognition_overlay=propose,
    )

    assert calls == [SOURCE]
    assert overlaid.semantic_turn is not None
    assert any(
        match.surface_baseline == "playerlex" for match in overlaid.semantic_turn.compiled_meaning.matches
    )
    assert overlaid.semantic_turn.frames == baseline.semantic_turn.frames == []
    assert overlaid.user_ops == baseline.user_ops
    assert overlaid.rule_ops == baseline.rule_ops
    assert overlaid.checks == baseline.checks == []
    assert overlaid.proposal_ops == baseline.proposal_ops == []
    forbidden = {
        "check",
        "semantic_binding_commit",
        "semantic_world_alignment_commit",
        "semantic_frame_commit",
        "mechanic_settlement_commit",
        "worldlex_record",
        "worldlex_assignment",
    }
    assert not forbidden.intersection(op.get("op") for op in overlaid.rule_ops if isinstance(op, dict))


def test_duplicate_approvals_coalesce_and_same_lex_meanings_remain_ambiguous():
    store = Store(":memory:")
    service = _service(store)
    first = service.approve(
        kind="alias",
        surface="Night Fold",
        lex_id="capability",
        concept_id="action.teleportation",
    )
    second = service.approve(
        kind="name",
        surface="Night Fold",
        lex_id="capability",
        concept_id="action.teleportation",
    )
    service.approve(
        kind="alias",
        surface="Night Fold",
        lex_id="capability",
        concept_id="skill.stealth",
    )
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    proposal = service.propose(SOURCE)
    proposal["matches"].append(copy.deepcopy(proposal["matches"][0]))
    proposal["match_count"] += 1

    meaning = merge_playerlex_proposal(base, proposal, fabric=fabric, source_text=SOURCE)
    rows = [match for match in meaning.matches if match.surface_baseline == "playerlex"]
    assert {(row.lex_id, row.concept_id) for row in rows} == {
        ("capability", "action.teleportation"),
        ("capability", "skill.stealth"),
    }
    ambiguity = ("action.teleportation", "skill.stealth")
    assert all(row.ambiguity == ambiguity for row in rows)
    assert meaning.unresolved[-2:] == ambiguity
    teleportation = next(row for row in rows if row.concept_id == "action.teleportation")
    expected_refs = {
        f"playerlex.{first['entry_id'].removeprefix('playerlex_')}.r1",
        f"playerlex.{second['entry_id'].removeprefix('playerlex_')}.r1",
    }
    assert expected_refs.issubset(set(teleportation.source_ids))
    validate_compiled_meaning(meaning.as_dict())


def test_cross_lex_id_collision_remains_two_typed_rows_without_invented_ambiguity():
    store = Store(":memory:")
    service = _service(store)
    for lex_id in ("capability", "action"):
        service.approve(
            kind="alias",
            surface="Patch Rite",
            lex_id=lex_id,
            concept_id="action.repair",
        )
    source = "I perform Patch Rite."
    _base, meaning = _merge(service, source)
    rows = [match for match in meaning.matches if match.surface_baseline == "playerlex"]

    assert [(row.lex_id, row.concept_id) for row in rows] == [
        ("action", "action.repair"),
        ("capability", "action.repair"),
    ]
    assert all(row.ambiguity == () for row in rows)
    action = next(row for row in rows if row.lex_id == "action")
    sealed = load_default_semantic_fabric().entry("action.repair")
    assert action.required_roles == sealed.required_roles
    assert action.optional_roles == sealed.optional_roles
    assert action.completion == sealed.completion
    assert action.features == sealed.features
    assert action_classes_for_matches((action,)) == ("repair_or_heal",)


def test_refused_only_and_no_overlay_paths_return_the_exact_base_object():
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    refused = {
        "schema": "playerlex-recognition-proposal/1",
        "match_count": 0,
        "matches": [],
        "refused": [
            {
                "entry_id": "playerlex_" + "a" * 32,
                "lex_id": "capability",
                "reason": "meaning_fingerprint_changed",
            },
            {
                "entry_id": "playerlex_" + "b" * 32,
                "lex_id": "action",
                "reason": "corrupt_entry",
            },
        ],
    }
    assert merge_playerlex_proposal(base, refused, fabric=fabric, source_text=SOURCE) is base

    turn = SemanticTurn(SOURCE)
    compiled = turn.compile(fabric)
    assert compiled.as_dict() == base.as_dict()
    calls: list[str] = []
    assert (
        turn.compile(
            fabric,
            recognition_overlay=lambda text: calls.append(text) or refused,
        )
        is compiled
    )
    assert calls == []


def test_stale_corrupt_or_authority_claiming_candidate_rows_are_excluded():
    store = Store(":memory:")
    service = _service(store)
    _approve_night_fold(service)
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    proposal = service.propose(SOURCE)

    stale = copy.deepcopy(proposal)
    stale["matches"][0]["status"] = "stale"
    corrupt = copy.deepcopy(proposal)
    corrupt["matches"][0]["status"] = "corrupt"
    authority_claim = copy.deepcopy(proposal)
    authority_claim["matches"][0]["authorized"] = True
    mismatched_meaning = copy.deepcopy(proposal)
    mismatched_meaning["matches"][0]["concept"]["meaning_fingerprint"] = "sha256:" + "f" * 64

    for rejected in (stale, corrupt, authority_claim, mismatched_meaning):
        assert merge_playerlex_proposal(base, rejected, fabric=fabric, source_text=SOURCE) is base


def test_candidate_with_wrong_source_span_text_returns_exact_base_object():
    store = Store(":memory:")
    service = _service(store)
    _approve_night_fold(service)
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    proposal = service.propose(SOURCE)
    proposal["matches"][0]["source_span"]["text"] = "Wrong Text"

    assert merge_playerlex_proposal(base, proposal, fabric=fabric, source_text=SOURCE) is base


def test_candidate_with_out_of_bounds_source_span_returns_exact_base_object():
    store = Store(":memory:")
    service = _service(store)
    _approve_night_fold(service)
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    proposal = service.propose(SOURCE)
    span = proposal["matches"][0]["source_span"]
    span["start"] = len(SOURCE) + 1
    span["end"] = span["start"] + len(span["text"])

    assert merge_playerlex_proposal(base, proposal, fabric=fabric, source_text=SOURCE) is base


def test_proposal_for_different_source_returns_exact_base_object():
    store = Store(":memory:")
    service = _service(store)
    _approve_night_fold(service)
    fabric = load_default_semantic_fabric()
    base = fabric.translate(SOURCE)
    different_source = "Night Fold now."
    proposal = service.propose(different_source)

    assert (
        merge_playerlex_proposal(
            base,
            proposal,
            fabric=fabric,
            source_text=different_source,
        )
        is base
    )


def test_pipeline_owns_one_cached_service_and_calls_it_once_for_the_player_turn(monkeypatch):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, rng=random.Random(11))
    assert pipe.playerlex_service is not None
    cached_service = pipe.playerlex_service
    assert store.db.in_transaction is False
    _approve_night_fold(pipe.playerlex_service)

    calls: list[str] = []
    original = pipe.playerlex_service.propose

    def counted(text: str):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(pipe.playerlex_service, "propose", counted)
    body = json.dumps(
        {
            "model": "playerlex-live-test",
            "messages": [{"role": "user", "content": SOURCE}],
        }
    ).encode("utf-8")
    pipe.process(
        Stamp(
            session="playerlex-live",
            turn=1,
            gen_type="normal",
            speaker="Narrator",
            card_role="narrator",
            user="Bean",
        ),
        body,
    )

    assert calls == [SOURCE]
    assert pipe.playerlex_service is cached_service


@pytest.mark.parametrize(
    "replay_kind",
    ("duplicate_transport", "continue", "swipe", "lost_reply"),
)
def test_pipeline_never_calls_playerlex_for_replay_or_non_player_turns(
    monkeypatch,
    replay_kind: str,
):
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, rng=random.Random(12))
    assert pipe.playerlex_service is not None
    _approve_night_fold(pipe.playerlex_service)

    calls: list[str] = []
    original_propose = pipe.playerlex_service.propose

    def counted_propose(text: str):
        calls.append(text)
        return original_propose(text)

    reserved_kinds: list[str] = []
    original_reserve = pipe._reserve_lost_turn

    def observed_reserve(resolution, document, state):
        reserved = original_reserve(resolution, document, state)
        if reserved is not None:
            reserved_kinds.append(reserved["kind"])
        return reserved

    monkeypatch.setattr(pipe.playerlex_service, "propose", counted_propose)
    monkeypatch.setattr(pipe, "_reserve_lost_turn", observed_reserve)
    body = json.dumps(
        {
            "model": "playerlex-live-replay-test",
            "messages": [{"role": "user", "content": SOURCE}],
        }
    ).encode("utf-8")
    initial_stamp = Stamp(
        session=f"playerlex-{replay_kind}",
        turn=1,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Bean",
    )
    pipe.process(initial_stamp, body)
    assert calls == [SOURCE]

    if replay_kind == "duplicate_transport":
        replay_stamp = initial_stamp
    else:
        replay_stamp = Stamp(
            session=initial_stamp.session,
            turn=2 if replay_kind == "lost_reply" else 1,
            gen_type=replay_kind if replay_kind != "lost_reply" else "normal",
            speaker="Narrator",
            card_role="narrator",
            user="Bean",
        )

    _packet, replay_context = pipe.process(replay_stamp, body)

    assert calls == [SOURCE]
    assert replay_context is not None
    if replay_kind == "duplicate_transport":
        assert replay_context.network_duplicate is True
        assert reserved_kinds == []
    elif replay_kind == "continue":
        assert replay_context.klass == "continue"
        assert reserved_kinds == []
    elif replay_kind == "swipe":
        assert replay_context.klass == "swipe"
        assert reserved_kinds == ["swipe_replay"]
    else:
        assert replay_context.klass == "new_turn"
        assert reserved_kinds == ["lost_reply"]
