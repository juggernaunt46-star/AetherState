"""Source-bounded occurrence construction before canonical semantic frames."""
from __future__ import annotations

from copy import deepcopy
import random

import pytest

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.semantic_occurrence import (
    OccurrenceAnchor,
    OccurrenceGraphError,
    build_occurrence_graph,
    validate_occurrence_graph,
)
from aetherstate.state import empty_state


_AUTHORITY = {
    "issuer": "player",
    "channel": "player_input",
    "lifecycle_phase": "new_action",
    "grammar_version": "tier0-semantic/1",
    "operation_family": "semantic_interpretation",
}


def _anchor(text: str, kind: str, identity: str, phrase: str, *, after: int = 0,
            source: str | None = None) -> OccurrenceAnchor:
    start = text.index(phrase, after)
    return OccurrenceAnchor(
        kind, identity, start, start + len(phrase), source or f"test_{kind}",
    )


def _graph(text: str, anchors: list[OccurrenceAnchor], **authority) -> dict:
    return build_occurrence_graph(
        text,
        anchors=anchors,
        **{**_AUTHORITY, **authority},
    )


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.foe_floor = True
    cfg.specialization.intent_floor = True
    return cfg


def _state() -> dict:
    state = empty_state()
    state["entities"] = {
        "mage": {"kind": "player", "name": "Mage", "present": True},
        "ana": {"kind": "npc", "name": "Ana", "present": True},
        "bo": {"kind": "npc", "name": "Bo", "present": True},
        "bandit": {"kind": "npc", "name": "Bandit", "present": True},
    }
    state["player"] = {"mage": {
        "eid": "mage",
        "stats": {"INT": 16},
        "skills": {"burn": 4, "elementalism": 4, "praise": 2, "brawl": 3},
        "abilities": [],
        "resources": {},
        "defs": {"skills": {
            "burn": {"name": "Burn", "keyed_stat": "INT", "governs": ["burn"]},
            "elementalism": {
                "name": "Elementalism", "keyed_stat": "INT", "governs": ["rain"],
            },
            "praise": {"name": "Praise", "keyed_stat": "INT", "governs": ["praise"]},
            "brawl": {"name": "Brawl", "keyed_stat": "INT", "governs": ["strike"]},
        }},
    }}
    state["combat"] = {
        "active": True,
        "combatants": {
            eid: {"id": eid, "eid": eid, "name": name, "side": "enemy",
                  "defeated": False, "hp": {"cur": 14, "max": 14}}
            for eid, name in (("ana", "Ana"), ("bo", "Bo"), ("bandit", "Bandit"))
        },
    }
    return state


def _run(text: str):
    return _run_in_state(text, _state())


def _run_in_state(text: str, state: dict):
    return tier0.run(
        {"messages": [{"role": "user", "content": text}]},
        "new_turn", False, state, _cfg(), random.Random(7), turn=4,
    )


def _frames(result) -> list[dict]:
    return [op["frame"] for op in result.rule_ops
            if op.get("op") == "semantic_frame_commit"]


def _settlements(result) -> list[dict]:
    return [op for op in result.rule_ops if op.get("op") == "mechanic_settlement_commit"]


def _settled_hp(result) -> list[tuple[str, int]]:
    return [
        (str(member["target"]), int(member["delta"]))
        for wrapper in _settlements(result)
        for member in wrapper.get("members") or ()
        if member.get("op") == "combatant_hp"
    ]


def _mechanic_mutations(result) -> list[dict]:
    rows = [
        member
        for wrapper in _settlements(result)
        for member in wrapper.get("members") or ()
    ]
    rows.extend(
        op for op in result.rule_ops if op.get("op") != "mechanic_settlement_commit"
    )
    return [
        row for row in rows
        if row.get("op") in {"check", "combatant_hp", "master_tick"}
        or row.get("_cost")
        or row.get("_ability_cd")
    ]


def test_negated_burn_cannot_borrow_the_next_occurrences_target():
    result = _run("I don't burn Ana; I praise Bo.")
    burn = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert burn["polarity"] == "negative"
    assert burn["target_entity_id"] == "ana"
    assert burn["target_name"] == "Ana"
    assert not any(op.get("op") == "mechanic_settlement_commit"
                   and op.get("_semantic_frame_ref") == burn["fingerprint"]
                   for op in result.rule_ops)


def test_action_anchor_owns_polarity_when_structured_capability_precedes_it():
    text = "((aether.check melee)) I do not cut the revenant."
    graph = _graph(text, [
        _anchor(text, "capability", "melee", "melee"),
        _anchor(text, "action", "weapon_attack", "cut"),
    ])

    occurrence = next(node for node in graph["occurrences"] if node["actions"])
    assert occurrence["polarity"] == "negated"
    assert "occurrence.negated" in occurrence["unresolved_reasons"]


@pytest.mark.parametrize(
    ("companion", "expected_boundary"),
    [
        ("Do not burn Ana.", "negative"),
        ("I don't burn Ana.", "negative"),
        ("I might burn Ana.", "occurrence.actuality_unbound"),
        ("I plan not to burn Ana.", "negative"),
        ("I pretend to burn Ana.", "occurrence.actuality_unbound"),
        ('"I burn Ana."', "occurrence.quoted"),
        ("`I burn Ana.`", "occurrence.quoted"),
        ("```I burn Ana.```", "occurrence.quoted"),
    ],
    ids=(
        "imperative-negation",
        "contracted-negation",
        "possible",
        "negative-plan",
        "pretend",
        "quoted-speech",
        "inline-code",
        "fenced-code",
    ),
)
def test_explicit_check_cannot_override_companion_scope(
    companion: str,
    expected_boundary: str,
):
    result = _run(f"((aether.check burn)) {companion}")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    if expected_boundary == "negative":
        assert frame["polarity"] == "negative"
    else:
        assert expected_boundary in frame["ambiguity"]
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


def test_explicit_check_past_report_abstains_and_exact_present_command_still_executes():
    past = _run("((aether.check burn)) I burned Ana yesterday.")
    past_frame = next(frame for frame in _frames(past) if frame["capability_id"] == "burn")
    assert past_frame["time_scope"] == "past"
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in past.rule_ops)

    positive = _run("((aether.check burn)) I burn Ana.")
    positive_frame = next(
        frame for frame in _frames(positive) if frame["capability_id"] == "burn"
    )
    settlements = [
        op for op in positive.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == positive_frame["fingerprint"]
    ]
    assert positive_frame["polarity"] == "positive"
    assert positive_frame["modality"] == "command"
    assert positive_frame["time_scope"] == "current"
    assert positive_frame["ambiguity"] == []
    assert len(settlements) == 1


