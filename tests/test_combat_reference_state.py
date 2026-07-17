"""Deterministic state-owned live-combat reference identity and lifecycle truth."""
from __future__ import annotations

from copy import deepcopy

from aetherstate.canon import canonicalize, chain
from aetherstate.state import (
    CombatReferenceStatus,
    combat_reference_candidates,
    empty_state,
    reduce_state,
    resolve_combat_reference,
    resolve_combatant,
)
from aetherstate.store import Store


def _cohort_state(name: str = "Hollowed", total: int = 4, spawned: int = 3) -> dict:
    state = empty_state()
    ref = f"{name.casefold().replace(' ', '_')}_x{total}"
    state["battle"] = {
        "active": True,
        "name": "Reference Field",
        "cohort": {
            "schema": "battle-cohort/1",
            "id": ref,
            "name": name,
            "total": total,
            "tier": "standard",
            "armament": "",
            "spawned": spawned,
            "remaining": total - spawned,
        },
    }
    rows = {}
    base = name.casefold().replace(" ", "_")
    for index in range(1, spawned + 1):
        cid = base if index == 1 else f"{base}#{index}"
        rows[cid] = {
            "id": cid,
            "name": name,
            "side": "enemy",
            "hp": {"cur": 6, "max": 6},
            "defeated": False,
            "cohort": {"ref": ref, "index": index, "total": total},
        }
    rows["glass_seer"] = {
        "id": "glass_seer",
        "name": "Glass Seer",
        "side": "enemy",
        "hp": {"cur": 9, "max": 9},
        "defeated": False,
    }
    state["combat"] = {"active": True, "combatants": rows, "history": []}
    return state


def test_reference_api_resolves_visible_label_ordinal_morphology_and_exact_name():
    state = _cohort_state()

    direct = resolve_combat_reference(state, "Hollowed #1")
    assert direct.status is CombatReferenceStatus.RESOLVED
    assert direct.match_kind == "visible_label"
    assert direct.selected is not None
    assert direct.selected.combatant_id == "hollowed"
    assert direct.selected.label == "Hollowed #1"
    assert direct.selected.state == "active"

    ordinal = resolve_combat_reference(state, "the first hollow")
    assert ordinal.status is CombatReferenceStatus.RESOLVED
    assert ordinal.match_kind == "cohort_ordinal"
    assert ordinal.selected == direct.selected

    ordinary = resolve_combat_reference(state, "Glass Seer")
    assert ordinary.status is CombatReferenceStatus.RESOLVED
    assert ordinary.match_kind == "visible_label"
    assert ordinary.selected is not None
    assert ordinary.selected.combatant_id == "glass_seer"
    assert ordinary.selected.cohort_ref is None

    bare = resolve_combat_reference(state, "Hollowed")
    assert bare.status is CombatReferenceStatus.AMBIGUOUS
    assert bare.selected is None
    assert [candidate.label for candidate in bare.candidates] == [
        "Hollowed #1",
        "Hollowed #2",
        "Hollowed #3",
        "Hollowed #4",
    ]
    assert resolve_combatant(state, "Hollowed") is None
    assert resolve_combat_reference(state, "hollowed").status \
        is CombatReferenceStatus.AMBIGUOUS
    # Exact reducer journals keep the historical internal-id compatibility channel.
    assert resolve_combatant(state, "hollowed") == "hollowed"


def test_reference_api_rejects_unrelated_numbers_loci_possessions_and_pronouns():
    state = _cohort_state()
    rejected = (
        "1",
        "#1",
        "I rolled first",
        "Hollowed #1's blade",
        "Hollowed #1's left arm",
        "Hollowed #1's existence",
        "its existence",
        "him",
        "her",
        "it",
        "them",
        "that one",
    )

    for token in rejected:
        result = resolve_combat_reference(state, token)
        assert result.status is CombatReferenceStatus.UNKNOWN, (token, result)
        assert result.selected is None
        assert not result.candidates
        assert resolve_combatant(state, token) is None


def test_reference_api_exposes_two_cohort_head_collision_instead_of_guessing():
    state = _cohort_state("Baser Hollow", total=2, spawned=2)
    second = _cohort_state("Ash Hollows", total=2, spawned=2)
    state["combat"]["combatants"].update(second["combat"]["combatants"])

    broad = resolve_combat_reference(state, "first hollow")
    assert broad.status is CombatReferenceStatus.AMBIGUOUS
    assert [candidate.label for candidate in broad.candidates] == [
        "Baser Hollow #1",
        "Ash Hollows #1",
    ]
    assert resolve_combatant(state, "first hollow") is None

    exact = resolve_combat_reference(state, "first baser hollow")
    assert exact.status is CombatReferenceStatus.RESOLVED
    assert exact.selected is not None
    assert exact.selected.label == "Baser Hollow #1"


