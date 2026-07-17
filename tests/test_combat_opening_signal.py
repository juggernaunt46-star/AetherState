"""Pure combat-opening signal: narrow activation and adversarial abstention."""
from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.config import Config
from aetherstate.tier0 import (
    combat_opening_assessment,
    combat_opening_signal,
    combat_opening_target,
)


ROOT_K = (
    "I cross the first iron line, draw my longsword, and challenge Marshal Varo. "
    "He is the hostile first opponent; I hold position and watch him commit."
)


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.combat_opening_primer = True
    return cfg


def _state(*, present: bool = True, active: bool = False) -> dict:
    return {
        "entities": {
            "kael": {"kind": "player", "name": "Kael", "present": True},
            "varo": {
                "kind": "npc",
                "name": "Marshal Varo",
                "aliases": ["Varo"],
                "present": present,
            },
        },
        "player": {"kael": {}},
        "combat": {"active": active, "combatants": {}},
    }


def _doc(text: str, *, prior_user: str = "") -> dict:
    messages = []
    if prior_user:
        messages.extend([
            {"role": "user", "content": prior_user},
            {"role": "assistant", "content": "Earlier narration."},
        ])
    messages.append({"role": "user", "content": text})
    return {"messages": messages}


@pytest.mark.parametrize("klass", ["new_turn", "new_session"])
def test_root_k_grounded_hostile_challenge_signals_on_opening_request(klass: str):
    assert combat_opening_signal(_doc(ROOT_K), _state(), _cfg(), klass)
    assert combat_opening_target(_doc(ROOT_K), _state(), _cfg(), klass) \
        == ("varo", "Marshal Varo")


def test_opening_assessment_retains_the_exact_winning_source_coordinates():
    assessment = combat_opening_assessment(
        _doc(ROOT_K), _state(), _cfg(), "new_turn",
    )

    assert assessment.clause_index == 0
    assert assessment.action_span == (52, 61)
    assert ROOT_K[slice(*assessment.action_span)] == "challenge"
    assert ROOT_K[slice(*assessment.target_span)] == "Marshal Varo"


def test_later_opening_uses_its_real_clause_and_target_occurrence():
    text = (
        "I watch the gate. Marshal Varo is the hostile opponent. "
        "I draw my sword and challenge Marshal Varo."
    )
    assessment = combat_opening_assessment(
        _doc(text), _state(), _cfg(), "new_turn",
    )

    assert assessment.target == ("varo", "Marshal Varo")
    assert assessment.clause_index == 2
    assert text[slice(*assessment.action_span)] == "challenge"
    assert text[slice(*assessment.target_span)] == "Marshal Varo"
    assert assessment.target_span[0] > text.index("Marshal Varo")


def test_mechanical_target_is_independent_of_private_primer_knob():
    cfg = _cfg()
    cfg.specialization.combat_opening_primer = False

    assert not combat_opening_signal(_doc(ROOT_K), _state(), cfg, "new_turn")
    assert combat_opening_target(_doc(ROOT_K), _state(), cfg, "new_turn") \
        == ("varo", "Marshal Varo")


def test_direct_patient_beats_earlier_inserted_bystander_and_coordinated_targets_abstain():
    state = _state()
    # Put the bystander first to prove target choice is grammatical, never entity insertion order.
    state["entities"] = {
        "kael": state["entities"]["kael"],
        "zane": {"kind": "npc", "name": "Marshal Zane", "aliases": ["Zane"],
                 "present": True},
        "varo": state["entities"]["varo"],
    }

    bystander = _doc("I strike Varo while Zane watches from the gate.")
    ambiguous = _doc("I draw my sword and challenge Varo and Zane as hostile opponents.")

    assert combat_opening_target(bystander, state, _cfg(), "new_turn") \
        == ("varo", "Marshal Varo")
    assert combat_opening_target(ambiguous, state, _cfg(), "new_turn") is None
    assert not combat_opening_signal(ambiguous, state, _cfg(), "new_turn")