@pytest.mark.parametrize(
    "text",
    [
        "I refuse to burn Ana.",
        "I avoid burning Ana.",
        "I do anything except burn Ana.",
    ],
)
def test_negative_governors_cannot_turn_a_mentioned_capability_into_a_roll(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["polarity"] == "negative"
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "I choose not to burn Bo.",
        "I decide not to burn Bo.",
        "I try not to burn Bo.",
        "Not burn Bo.",
        "I am not burning Bo.",
        "I am not merely considering burning Bo.",
        "I am not only planning to burn Bo.",
    ],
    ids=(
        "choose-not-to",
        "decide-not-to",
        "try-not-to",
        "standalone-not",
        "progressive-not",
        "nonadjacent-not-merely",
        "nonadjacent-not-only",
    ),
)
def test_event_local_negation_forms_are_semantic_evidence_but_never_settle(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["polarity"] == "negative"
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


def test_negated_directed_harm_cannot_construct_or_settle_damage():
    result = _run("I choose not to rain fire on Bandit.")
    frame = next(
        frame for frame in _frames(result) if frame["capability_id"] == "elementalism"
    )

    assert frame["polarity"] == "negative"
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "bandit"
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )
    assert not any(
        member.get("op") == "combatant_hp"
        for op in result.rule_ops
        for member in ([op] if op.get("op") != "mechanic_settlement_commit"
                       else op.get("members") or ())
    )


def test_negation_scope_stops_at_the_occurrence_boundary():
    result = _run("I choose not to burn Ana; I burn Bo.")
    frames = [
        frame for frame in _frames(result) if frame["capability_id"] == "burn"
    ]

    assert [(frame["polarity"], frame["target_entity_id"]) for frame in frames] == [
        ("negative", "ana"),
        ("positive", "bo"),
    ]
    settled_refs = {
        op.get("_semantic_frame_ref")
        for op in result.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
    }
    assert settled_refs == {frames[1]["fingerprint"]}


def test_double_negation_is_unresolved_instead_of_simplified_into_authority():
    result = _run("I don't choose not to burn Bo.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["polarity"] == "unknown"
    assert "occurrence.polarity_unbound" in frame["ambiguity"]
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


@pytest.mark.parametrize(
    "text",
    [
        "I choose to burn Bo.",
        "I decide to burn Bo.",
        "I try to burn Bo.",
        "I am burning Bo.",
        "I do not merely burn Bo.",
        "I am not only burning Bo.",
    ],
    ids=(
        "choose-positive",
        "decide-positive",
        "try-positive",
        "progressive-positive",
        "not-merely-additive",
        "not-only-additive",
    ),
)
def test_legitimate_positive_forms_keep_occurrence_authority(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["polarity"] == "positive"
    assert "occurrence.polarity_unbound" not in frame["ambiguity"]
    assert any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


def test_negation_representation_does_not_weaken_quote_or_hypothesis_controls():
    quoted = _run('"I choose not to burn Bo."')
    assert _frames(quoted) == []
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in quoted.rule_ops)

    hypothetical = _run("If I choose not to burn Bo.")
    frame = next(
        frame for frame in _frames(hypothetical) if frame["capability_id"] == "burn"
    )
    assert frame["polarity"] == "negative"
    assert frame["modality"] == "hypothetical"
    assert not any(
        op.get("op") == "mechanic_settlement_commit" for op in hypothetical.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "I describe burning Bo.",
        "I discuss burning Bo.",
        "I recount burning Bo.",
        "I narrate burning Bo.",
        "I report that I burned Bo.",
        "I remember burning Bo.",
        "I recall burning Bo.",
        "I promise to burn Bo.",
        "I threaten to burn Bo.",
        "I order the flames to burn Bo.",
    ],
    ids=(
        "describe",
        "discuss",
        "recount",
        "narrate",
        "report",
        "remember",
        "recall",
        "promise",
        "threaten",
        "order",
    ),
)
def test_represented_or_assigned_inner_action_is_never_a_present_player_action(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert any(
        "occurrence.actuality_unbound" in node["unresolved_reasons"]
        for node in result.semantic_turn.occurrence_graph["occurrences"]
        if any(
            row["identity"] == frame["capability_id"]
            for row in node["capabilities"]
        )
    )
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


def test_mentioning_directed_harm_cannot_construct_or_settle_damage():
    result = _run("I mention raining fire on Bandit.")
    frame = next(
        frame for frame in _frames(result) if frame["capability_id"] == "elementalism"
    )

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "bandit"
    assert "occurrence.actuality_unbound" in frame["ambiguity"]
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)
    assert not any(
        member.get("op") == "combatant_hp"
        for op in result.rule_ops
        for member in ([op] if op.get("op") != "mechanic_settlement_commit"
                       else op.get("members") or ())
    )


def test_directive_does_not_lend_player_actor_authority_to_its_inner_action():
    result = _run("I tell Bo to burn Ana.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert "occurrence.actuality_unbound" in frame["ambiguity"]
    assert {"ana", "bo"}.issubset(set(frame["ambiguity"]))
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


def test_plan_keeps_future_time_while_remaining_nonperforming():
    result = _run("I plan to burn Bo.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["modality"] == "possible"
    assert frame["time_scope"] == "future"
    assert "occurrence.actuality_unbound" in frame["ambiguity"]
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


@pytest.mark.parametrize(
    ("text", "capability_phrase"),
    [
        ("I describe Bo.", "describe"),
        ("I promise Bo.", "promise"),
    ],
    ids=("describe-performative", "promise-performative"),
)
def test_governor_itself_remains_an_actual_performative(
    text: str,
    capability_phrase: str,
):
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "social", capability_phrase),
        _anchor(text, "target", "bo", "Bo"),
    ])

    assert graph["occurrences"][0]["actuality"] == "actual"
    assert "occurrence.actuality_unbound" not in graph["occurrences"][0][
        "unresolved_reasons"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "I mention fire, then I burn Bo.",
        "I remember Ana, then I burn Bo.",
        "I promise restraint, then I burn Bo.",
        "I tell Ana to wait, then I burn Bo.",
        "I describe a flame; I burn Bo.",
    ],
    ids=(
        "mention-then",
        "remember-then",
        "promise-then",
        "tell-then",
        "describe-semicolon",
    ),
)
def test_representation_governor_does_not_suppress_a_later_independent_action(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["polarity"] == "positive"
    assert frame["modality"] == "actual"
    assert frame["time_scope"] == "current"
    assert "occurrence.actuality_unbound" not in frame["ambiguity"]
    assert any(
        op.get("op") == "mechanic_settlement_commit"
        and op.get("_semantic_frame_ref") == frame["fingerprint"]
        for op in result.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "I mention fire, then rain fire on Bandit.",
        "I promise restraint, then rain fire on Bandit.",
    ],
    ids=("completed-mention", "completed-promise"),
)
def test_completed_outer_speech_act_does_not_capture_actorless_later_action(text: str):
    result = _run(text)

    assert len(_settlements(result)) == 1
    assert _settled_hp(result) == [("bandit", -2)]