def test_structured_reference_rejects_partial_tokens_but_legacy_keeps_compatibility():
    state = _cohort_state()
    state["combat"]["combatants"]["stone_guard"] = {
        "id": "stone_guard",
        "name": "Stone Guard",
        "side": "enemy",
        "hp": {"cur": 8, "max": 8},
        "defeated": False,
    }
    before = deepcopy(state)

    for token, expected_id in (
        ("seer", "glass_seer"),
        ("glass", "glass_seer"),
        ("guard", "stone_guard"),
    ):
        first = resolve_combat_reference(state, token)
        second = resolve_combat_reference(state, token)
        assert first == second
        assert first.status is CombatReferenceStatus.UNKNOWN, (token, first)
        assert first.match_kind is None
        assert first.selected is None
        assert first.candidates == ()
        # Reducers keep the historical convenience channel; structured Player binding does not.
        assert resolve_combatant(state, token) == expected_id

    assert state == before


def test_exact_id_and_visible_name_collision_is_ambiguous_for_any_case():
    state = empty_state()
    state["combat"] = {
        "active": True,
        "history": [],
        "combatants": {
            "seer": {
                "id": "seer",
                "name": "Masked Oracle",
                "side": "enemy",
                "hp": {"cur": 7, "max": 7},
                "defeated": False,
            },
            "oracle_seer": {
                "id": "oracle_seer",
                "name": "Seer",
                "side": "enemy",
                "hp": {"cur": 6, "max": 6},
                "defeated": False,
            },
        },
    }
    before = deepcopy(state)

    for token in ("seer", "Seer"):
        first = resolve_combat_reference(state, token)
        second = resolve_combat_reference(state, token)
        assert first == second
        assert first.status is CombatReferenceStatus.AMBIGUOUS, (token, first)
        assert first.selected is None
        assert {
            (candidate.combatant_id, candidate.label)
            for candidate in first.candidates
        } == {("seer", "Masked Oracle"), ("oracle_seer", "Seer")}

    # Reducer journals retain their exact, case-sensitive internal-id channel.  A Player-facing
    # normalized label is not that trusted transport identity.
    assert resolve_combatant(state, "seer") == "seer"
    assert resolve_combatant(state, "Seer") is None

    assert state == before


def test_exact_noncohort_name_and_valid_cohort_ordinal_are_ambiguous():
    state = _cohort_state("Hollow", total=2, spawned=2)
    state["combat"]["combatants"]["first_hollow"] = {
        "id": "first_hollow",
        "name": "First Hollow",
        "side": "enemy",
        "hp": {"cur": 8, "max": 8},
        "defeated": False,
    }
    before = deepcopy(state)

    for token in ("first hollow", "First Hollow"):
        first = resolve_combat_reference(state, token)
        second = resolve_combat_reference(state, token)
        assert first == second
        assert first.status is CombatReferenceStatus.AMBIGUOUS, (token, first)
        assert first.selected is None
        assert {
            (candidate.combatant_id, candidate.label)
            for candidate in first.candidates
        } == {("hollow", "Hollow #1"), ("first_hollow", "First Hollow")}
        assert resolve_combatant(state, token) is None

    assert state == before


def test_legacy_subset_never_retargets_an_exact_defeated_identity():
    state = empty_state()
    state["combat"] = {
        "active": True,
        "history": [],
        "combatants": {
            "stone": {
                "id": "stone",
                "name": "Stone",
                "side": "enemy",
                "hp": {"cur": 0, "max": 4},
                "defeated": True,
            },
            "stone_guard": {
                "id": "stone_guard",
                "name": "Stone Guard",
                "side": "enemy",
                "hp": {"cur": 8, "max": 8},
                "defeated": False,
            },
        },
    }
    before = deepcopy(state)

    exact = resolve_combat_reference(state, "Stone")
    assert exact.status is CombatReferenceStatus.DEFEATED
    assert exact.selected is not None
    assert exact.selected.combatant_id == "stone"
    assert resolve_combatant(state, "Stone") is None
    assert state == before