def test_grounded_direct_physical_ranged_and_magic_attacks_are_supported_transitions():
    for text in (
        "I strike Marshal Varo.",
        "I step inside his guard and strike Varo with the pommel.",
        "I cut Varo from shoulder to hip with my sword.",
        "I strike Varo's sword arm.",
        "I fire my crossbow at Marshal Varo.",
        "I cast a bolt of force from my wand at Marshal Varo.",
    ):
        assert combat_opening_signal(_doc(text), _state(), _cfg(), "new_turn")


def test_explicit_foe_command_signals_without_a_preexisting_target():
    doc = _doc("((aether.foe Glass Warden elite mirrored glaive)) I hold the doorway.")
    assert combat_opening_signal(doc, _state(), _cfg(), "new_turn")


def test_only_the_newest_user_message_can_signal():
    doc = _doc("I wait by the gate.", prior_user="((aether.foe Glass Warden elite))")
    assert not combat_opening_signal(doc, _state(), _cfg(), "new_turn")


@pytest.mark.parametrize(
    "text",
    [
        '"I challenge Marshal Varo to a duel," I say.',
        "If the talks fail, I would challenge Marshal Varo to a duel.",
        "I do not challenge Marshal Varo, even though he is the hostile opponent.",
        "I refuse to confront Marshal Varo with my sword drawn.",
        "I watch Marshal Varo's stance without attacking.",
        "I draw my longsword and challenge Marshal Zane, the hostile opponent.",
        "I challenge Marshal Varo's conclusion about the hostile opponent.",
        "I ask Marshal Varo whether he would duel me.",
    ],
)
def test_quoted_hypothetical_negated_unknown_and_abstract_mentions_abstain(text: str):
    assert not combat_opening_signal(_doc(text), _state(), _cfg(), "new_turn")


@pytest.mark.parametrize(
    "text",
    [
        "I fire Marshal Varo from his post.",
        "I duel Marshal Varo at chess.",
        "I shoot Marshal Varo a warning look.",
        "I remember attacking Marshal Varo years ago.",
    ],
)
def test_ambiguous_verbs_and_remembered_violence_need_present_physical_context(text: str):
    assert not combat_opening_signal(_doc(text), _state(), _cfg(), "new_turn")


@pytest.mark.parametrize(
    "text",
    [
        "I strike Varo from the guest list.",
        "I cut Varo from the team.",
        "I tackle Varo's paperwork.",
        "I attack paperwork Varo left.",
    ],
)
def test_known_npc_in_noncombat_target_role_does_not_signal(text: str):
    assert not combat_opening_signal(_doc(text), _state(), _cfg(), "new_turn")


def test_offstage_known_npc_is_not_a_grounded_natural_target():
    assert not combat_opening_signal(
        _doc(ROOT_K), _state(present=False), _cfg(), "new_turn")


@pytest.mark.parametrize(
    ("klass", "duplicate", "specialization", "war_room", "primer", "active"),
    [
        ("continue", False, "rpg", True, True, False),
        ("impersonate", False, "rpg", True, True, False),
        ("new_turn", True, "rpg", True, True, False),
        ("new_turn", False, "none", True, True, False),
        ("new_turn", False, "rpg", False, True, False),
        ("new_turn", False, "rpg", True, False, False),
        ("new_turn", False, "rpg", True, True, True),
    ],
)
def test_mode_turn_and_active_combat_gates(
        klass: str, duplicate: bool, specialization: str,
        war_room: bool, primer: bool, active: bool):
    cfg = _cfg()
    cfg.specialization.name = specialization
    cfg.specialization.war_room = war_room
    cfg.specialization.combat_opening_primer = primer
    assert not combat_opening_signal(
        _doc(ROOT_K), _state(active=active), cfg, klass, duplicate=duplicate)


def test_signal_is_read_only_over_request_and_state():
    doc = _doc(ROOT_K)
    state = _state()
    doc_before = deepcopy(doc)
    state_before = deepcopy(state)

    assert combat_opening_signal(doc, state, _cfg(), "new_turn")
    assert doc == doc_before
    assert state == state_before