def test_representation_governor_does_not_weaken_quote_or_hypothesis_controls():
    quoted = _run('"I mention burning Bo."')
    assert _frames(quoted) == []
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in quoted.rule_ops)

    hypothetical = _run("If I mention burning Bo.")
    frame = next(
        frame for frame in _frames(hypothetical) if frame["capability_id"] == "burn"
    )
    assert frame["modality"] == "hypothetical"
    assert not any(
        op.get("op") == "mechanic_settlement_commit" for op in hypothetical.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "> I burn Ana.",
        "I quote: I burn Ana.",
    ],
)
def test_markdown_or_explicitly_reported_quote_cannot_settle(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert "occurrence.quoted" in frame["ambiguity"]
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


def test_rain_fire_is_directed_or_explicitly_unresolved_never_a_complete_harmless_check():
    result = _run("I rain fire on Bandit.")
    frame = next(frame for frame in _frames(result)
                 if frame["capability_id"] == "elementalism")

    directed = frame["action_class"] == "weapon_attack" \
        and frame["target_entity_id"] == "bandit"
    explicit_unresolved = any(
        reason.startswith("occurrence.") for reason in frame["ambiguity"]
    )
    assert directed or explicit_unresolved
    if not directed:
        assert not any(op.get("op") == "mechanic_settlement_commit"
                       and op.get("_semantic_frame_ref") == frame["fingerprint"]
                       for op in result.rule_ops)


def test_compound_occurrences_keep_exact_actor_capability_and_target_bindings():
    text = "I burn Ana and I praise Bo."
    second_i = text.index("I", 1)
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
        OccurrenceAnchor("actor", "mage", second_i, second_i + 1, "test_actor"),
        _anchor(text, "capability", "praise", "praise"),
        _anchor(text, "target", "bo", "Bo"),
    ])

    assert graph["authority"]["allowed"] is True
    assert [node["occurrence_id"] for node in graph["occurrences"]] \
        == ["occurrence.1", "occurrence.2"]
    assert [{row["identity"] for row in node["capabilities"]}
            for node in graph["occurrences"]] == [{"burn"}, {"praise"}]
    assert [{row["identity"] for row in node["targets"]}
            for node in graph["occurrences"]] == [{"ana"}, {"bo"}]
    assert [{row["identity"] for row in node["actors"]}
            for node in graph["occurrences"]] == [{"mage"}, {"mage"}]


def test_missing_second_actor_is_explicit_and_never_borrowed_from_first_occurrence():
    text = "I burn Ana and praise Bo."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
        _anchor(text, "capability", "praise", "praise"),
        _anchor(text, "target", "bo", "Bo"),
    ])

    first, second = graph["occurrences"]
    assert {row["identity"] for row in first["actors"]} == {"mage"}
    assert second["actors"] == []
    assert "occurrence.actor_unbound" in second["unresolved_reasons"]


@pytest.mark.parametrize(
    ("text", "field", "reason"),
    [
        ("I don't burn Ana and praise Bo.", "polarity", "occurrence.polarity_unbound"),
        ("If I burn Ana and praise Bo.", "actuality", "occurrence.actuality_unbound"),
        (
            "Metaphorically, I burn Ana and praise Bo.",
            "actuality",
            "occurrence.actuality_unbound",
        ),
    ],
)
def test_shared_scope_is_never_lent_across_a_coordinated_occurrence(
    text: str,
    field: str,
    reason: str,
):
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
        _anchor(text, "capability", "praise", "praise"),
        _anchor(text, "target", "bo", "Bo"),
    ])

    second = graph["occurrences"][1]
    assert second[field] == "unknown"
    assert reason in second["unresolved_reasons"]


@pytest.mark.parametrize(
    ("authority", "reason"),
    [
        ({"issuer": "narrator", "channel": "player_input"},
         "issuer_channel_mismatch"),
        ({"issuer": "player", "channel": "narrator_reply"},
         "issuer_channel_mismatch"),
        ({"issuer": "narrator", "channel": "narrator_reply"},
         "producer_not_allowed"),
        ({"lifecycle_phase": "retry"}, "phase_not_allowed"),
        ({"grammar_version": "tier0-semantic/2"}, "grammar_not_allowed"),
        ({"operation_family": "state_admission"}, "operation_family_not_allowed"),
    ],
    ids=(
        "narrator-on-player-channel", "player-on-narrator-channel",
        "wrong-producer", "wrong-phase", "wrong-grammar", "wrong-operation",
    ),
)
def test_directional_authority_fails_closed_for_wrong_producer_channel_or_phase(
    authority: dict,
    reason: str,
):
    text = "I burn Ana."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
    ], **authority)

    assert graph["authority"]["allowed"] is False
    assert graph["authority"]["reason"] == reason
    assert f"occurrence.authority.{reason}" in graph["occurrences"][0][
        "unresolved_reasons"
    ]


@pytest.mark.parametrize(
    ("text", "actuality", "reason"),
    [
        ('"I burn Ana."', "quoted", "occurrence.quoted"),
        ("\u201cI burn Ana.\u201d", "quoted", "occurrence.quoted"),
        ("If I burn Ana.", "hypothetical", "occurrence.hypothetical"),
        ("Metaphorically, I burn Ana.", "metaphorical", "occurrence.metaphorical"),
    ],
)
def test_quoted_hypothetical_and_metaphorical_occurrences_are_explicit(
    text: str,
    actuality: str,
    reason: str,
):
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
    ])

    node = graph["occurrences"][0]
    assert node["actuality"] == actuality
    assert reason in node["unresolved_reasons"]