def test_ordinal_head_fallback_is_single_word_only_and_compounds_refuse():
    state = _cohort_state("Baser Hollow", total=4, spawned=3)

    generic = resolve_combat_reference(state, "first hollow")
    assert generic.status is CombatReferenceStatus.RESOLVED
    assert generic.match_kind == "cohort_ordinal"
    assert generic.selected is not None
    assert generic.selected.label == "Baser Hollow #1"

    for token in ("first baser hollow", "first baser hollows"):
        exact = resolve_combat_reference(state, token)
        assert exact.status is CombatReferenceStatus.RESOLVED, (token, exact)
        assert exact.match_kind == "cohort_ordinal"
        assert exact.selected is not None
        assert exact.selected.label == "Baser Hollow #1"

    for token in (
        "first ash hollow",
        "first imaginary hollow",
        "first friendly ancient hollow",
    ):
        result = resolve_combat_reference(state, token)
        assert result.status is CombatReferenceStatus.UNKNOWN, (token, result)
        assert result.match_kind is None
        assert result.selected is None
        assert result.candidates == ()


def test_ordinal_compounds_and_cardinality_are_not_one_reference():
    cohort_state = _cohort_state()
    for token in (
        "first hollow and Hollowed #1",
        "first hollow and second hollow",
        "first hollow, second hollow",
    ):
        result = resolve_combat_reference(cohort_state, token)
        assert result.status is CombatReferenceStatus.UNKNOWN, (token, result)
        assert result.selected is None
        assert result.candidates == ()


def test_spaced_ordinals_are_exact_through_twenty_seventh():
    state = _cohort_state("Baser Hollow", total=27, spawned=27)
    spaced = (
        ("twenty first", 21),
        ("twenty second", 22),
        ("twenty third", 23),
        ("twenty fourth", 24),
        ("twenty fifth", 25),
        ("twenty sixth", 26),
        ("twenty seventh", 27),
    )
    for ordinal, index in spaced:
        result = resolve_combat_reference(state, f"{ordinal} baser hollow")
        assert result.status is CombatReferenceStatus.RESOLVED, (ordinal, result)
        assert result.match_kind == "cohort_ordinal"
        assert result.selected is not None
        assert result.selected.label == f"Baser Hollow #{index}"


def test_numeric_ordinal_suffixes_must_match_their_number():
    state = _cohort_state("Baser Hollow", total=27, spawned=27)
    valid_numeric = (
        ("1st", 1),
        ("2nd", 2),
        ("3rd", 3),
        ("4th", 4),
        ("11th", 11),
        ("12th", 12),
        ("13th", 13),
        ("21st", 21),
        ("22nd", 22),
        ("23rd", 23),
        ("27th", 27),
    )
    for ordinal, index in valid_numeric:
        result = resolve_combat_reference(state, f"{ordinal} baser hollow")
        assert result.status is CombatReferenceStatus.RESOLVED, (ordinal, result)
        assert result.selected is not None
        assert result.selected.label == f"Baser Hollow #{index}"

    for ordinal in ("1nd", "2st", "3th", "4rd", "11st", "12nd", "13rd", "21nd"):
        result = resolve_combat_reference(state, f"{ordinal} baser hollow")
        assert result.status is CombatReferenceStatus.UNKNOWN, (ordinal, result)
        assert result.match_kind is None
        assert result.selected is None
        assert result.candidates == ()


def test_reference_api_distinguishes_exact_defeated_and_queued_ordinals():
    state = _cohort_state()
    state["combat"]["combatants"]["hollowed"]["defeated"] = True
    state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 0

    defeated = resolve_combat_reference(state, "Hollowed #1")
    assert defeated.status is CombatReferenceStatus.DEFEATED
    assert defeated.selected is not None
    assert defeated.selected.combatant_id == "hollowed"
    assert defeated.selected.state == "defeated"
    assert resolve_combatant(state, "Hollowed #1") is None

    queued = resolve_combat_reference(state, "Hollowed #4")
    assert queued.status is CombatReferenceStatus.QUEUED
    assert queued.selected is not None
    assert queued.selected.combatant_id is None
    assert queued.selected.state == "queued"
    assert queued.selected.cohort_ref == "hollowed_x4"
    assert queued.selected.cohort_index == 4
    assert resolve_combatant(state, "Hollowed #4") is None


def _journaled_cohort(store: Store, branch: str) -> None:
    cohort = {
        "schema": "battle-cohort/1",
        "id": "hollowed_x4",
        "name": "Hollowed",
        "total": 4,
        "tier": "standard",
        "armament": "",
    }
    store.journal(branch, 1, 1, [
        {"op": "battle_start", "name": "Reference Field", "cohort": cohort, "_turn": 1},
        *(
            {
                "op": "combatant_spawn",
                "name": "Hollowed",
                "side": "enemy",
                "tier": "standard",
                "cohort_ref": "hollowed_x4",
                "cohort_index": index,
                "_turn": 1,
            }
            for index in (1, 2, 3)
        ),
    ], "rule")