def test_multiple_targets_and_actions_are_visible_instead_of_silently_collapsed():
    targets = "I strike Ana and Bo."
    target_graph = _graph(targets, [
        _anchor(targets, "actor", "mage", "I"),
        _anchor(targets, "capability", "brawl", "strike"),
        _anchor(targets, "action", "weapon_attack", "strike"),
        _anchor(targets, "target", "ana", "Ana"),
        _anchor(targets, "target", "bo", "Bo"),
    ])
    assert "occurrence.multiple_targets" in target_graph["occurrences"][0][
        "unresolved_reasons"
    ]

    actions = "I inspect and strike Ana."
    action_graph = _graph(actions, [
        _anchor(actions, "actor", "mage", "I"),
        _anchor(actions, "capability", "focus", "inspect"),
        _anchor(actions, "action", "inspection", "inspect"),
        _anchor(actions, "action", "weapon_attack", "strike"),
        _anchor(actions, "target", "ana", "Ana"),
    ])
    assert "occurrence.multiple_actions" in action_graph["occurrences"][0][
        "unresolved_reasons"
    ]


def test_one_written_target_slot_with_two_identities_is_ambiguity_not_cardinality():
    text = "I strike Sentry."
    target_start = text.index("Sentry")
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "brawl", "strike"),
        _anchor(text, "action", "weapon_attack", "strike"),
        OccurrenceAnchor(
            "target", "north_sentry", target_start, target_start + len("Sentry"),
            "direct_patient",
        ),
        OccurrenceAnchor(
            "target", "south_sentry", target_start, target_start + len("Sentry"),
            "direct_patient",
        ),
    ])
    node = graph["occurrences"][0]

    assert {row["identity"] for row in node["targets"]} == {
        "north_sentry", "south_sentry",
    }
    assert {tuple(row["span"]) for row in node["targets"]} == {
        (target_start, target_start + len("Sentry")),
    }
    assert "occurrence.multiple_targets" not in node["unresolved_reasons"]


def test_graph_is_deterministic_and_rejects_cross_occurrence_field_provenance():
    text = "I burn Ana; I praise Bo."
    anchors = [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "burn", "burn"),
        _anchor(text, "target", "ana", "Ana"),
        _anchor(text, "capability", "praise", "praise"),
        _anchor(text, "target", "bo", "Bo"),
    ]
    first = _graph(text, anchors)
    second = _graph(text, list(reversed(anchors)))
    assert first == second

    tampered = deepcopy(first)
    tampered["occurrences"][0]["targets"][0]["span"] = list(
        tampered["occurrences"][1]["targets"][0]["span"]
    )
    with pytest.raises(OccurrenceGraphError, match="crosses its owning source boundary"):
        validate_occurrence_graph(tampered, source_text=text)


def test_tier0_positive_compound_frames_retain_only_their_own_targets():
    result = _run("I burn Ana; I praise Bo.")
    frames = {frame["capability_id"]: frame for frame in _frames(result)}

    assert frames["burn"]["target_entity_id"] == "ana"
    assert frames["praise"]["target_entity_id"] == "bo"
    assert frames["burn"]["context_frame"]["span_end"] \
        <= frames["praise"]["context_frame"]["span_start"]


def test_later_weapon_action_cannot_borrow_an_earlier_social_capability_and_target():
    result = _run("I praise Ana and strike.")
    praise = next(frame for frame in _frames(result) if frame["capability_id"] == "praise")

    assert praise["action_class"] == "weapon_attack"
    assert "occurrence.actuality_unbound" in praise["ambiguity"]
    assert not any(
        member.get("op") == "combatant_hp"
        for op in result.rule_ops
        if op.get("op") == "mechanic_settlement_commit"
        for member in op.get("members") or ()
    )


def test_metaphorical_capability_is_recorded_but_cannot_settle():
    result = _run("Metaphorically, I burn Ana.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert "occurrence.metaphorical" in frame["ambiguity"]
    assert not any(op.get("op") == "mechanic_settlement_commit"
                   and op.get("_semantic_frame_ref") == frame["fingerprint"]
                   for op in result.rule_ops)


def test_curly_quoted_capability_is_not_a_performed_player_action():
    result = _run("\u201cI burn Ana.\u201d")

    assert _frames(result) == []
    assert not any(op.get("op") == "mechanic_settlement_commit" for op in result.rule_ops)


def test_incidental_entity_mention_cannot_be_lent_as_an_attack_target():
    result = _run("I strike a bell beside Ana.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] is None
    assert frame["target_name"] is None
    assert "occurrence.target_unbound" in frame["ambiguity"]


def test_direct_attack_positive_control_keeps_its_exact_target():
    result = _run("I strike Ana.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")

    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "ana"
    assert frame["target_name"] == "Ana"
    assert "occurrence.target_unbound" not in frame["ambiguity"]


@pytest.mark.parametrize(
    "text",
    [
        "I clearly report raining fire on Bandit.",
        "I honestly report that I rain fire on Bandit.",
        "According to Bo, I rain fire on Bandit.",
        "The report says I rain fire on Bandit.",
        "I vividly remember raining fire on Bandit.",
        "My memory is that I rain fire on Bandit.",
        "I strongly believe I rain fire on Bandit.",
        "I am convinced I rain fire on Bandit.",
        "In my belief, I rain fire on Bandit.",
        "I secretly plan to rain fire on Bandit.",
        "My plan is to rain fire on Bandit.",
        "I am preparing to rain fire on Bandit.",
        "I solemnly promise to rain fire on Bandit.",
        "I have promised to rain fire on Bandit.",
        "I made a solemn promise to rain fire on Bandit.",
        "I angrily threaten to rain fire on Bandit.",
        "I issue a threat to rain fire on Bandit.",
        "I issue an order to rain fire on Bandit.",
        "I sternly order Bo to rain fire on Bandit.",
        "I tell Bo to rain fire on Bandit.",
        "I cannot rain fire on Bandit.",
        "I am unable to rain fire on Bandit.",
        "I fail to rain fire on Bandit.",
        "Were I to rain fire on Bandit.",
        "Had I chosen to rain fire on Bandit.",
        "Should I rain fire on Bandit.",
        "I can rain fire on Bandit.",
        "I could rain fire on Bandit.",
        "I may rain fire on Bandit.",
        "I might rain fire on Bandit.",
        "I should rain fire on Bandit.",
        "I would rain fire on Bandit.",
        "I will rain fire on Bandit.",
        "I shall rain fire on Bandit.",
        "I am going to rain fire on Bandit.",
        "I am about to rain fire on Bandit.",
        "I must rain fire on Bandit.",
        "I need to rain fire on Bandit.",
        "I want to rain fire on Bandit.",
        "I ought to rain fire on Bandit.",
        "I have to rain fire on Bandit.",
        "I am supposed to rain fire on Bandit.",
        "I almost rain fire on Bandit.",
        "I am incapable of raining fire on Bandit.",
        "I am prevented from raining fire on Bandit.",
        "Maybe I rain fire on Bandit.",
        "In case I rain fire on Bandit.",
        "In metaphor, I rain fire on Bandit.",
        "As a figure of speech, I rain fire on Bandit.",
        "I represent raining fire on Bandit.",
        "I simulate raining fire on Bandit.",
        "I mime raining fire on Bandit.",
        "I depict myself raining fire on Bandit.",
        "I portray myself raining fire on Bandit.",
        "I roleplay raining fire on Bandit.",
        "I act like I rain fire on Bandit.",
        "I am pretending to rain fire on Bandit.",
        "I forget raining fire on Bandit.",
        "I relive raining fire on Bandit.",
        "I used to rain fire on Bandit.",
        "I have rained fire on Bandit before.",
        "I had rained fire on Bandit.",
        "I was raining fire on Bandit.",
        "I did rain fire on Bandit.",
        "I rained fire on Bandit.",
        "I deliberately rained fire on Bandit.",
        "Yesterday I rain fire on Bandit.",
        "I rain fire on Bandit earlier.",
        "I rain fire on Bandit an hour ago.",
        "I rain fire on Bandit before.",
        "I aim to rain fire on Bandit.",
        "I schedule myself to rain fire on Bandit.",
        "I expect to rain fire on Bandit.",
        "I am set to rain fire on Bandit.",
        "I am fixing to rain fire on Bandit.",
        "I pledge to rain fire on Bandit.",
        "I give my word that I rain fire on Bandit.",
        "I guarantee I will rain fire on Bandit.",
        "Next turn I rain fire on Bandit.",
        "Later I rain fire on Bandit.",
        "Soon I rain fire on Bandit.",
        "At dawn I rain fire on Bandit.",
        "In an hour I rain fire on Bandit.",
        "After Bo moves, I rain fire on Bandit.",
        "Once Bo moves, I rain fire on Bandit.",
    ],
)
def test_clause_local_nonperformance_matrix_has_zero_settlement_and_zero_hp(
    text: str,
):
    result = _run(text)
    graph = result.semantic_turn.occurrence_graph
    nodes = [
        node for node in graph["occurrences"]
        if any(row["identity"] == "elementalism" for row in node["capabilities"])
    ]

    assert nodes
    assert all(
        node["polarity"] != "affirmative" or node["actuality"] != "actual"
        for node in nodes
    )
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


@pytest.mark.parametrize(
    "companion",
    [
        "I vividly remember raining fire on Bandit.",
        "I can rain fire on Bandit.",
        "I will rain fire on Bandit.",
        "Were I to rain fire on Bandit.",
        "In metaphor, I rain fire on Bandit.",
        "I cannot rain fire on Bandit.",
        "I promise to rain fire on Bandit.",
        "I order Bo to rain fire on Bandit.",
        "I must rain fire on Bandit.",
        "I need to rain fire on Bandit.",
        "I want to rain fire on Bandit.",
        "I am incapable of raining fire on Bandit.",
        "I am prevented from raining fire on Bandit.",
        "I almost rain fire on Bandit.",
        "I rained fire on Bandit.",
        "I was raining fire on Bandit.",
        "I had rained fire on Bandit.",
        "Yesterday I rain fire on Bandit.",
    ],
)
def test_structured_check_uses_the_same_nonactual_companion_owner(companion: str):
    result = _run(f"((aether.check elementalism)) {companion}")

    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []
    frame = next(
        frame for frame in _frames(result) if frame["capability_id"] == "elementalism"
    )
    assert frame["modality"] != "command" or frame["polarity"] != "positive" \
        or frame["ambiguity"]


@pytest.mark.parametrize(
    "text",
    [
        "I cannot not rain fire on Bandit.",
        "I do not fail to rain fire on Bandit.",
        "I do not refuse to rain fire on Bandit.",
        "I never fail to rain fire on Bandit.",
    ],
)
def test_double_negative_never_simplifies_into_damage_authority(text: str):
    result = _run(text)
    frame = next(
        frame for frame in _frames(result) if frame["capability_id"] == "elementalism"
    )

    assert frame["polarity"] != "positive"
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    "text",
    [
        "I rain fire on Bandit.",
        "I try to rain fire on Bandit.",
        "I cast rain fire on Bandit.",
        "I choose to rain fire on Bandit.",
        "I am raining fire on Bandit.",
        "I cannot help but rain fire on Bandit.",
        "I begin to rain fire on Bandit.",
        "I start raining fire on Bandit.",
        "I currently rain fire on Bandit.",
        "I not only rain fire on Bandit.",
        "I do not merely rain fire on Bandit.",
        "((aether.check elementalism)) I rain fire on Bandit.",
        "((aether.check elementalism)) I try to rain fire on Bandit.",
        "((aether.check elementalism)) I cannot help but rain fire on Bandit.",
        "((aether.check elementalism)) I am not only raining fire on Bandit.",
    ],
)
def test_current_direct_and_additive_positive_controls_keep_exact_damage(text: str):
    result = _run(text)

    assert len(_settlements(result)) == 1
    assert _settled_hp(result) == [("bandit", -2)]


@pytest.mark.parametrize(
    "surface",
    [
        "used", "died", "tied", "bled", "blew", "dug", "fed", "fell", "fled",
        "flew", "forgot", "froze", "heard", "hid", "knew", "laid", "led", "lit",
        "paid", "said", "sang", "sank", "sat", "saw", "shook", "slept", "slid",
        "spent", "spun", "stood", "struck", "swam", "swept", "taught", "thought",
        "threw", "woke", "wrote",
    ],
)
def test_unambiguous_irregular_past_surface_is_not_current_authority(surface: str):
    text = f"I {surface} Bo."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "test_capability", surface),
        _anchor(text, "target", "bo", "Bo"),
    ])
    node = graph["occurrences"][0]

    assert node["actuality"] == "unknown"
    assert "occurrence.actuality_unbound" in node["unresolved_reasons"]