def test_queue_advance_preserves_reference_identity_and_single_target_hp():
    store = Store(":memory:")
    _session, branch = store.create_session(external_id="combat-reference-queue")
    _journaled_cohort(store, branch)
    before = store.state_at(branch, 1, reduce_state, empty=empty_state())

    queued = resolve_combat_reference(before, "fourth hollow")
    assert queued.status is CombatReferenceStatus.QUEUED
    assert queued.selected is not None
    stable_identity = (
        queued.selected.label,
        queued.selected.cohort_ref,
        queued.selected.cohort_index,
        queued.selected.cohort_total,
    )

    store.journal(branch, 2, 2, [
        {"op": "combatant_hp", "target": "hollowed#2", "delta": -1, "_turn": 2},
        {"op": "combatant_hp", "target": "hollowed", "delta": -6, "_turn": 2},
        {"op": "combatant_defeat", "target": "hollowed", "_turn": 2},
        {
            "op": "combatant_spawn",
            "name": "Hollowed",
            "side": "enemy",
            "tier": "standard",
            "cohort_ref": "hollowed_x4",
            "cohort_index": 4,
            "_turn": 2,
        },
    ], "rule")
    after = store.state_at(branch, 2, reduce_state, empty=empty_state())

    initial_hp = before["combat"]["combatants"]["hollowed#2"]["hp"]
    assert after["combat"]["combatants"]["hollowed#2"]["hp"] == {
        "cur": initial_hp["cur"] - 1,
        "max": initial_hp["max"],
    }
    assert after["combat"]["combatants"]["hollowed#3"]["hp"] \
        == before["combat"]["combatants"]["hollowed#3"]["hp"]
    active = resolve_combat_reference(after, "fourth hollow")
    assert active.status is CombatReferenceStatus.RESOLVED
    assert active.selected is not None
    assert active.selected.combatant_id == "hollowed#4"
    assert (
        active.selected.label,
        active.selected.cohort_ref,
        active.selected.cohort_index,
        active.selected.cohort_total,
    ) == stable_identity
    assert resolve_combat_reference(after, "Hollowed #1").status \
        is CombatReferenceStatus.DEFEATED
    store.close()


def test_reference_results_survive_replay_reopen_fork_retry_and_duplicate_reads(tmp_path):
    db_path = tmp_path / "combat-reference.sqlite3"
    store = Store(db_path)
    _session, source = store.create_session(external_id="combat-reference-replay")
    messages = canonicalize([{"role": "user", "content": "Open the reference fight."}])
    chains = chain(messages)
    store.append_msgs(source, 0, [
        (message.role, message.content_hash, chain_hash)
        for message, chain_hash in zip(messages, chains)
    ])
    store.record_turn(source, 1, "new_turn", "normal")
    store.write_turn_hashes(source, 1, user_hash=messages[0].content_hash)
    _journaled_cohort(store, source)
    source_state = store.state_at(source, 1, reduce_state, empty=empty_state())
    expected = resolve_combat_reference(source_state, "second hollow")

    # Retry and duplicate transport reads are pure: no row or reference identity changes.
    before = deepcopy(source_state)
    assert resolve_combat_reference(source_state, "second hollow") == expected
    assert resolve_combat_reference(source_state, "second hollow") == expected
    assert source_state == before

    child = store.fork_branch(source, at_pos=1, fork_turn=1)
    child_state = store.state_at(child, 1, reduce_state, empty=empty_state())
    assert child_state == source_state
    assert resolve_combat_reference(child_state, "second hollow") == expected
    store.close()

    reopened = Store(db_path)
    reopened_source = reopened.state_at(source, 1, reduce_state, empty=empty_state())
    reopened_child = reopened.state_at(child, 1, reduce_state, empty=empty_state())
    assert reopened_source == source_state
    assert reopened_child == child_state
    assert resolve_combat_reference(reopened_source, "second hollow") == expected
    assert resolve_combat_reference(reopened_child, "second hollow") == expected
    reopened.close()


def test_candidate_projection_is_total_for_malformed_or_missing_state():
    assert combat_reference_candidates({}) == ()
    assert combat_reference_candidates({"combat": {"combatants": []}}) == ()
    assert resolve_combat_reference({}, None).status is CombatReferenceStatus.UNKNOWN