@pytest.mark.parametrize(
    "surface", ["cast", "hit", "cut", "proceed", "succeed", "exceed"],
)
def test_present_compatible_surface_needs_separate_past_evidence(surface: str):
    text = f"I {surface} Bo."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "test_capability", surface),
        _anchor(text, "target", "bo", "Bo"),
    ])
    node = graph["occurrences"][0]

    assert node["actuality"] == "actual"
    assert "occurrence.actuality_unbound" not in node["unresolved_reasons"]


@pytest.mark.parametrize("action", ["cast", "use"])
@pytest.mark.parametrize("capability", ["Burned Earth", "Speed"])
def test_capability_name_spelling_is_not_mistaken_for_past_tense(
    action: str,
    capability: str,
):
    text = f"I {action} {capability} on Bo."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "action", "skill_check", action),
        _anchor(
            text, "capability", "test_capability", capability,
            source="candidate_named",
        ),
        _anchor(text, "target", "bo", "Bo"),
    ])
    node = graph["occurrences"][0]

    assert node["actuality"] == "actual"
    assert "occurrence.actuality_unbound" not in node["unresolved_reasons"]


@pytest.mark.parametrize(
    ("text", "expected_hp"),
    [
        (
            "I rained fire on Ana, then I rain fire on Bandit.",
            [("bandit", -2)],
        ),
        (
            "I rained fire on Ana, then rain fire on Bandit.",
            [],
        ),
        (
            "I rain fire on Ana, then I rained fire on Bandit.",
            [("ana", -2)],
        ),
        (
            "((aether.check elementalism)) I rained fire on Ana, then "
            "((aether.check elementalism)) I rain fire on Bandit.",
            [("bandit", -2)],
        ),
        (
            "((aether.check elementalism)) I rained fire on Ana, then "
            "((aether.check elementalism)) rain fire on Bandit.",
            [],
        ),
        (
            "((aether.check elementalism)) I rain fire on Ana, then "
            "((aether.check elementalism)) I rained fire on Bandit.",
            [("ana", -2)],
        ),
    ],
    ids=(
        "past-then-explicit-current",
        "past-then-actorless-inherits",
        "current-then-explicit-past",
        "structured-past-then-explicit-current",
        "structured-past-then-actorless-inherits",
        "structured-current-then-explicit-past",
    ),
)
def test_past_and_current_clauses_remain_occurrence_local(
    text: str,
    expected_hp: list[tuple[str, int]],
):
    result = _run(text)

    assert _settled_hp(result) == expected_hp
    assert len(_settlements(result)) == len(expected_hp)


@pytest.mark.parametrize(
    "text",
    [
        "I vividly remember raining fire on Ana; I rain fire on Bandit.",
        "I vividly remember raining fire on Ana, but I rain fire on Bandit.",
        "I vividly remember raining fire on Ana and then I rain fire on Bandit.",
        "I vividly remember raining fire on Ana and I rain fire on Bandit.",
    ],
)
def test_clear_renewed_clause_settles_only_its_independent_current_action(text: str):
    result = _run(text)

    assert len(_settlements(result)) == 1
    assert _settled_hp(result) == [("bandit", -2)]


@pytest.mark.parametrize(
    "text",
    [
        "I vividly remember raining fire on Ana and rain fire on Bandit.",
        "I solemnly promise restraint and rain fire on Bandit.",
        "I issue a warning and rain fire on Bandit.",
    ],
)
def test_ambiguous_same_subject_coordination_fails_closed(text: str):
    result = _run(text)

    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    "text",
    [
        "I force Bo to rain fire on Bandit.",
        "I have Bo rain fire on Bandit.",
        "I get Bo to rain fire on Bandit.",
        "I compel Bo to rain fire on Bandit.",
        "I require Bo to rain fire on Bandit.",
        "Let Bo rain fire on Bandit.",
    ],
)
def test_causative_or_directive_never_lends_player_actor_to_inner_action(text: str):
    result = _run(text)
    frame = next(
        frame for frame in _frames(result) if frame["capability_id"] == "elementalism"
    )

    assert "occurrence.actuality_unbound" in frame["ambiguity"]
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    "text",
    [
        "I will rain fire on Ana, then rain fire on Bandit.",
        "I plan to rain fire on Ana, then rain fire on Bandit.",
        "I promise to rain fire on Ana, then rain fire on Bandit.",
        "Tomorrow I rain fire on Ana, then rain fire on Bandit.",
    ],
)
def test_actorless_continuation_inherits_nonactual_or_future_scope(text: str):
    result = _run(text)

    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []
    nodes = [
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    ]
    assert len(nodes) == 2
    assert nodes[1]["actuality"] == "unknown"
    assert "occurrence.actuality_unbound" in nodes[1]["unresolved_reasons"]


@pytest.mark.parametrize(
    "text",
    [
        "I will rain fire on Ana, then I rain fire on Bandit.",
        "I plan to rain fire on Ana, then I rain fire on Bandit.",
        "Tomorrow I rain fire on Ana, then I rain fire on Bandit.",
        "I remember raining fire on Ana. I rain fire on Bandit.",
        "I remember raining fire on Ana; I rain fire on Bandit.",
    ],
)
def test_explicit_or_hard_boundary_renews_current_player_action(text: str):
    result = _run(text)

    assert len(_settlements(result)) == 1
    assert _settled_hp(result) == [("bandit", -2)]


@pytest.mark.parametrize(
    "text",
    [
        "I remember raining fire on Ana, and now I rain fire on Bandit.",
        "I plan to rain fire on Ana, and now I rain fire on Bandit.",
    ],
    ids=("remember-and-now", "plan-and-now"),
)
def test_explicit_and_now_clause_renews_current_player_action(text: str):
    result = _run(text)

    assert len(_settlements(result)) == 1
    assert _settled_hp(result) == [("bandit", -2)]


def test_conditional_if_then_keeps_scope_despite_repeated_player_subject():
    result = _run("If I rain fire on Ana, then I rain fire on Bandit.")

    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []
    nodes = [
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    ]
    assert len(nodes) == 2
    assert nodes[0]["actuality"] == "hypothetical"
    assert nodes[1]["actuality"] == "unknown"


def test_completed_embedded_question_does_not_taint_following_then_action():
    text = "I ask who burned Ana, then burn Bo."
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "action", "communication", "ask"),
        _anchor(text, "capability", "burn", "burned"),
        _anchor(text, "target", "ana", "Ana"),
        _anchor(text, "capability", "burn", "burn", after=text.index("then")),
        _anchor(text, "target", "bo", "Bo"),
    ])

    assert [node["actuality"] for node in graph["occurrences"]] == ["unknown", "actual"]


@pytest.mark.parametrize(
    ("text", "actuality"),
    [
        ("I cut Bo before.", "unknown"),
        ("I cut Bo before Ana moves.", "actual"),
    ],
    ids=("trailing-prior-time", "subordinate-completion-window"),
)
def test_before_scope_distinguishes_prior_time_from_current_completion_window(
    text: str,
    actuality: str,
):
    graph = _graph(text, [
        _anchor(text, "actor", "mage", "I"),
        _anchor(text, "capability", "brawl", "cut"),
        _anchor(text, "action", "weapon_attack", "cut"),
        _anchor(text, "target", "bo", "Bo"),
    ])

    assert graph["occurrences"][0]["actuality"] == actuality


def test_structured_spell_object_noun_does_not_override_visible_action_actuality():
    text = (
        "((aether.check reality_shaping)) I speak the unmaking word and erase Bo."
    )
    graph = _graph(text, [
        _anchor(
            text,
            "capability",
            "reality_shaping",
            "reality_shaping",
            source="candidate_explicit",
        ),
        _anchor(text, "actor", "mage", "I"),
        _anchor(
            text,
            "capability",
            "reality_shaping",
            "unmaking",
            source="candidate_inferred",
        ),
        _anchor(
            text,
            "capability",
            "reality_shaping",
            "erase",
            source="candidate_inferred",
        ),
        _anchor(text, "action", "destruction", "erase"),
        _anchor(text, "target", "bo", "Bo"),
    ])

    assert graph["occurrences"][0]["actuality"] == "actual"


def test_roleplay_past_admits_only_low_impact_targetless_check():
    state = _state()
    player = state["player"]["mage"]
    player["skills"]["stealth"] = 2
    player["defs"]["skills"]["stealth"] = {
        "name": "Stealth", "keyed_stat": "INT", "governs": ["sneak"],
    }
    safe = tier0.run(
        {"messages": [{"role": "user", "content": "I sneaked past the sentries."}]},
        "new_turn",
        False,
        state,
        _cfg(),
        random.Random(7),
        turn=4,
    )
    harmful = _run("I burned Ana.")

    safe_frame = next(
        frame for frame in _frames(safe) if frame["capability_id"] == "stealth"
    )
    harmful_frame = next(
        frame for frame in _frames(harmful) if frame["capability_id"] == "burn"
    )
    assert safe_frame["ambiguity"] == []
    assert len(_settlements(safe)) == 1
    assert "occurrence.actuality_unbound" in harmful_frame["ambiguity"]
    assert _settlements(harmful) == []
    assert _mechanic_mutations(harmful) == []


@pytest.mark.parametrize(
    ("companion", "node_polarity", "frame_polarity"),
    [
        ("I burn Ana and Bo.", "affirmative", "positive"),
        ("I burn both Ana and Bo.", "affirmative", "positive"),
        ("I burn Ana or Bo.", "affirmative", "positive"),
        ("I burn either Ana or Bo.", "affirmative", "positive"),
        ("I burn neither Ana nor Bo.", "negated", "negative"),
        ("I burn not only Ana but Bo.", "affirmative", "positive"),
    ],
    ids=("and", "both-and", "or", "either-or", "neither-nor", "not-only-but"),
)
@pytest.mark.parametrize("structured", [False, True], ids=("natural", "structured"))
def test_correlative_multi_target_is_explicitly_unresolved_and_never_settles(
    companion: str,
    node_polarity: str,
    frame_polarity: str,
    structured: bool,
):
    text = f"((aether.check burn)) {companion}" if structured else companion
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert "occurrence.multiple_targets" in node["unresolved_reasons"]
    assert {row["identity"] for row in node["targets"]} == {"ana", "bo"}
    assert node["polarity"] == node_polarity
    assert frame["polarity"] == frame_polarity
    assert frame["target_entity_id"] is None
    assert {"ana", "bo"}.issubset(frame["ambiguity"])
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    ("companion", "node_polarity", "frame_polarity"),
    [
        ("I strike Ana and Ana.", "affirmative", "positive"),
        ("I strike both Ana and Ana.", "affirmative", "positive"),
        ("I strike Ana or Ana.", "affirmative", "positive"),
        ("I strike either Ana or Ana.", "affirmative", "positive"),
        ("I strike neither Ana nor Ana.", "negated", "negative"),
        ("I strike not only Ana but Ana.", "affirmative", "positive"),
    ],
    ids=("and", "both-and", "or", "either-or", "neither-nor", "not-only-but"),
)
@pytest.mark.parametrize("structured", [False, True], ids=("natural", "structured"))
def test_same_entity_coordinated_target_bindings_never_collapse_to_one_patient(
    companion: str,
    node_polarity: str,
    frame_polarity: str,
    structured: bool,
):
    text = f"((aether.check brawl)) {companion}" if structured else companion
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")
    action_frame = next(
        frame for frame in result.semantic_turn.frames if frame.capability_id == "brawl"
    )
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert [row["identity"] for row in node["targets"]] == ["ana", "ana"]
    assert [row["source"] for row in node["targets"]] == [
        "direct_patient", "coordinated_patient",
    ]
    assert len({tuple(row["span"]) for row in node["targets"]}) == 2
    assert "occurrence.multiple_targets" in node["unresolved_reasons"]
    assert node["polarity"] == node_polarity
    assert frame["polarity"] == frame_polarity
    assert "occurrence.multiple_targets" in frame["ambiguity"]
    assert action_frame.mechanically_actionable is False
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


_ADDITIVE_TARGET_FORMS = (
    ("{left} and also {right}", "coordinated_patient", False),
    ("{left} but also {right}", "coordinated_patient", False),
    ("{left} as well as {right}", "coordinated_patient", False),
    ("{left} along with {right}", "ambiguous_target_scope", True),
    ("{left} together with {right}", "ambiguous_target_scope", True),
    ("{left} in addition to {right}", "coordinated_patient", False),
    ("{left} plus {right}", "coordinated_patient", False),
    ("{left} + {right}", "coordinated_patient", False),
    ("{left} alongside {right}", "ambiguous_target_scope", True),
    ("{left} & {right}", "coordinated_patient", False),
    ("{left} and/or {right}", "coordinated_patient", False),
    ("{left} and-or {right}", "coordinated_patient", False),
    ("{left} or else {right}", "ambiguous_target_scope", True),
    ("{left} and then {right}", "ambiguous_target_scope", True),
    ("{left}, {right}", "ambiguous_target_scope", True),
    ("{left}, also {right}", "coordinated_patient", False),
    ("{left}, additionally {right}", "coordinated_patient", False),
)


@pytest.mark.parametrize(
    ("target_form", "second_source", "scope_unbound"),
    _ADDITIVE_TARGET_FORMS,
    ids=(
        "and-also", "but-also", "as-well-as", "along-with", "together-with",
        "in-addition-to", "plus-word", "plus-symbol", "alongside", "ampersand",
        "and-slash-or", "and-or", "or-else", "and-then", "comma-list", "comma-also",
        "comma-additionally",
    ),
)
@pytest.mark.parametrize("structured", [False, True], ids=("natural", "structured"))
@pytest.mark.parametrize(
    ("left", "right", "expected_ids", "alias"),
    [
        ("Ana", "Ana", ["ana", "ana"], False),
        ("Ana", "Bo", ["ana", "bo"], False),
        ("Ana", "Crimson", ["ana", "ana"], True),
    ],
    ids=("same-identity", "distinct-targets", "alias-same-identity"),
)
def test_additive_target_family_never_silently_selects_one_patient(
    target_form: str,
    second_source: str,
    scope_unbound: bool,
    structured: bool,
    left: str,
    right: str,
    expected_ids: list[str],
    alias: bool,
):
    state = _state()
    if alias:
        state["entities"]["ana"]["aliases"] = ["Crimson"]
    companion = f"I strike {target_form.format(left=left, right=right)}."
    text = f"((aether.check brawl)) {companion}" if structured else companion
    result = _run_in_state(text, state)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")
    action_frame = next(
        frame for frame in result.semantic_turn.frames if frame.capability_id == "brawl"
    )
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert [row["identity"] for row in node["targets"]] == expected_ids
    assert [row["source"] for row in node["targets"]] == [
        "direct_patient", second_source,
    ]
    assert len({tuple(row["span"]) for row in node["targets"]}) == 2
    assert "occurrence.multiple_targets" in node["unresolved_reasons"]
    assert ("occurrence.target_scope_unbound" in node["unresolved_reasons"]) \
        is scope_unbound
    assert "occurrence.multiple_targets" in frame["ambiguity"]
    assert action_frame.mechanically_actionable is False
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    "text",
    [
        "I strike Ana alongside Bo as Bo guards.",
        "I strike Ana together with Bo while Bo watches.",
        "I strike Ana along with Bo who helps.",
        "I strike Ana or else Bo attacks.",
        "I strike Ana and also Bo blocks.",
        "I strike Ana and also Bo's sword.",
        "I strike Ana plus Bo in the doorway.",
        "I strike Ana as well as Bo, the guard.",
        "I strike Ana + Bo blocks.",
        "I strike Ana, also Bo blocks.",
        "I strike Ana, additionally Bo blocks.",
    ],
)
def test_ambiguous_additive_or_event_tail_refuses_instead_of_selecting_first(text: str):
    result = _run(text)

    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    ("text", "expected_settlements"),
    [
        ("I strike Ana but not Bo.", 1),
        ("I strike Ana except Bo.", 1),
        ("I strike Ana instead of Bo.", 1),
        ("I strike Ana, and Ana leaves.", 1),
        ("I strike Ana and then I praise Bo.", 2),
    ],
)
def test_exclusion_renewed_subject_and_independent_actions_remain_separate(
    text: str,
    expected_settlements: int,
):
    result = _run(text)

    assert len(_settlements(result)) == expected_settlements
    assert _settled_hp(result) == [("ana", -2)]


def test_noncoordinated_repeated_target_identity_remains_one_exact_patient():
    result = _run("I strike Ana in Ana's ribs.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")
    action_frame = next(
        frame for frame in result.semantic_turn.frames if frame.capability_id == "brawl"
    )
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert [row["identity"] for row in node["targets"]] == ["ana", "ana"]
    assert [row["source"] for row in node["targets"]] == [
        "direct_patient", "body_locus_owner",
    ]
    assert len({tuple(row["span"]) for row in node["targets"]}) == 2
    assert "occurrence.multiple_targets" not in node["unresolved_reasons"]
    assert frame["target_entity_id"] == "ana"
    assert frame["target_locus"] == "ribs"
    assert frame["target_locus_owner_id"] == "ana"
    assert frame["ambiguity"] == []
    assert action_frame.mechanically_actionable is True
    assert len(_settlements(result)) == 1
    assert _settled_hp(result)


def test_named_subject_after_coordination_is_not_rebound_as_a_second_patient():
    result = _run("I report that I strike Ana, and Ana leaves.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "brawl")
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert [(row["identity"], row["source"]) for row in node["targets"]] == [
        ("ana", "direct_patient"),
    ]
    assert "occurrence.multiple_targets" not in node["unresolved_reasons"]
    assert frame["target_entity_id"] == "ana"
    assert "occurrence.multiple_targets" not in frame["ambiguity"]
    assert _settlements(result) == []
    assert _settled_hp(result) == []
    assert _mechanic_mutations(result) == []


@pytest.mark.parametrize(
    "text",
    ["I burn Ana.", "((aether.check burn)) I burn Ana."],
    ids=("natural", "structured"),
)
def test_single_grounded_target_still_settles_once(text: str):
    result = _run(text)
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")
    node = next(
        node for node in result.semantic_turn.occurrence_graph["occurrences"]
        if node["capabilities"]
    )

    assert {row["identity"] for row in node["targets"]} == {"ana"}
    assert "occurrence.multiple_targets" not in node["unresolved_reasons"]
    assert frame["target_entity_id"] == "ana"
    assert frame["ambiguity"] == []
    assert len(_settlements(result)) == 1
    assert _mechanic_mutations(result)


def test_targetless_explicit_check_without_grounded_entity_still_settles_once():
    result = _run("((aether.check burn)) I inspect the sigils.")
    frame = next(frame for frame in _frames(result) if frame["capability_id"] == "burn")

    assert frame["target_entity_id"] is None
    assert frame["ambiguity"] == []
    assert len(_settlements(result)) == 1
    assert _mechanic_mutations(result)


def test_additive_actions_remain_two_independent_occurrences():
    result = _run("I not only burn Ana but praise Bo.")
    frames = {
        frame["capability_id"]: frame for frame in _frames(result)
        if frame["capability_id"] in {"burn", "praise"}
    }

    assert [(frames[key]["target_entity_id"], frames[key]["polarity"])
            for key in ("burn", "praise")] == [("ana", "positive"), ("bo", "positive")]
    assert len(_settlements(result)) == 2
