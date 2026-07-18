"""Sanitized regressions for harmful Player magic discovered in the Éranmor session audit."""
from __future__ import annotations

import random

import pytest

from aetherstate import tier0
from aetherstate.config import Config
from aetherstate.state import _COMBAT_REFERENCE_ORDINALS, empty_state


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.war_room = True
    cfg.specialization.foe_floor = True
    cfg.specialization.intent_floor = True
    return cfg


def _state() -> dict:
    state = empty_state()
    state["entities"]["mage"] = {
        "kind": "player", "name": "Mage", "present": True, "aliases": [],
    }
    state["entities"]["iven"] = {
        "kind": "npc", "name": "Iven", "present": True, "aliases": [],
    }
    state["player"] = {"mage": {
        "eid": "mage",
        "stats": {"INT": 16},
        "skills": {"elementalism": 4, "voidweaving": 4, "holloweaving": 4},
        "abilities": ["ice_focus", "hollow_focus"],
        "resources": {"mana": {"name": "Mana", "max": 30, "cur": 30}},
        "_resource_cost_policy": "strict/1",
        "defs": {
            "skills": {
                "elementalism": {
                    "name": "Elementalism", "keyed_stat": "INT",
                    "governs": ["rain"], "cost": {"mana": 1},
                },
                "voidweaving": {
                    "name": "Voidweaving", "keyed_stat": "INT",
                    "governs": ["destabilize"], "cost": {"mana": 1},
                },
                "holloweaving": {
                    "name": "Holloweaving", "keyed_stat": "INT",
                    "governs": ["holloweaving"], "cost": {"mana": 1},
                },
            },
            "abilities": {
                "ice_focus": {
                    "name": "Ice Focus", "kind": "active", "mechanic": "surge",
                    "magnitude": 1, "applies_to": "elementalism",
                    "cost": {"mana": 2}, "cooldown_turns": 2,
                },
                "hollow_focus": {
                    "name": "Hollow Focus", "kind": "active", "mechanic": "mod",
                    "magnitude": 1, "applies_to": "holloweaving",
                    "cost": {"mana": 2}, "cooldown_turns": 2,
                },
            },
        },
    }}
    return state


def _run(text: str):
    return _run_state(text, _state())


class _Rig:
    def randint(self, _minimum: int, _maximum: int) -> int:
        return 6


def _run_state(text: str, state: dict, rng=None):
    return tier0.run(
        {"messages": [
            {"role": "assistant", "content": "Iven braces for the Mage's next move."},
            {"role": "user", "content": text},
        ]},
        "new_turn",
        False,
        state,
        _cfg(),
        rng or random.Random(7),
    )


def _cohort_state() -> dict:
    state = _state()
    state["entities"].pop("iven")
    state["battle"] = {
        "active": True,
        "name": "Reference Field",
        "cohort": {
            "schema": "battle-cohort/1",
            "id": "hollowed_x4",
            "name": "Hollowed",
            "total": 4,
            "tier": "standard",
            "armament": "",
            "spawned": 3,
            "remaining": 1,
        },
    }
    state["combat"] = {
        "active": True,
        "combatants": {
            "hollowed": {
                "id": "hollowed", "name": "Hollowed", "side": "enemy",
                "hp": {"cur": 6, "max": 6}, "defeated": False,
                "cohort": {"ref": "hollowed_x4", "index": 1, "total": 4},
            },
            "hollowed#2": {
                "id": "hollowed#2", "name": "Hollowed", "side": "enemy",
                "hp": {"cur": 6, "max": 6}, "defeated": False,
                "cohort": {"ref": "hollowed_x4", "index": 2, "total": 4},
            },
            "hollowed#3": {
                "id": "hollowed#3", "name": "Hollowed", "side": "enemy",
                "hp": {"cur": 6, "max": 6}, "defeated": False,
                "cohort": {"ref": "hollowed_x4", "index": 3, "total": 4},
            },
        },
        "history": [],
    }
    return state


def _large_cohort_state() -> dict:
    state = _state()
    state["entities"].pop("iven")
    cohort_ref = "baser_hollow_x27"
    state["battle"] = {
        "active": True,
        "name": "Ordinal Field",
        "cohort": {
            "schema": "battle-cohort/1",
            "id": cohort_ref,
            "name": "Baser Hollow",
            "total": 27,
            "tier": "standard",
            "armament": "",
            "spawned": 27,
            "remaining": 0,
        },
    }
    combatants = {}
    for index in range(1, 28):
        combatant_id = "baser_hollow" if index == 1 else f"baser_hollow#{index}"
        combatants[combatant_id] = {
            "id": combatant_id,
            "name": "Baser Hollow",
            "side": "enemy",
            "hp": {"cur": 6, "max": 6},
            "defeated": False,
            "cohort": {"ref": cohort_ref, "index": index, "total": 27},
        }
    state["combat"] = {"active": True, "combatants": combatants, "history": []}
    return state


def _state_with_skill(skill_id: str, name: str) -> dict:
    state = _state()
    card = state["player"]["mage"]
    card["skills"][skill_id] = 4
    card["defs"]["skills"][skill_id] = {
        "name": name,
        "keyed_stat": "INT",
        "governs": ["dodge"],
        "cost": {"mana": 1},
    }
    return state


def _frames(result) -> list[dict]:
    return [
        op["frame"] for op in result.rule_ops
        if op.get("op") == "semantic_frame_commit"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check elementalism)) I rain down a huge wave of pointed ice on Iven.",
        "((aether.check voidweaving)) I destabilize the very existence of Iven.",
    ],
    ids=("directed-pointed-ice", "existence-destabilization"),
)
def test_directed_harmful_capability_is_one_targeted_settled_attack(text: str):
    result = _run(text)

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("weapon_attack", "iven")]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "iven"
    assert damage[0]["delta"] < 0


def test_two_explicit_checks_stay_two_occurrences_and_only_prose_owner_is_attack():
    result = _run(
        "((aether.check elementalism)) "
        "((aether.check elementalism use ice_focus)) "
        "I then rain down a huge wave of pointed ice on Iven."
    )

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("skill_check", None), ("weapon_attack", "iven")]
    assert len([op for op in result.rule_ops if op.get("op") == "combatant_hp"]) == 1


def test_single_composed_active_check_owns_the_projectile_attack_and_one_impact():
    result = _run(
        "((aether.check elementalism use ice_focus)) "
        "I launch a massive ice spike at Iven."
    )

    frames = _frames(result)
    assert [(frame["action_class"], frame["target_entity_id"]) for frame in frames] \
        == [("weapon_attack", "iven")]
    assert frames[0]["invoked_capability_ids"] == ["ice_focus"]
    assert len(result.checks) == 1
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "iven"
    assert damage[0]["delta"] < 0


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check voidweaving)) I launch a massive ice spike at Iven.",
        "((aether.check elementalism)) I destabilize the very existence of Iven.",
    ],
    ids=("void-does-not-authorize-ice", "elements-do-not-authorize-essential-self"),
)
def test_unrelated_owned_skill_cannot_authorize_hp_damage(text: str):
    result = _run(text)

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert "capability.action_unbound" in frame["ambiguity"]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)
    assert not any(
        op.get("op") == "mechanic_settlement_commit"
        for op in result.rule_ops
    )
    assert not any(
        op.get("op") in {
            "check", "combatant_spawn", "scene_set", "resource_adj",
            "ability_cooldown_set", "master_tick", "effect_add",
        }
        for op in result.rule_ops
    )


@pytest.mark.parametrize(
    ("skill_id", "name"),
    [
        ("avoidance", "Avoidance"),
        ("avoidant_guard", "Avoidant Guard"),
        ("avoiding_danger", "Avoiding Danger"),
    ],
    ids=("avoidance", "avoidant-compound", "avoiding-compound"),
)
def test_capability_domain_roots_do_not_match_inside_unrelated_tokens(
    skill_id: str,
    name: str,
):
    result = _run_state(
        f"((aether.check {skill_id})) I strike at Iven's existence.",
        _state_with_skill(skill_id, name),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert "capability.action_unbound" in frame["ambiguity"]
    assert not any(
        op.get("op") in {"combatant_hp", "resource_adj"}
        or op.get("contract_id") == "weapon_attack/1"
        for op in result.rule_ops
    )


def test_productive_void_domain_root_still_authorizes_essential_self_harm():
    state = _state_with_skill("voidcraft", "Voidcraft")
    state["player"]["mage"]["defs"]["skills"]["voidcraft"]["governs"] = ["ward"]
    result = _run_state(
        "((aether.check voidcraft)) I strike at Iven's existence.",
        state,
        _Rig(),
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert "capability.action_unbound" not in frame["ambiguity"]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven"


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check elementalism)) I rain down praise on Iven.",
        "((aether.check voidweaving)) I destabilize negotiations with Iven.",
    ],
    ids=("nonharmful-rain", "abstract-destabilization"),
)
def test_similar_nonharmful_language_remains_a_skill_check(text: str):
    result = _run(text)

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("skill_check", None)]
    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"} for op in result.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check melee at Iven)) I cut a deal with Iven.",
        "((aether.check melee at Iven)) I cut the deal with Iven.",
        "((aether.check melee at Iven)) I cut another dangerous deal with Iven.",
        "((aether.check melee at Iven)) I attack the problem with Iven.",
        "((aether.check melee at Iven)) I attack this difficult problem with Iven.",
        "((aether.check melee at Iven)) I hit the open road with Iven.",
        "((aether.check melee at Iven)) I parry his strike.",
        "((aether.check melee at Iven)) I parry, his strike glances off.",
        "((aether.check melee at Iven)) I parry before his strike lands.",
        "((aether.check melee at Iven)) I block Iven's attack.",
        "((aether.check melee at Iven)) I dodge the incoming slash.",
    ],
    ids=(
        "cut-a-deal", "cut-the-deal", "cut-modified-deal", "attack-the-problem",
        "attack-modified-problem", "hit-modified-road", "parry-attack-noun",
        "parry-comma-attack-noun", "parry-temporal-attack-noun",
        "block-attack-noun", "dodge-attack-noun",
    ),
)
def test_nonperformed_attack_language_never_becomes_hp_damage(text: str):
    result = _run_state(text, _state(), _Rig())

    assert [frame["action_class"] for frame in _frames(result)] == ["skill_check"]
    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"}
        or (op.get("op") == "scene_set" and op.get("phase") == "climax")
        or (
            op.get("op") == "mechanic_settlement_commit"
            and op.get("contract_id") == "weapon_attack/1"
        )
        for op in result.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check melee at Iven)) I cut Iven down.",
        "((aether.check melee at Iven)) I stab Iven.",
        "((aether.check melee at Iven)) I parry his strike and strike Iven.",
        "((aether.check melee at Iven)) I cut Iven down to end the deal.",
    ],
    ids=("literal-cut", "literal-stab", "defend-then-strike", "literal-before-deal"),
)
def test_literal_melee_predicate_remains_one_hp_attack(text: str):
    result = _run_state(text, _state(), _Rig())

    frames = _frames(result)
    assert [(frame["action_class"], frame["target_entity_id"]) for frame in frames] \
        == [("weapon_attack", "iven")]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven" and damage[0]["delta"] < 0


@pytest.mark.parametrize(
    "separator",
    [", then ", "; then ", " and then "],
    ids=("comma-then", "semicolon-then", "and-then"),
)
def test_one_explicit_melee_check_composes_defense_then_one_strike(separator: str):
    text = (
        "((aether.check melee at Iven)) I parry his blow"
        f"{separator}strike Iven."
    )
    result = _run_state(text, _state(), _Rig())

    frames = _frames(result)
    assert [(frame["capability_id"], frame["action_class"], frame["target_entity_id"])
            for frame in frames] == [("swordplay", "weapon_attack", "iven")]
    assert len(result.checks) == 1
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven" and damage[0]["delta"] < 0

    occurrences = result.semantic_turn.occurrence_graph["occurrences"]
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert {row["identity"] for row in occurrence["actions"]} == {"weapon_attack"}
    assert {row["identity"] for row in occurrence["capabilities"]} == {"swordplay"}
    assert all(
        occurrence["source_span"][0] <= row["span"][0] < row["span"][1]
        <= occurrence["source_span"][1]
        for key in ("actions", "capabilities", "targets")
        for row in occurrence[key]
    )


def test_one_elemental_check_composes_brace_then_domain_grounded_projectile():
    result = _run_state(
        "((aether.check elementalism use ice_focus)) "
        "I brace, then launch a massive ice spike at Iven.",
        _state(),
        _Rig(),
    )

    frames = _frames(result)
    assert [(frame["capability_id"], frame["action_class"], frame["target_entity_id"])
            for frame in frames] == [("elementalism", "weapon_attack", "iven")]
    assert frames[0]["invoked_capability_ids"] == ["ice_focus"]
    assert len(result.checks) == 1
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven" and damage[0]["delta"] < 0
    assert len(result.semantic_turn.occurrence_graph["occurrences"]) == 1


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check melee at Iven)) I parry. Then strike Iven.",
        "((aether.check melee at Iven)) I parry, then strike Iven and stab Iven.",
        "((aether.check melee at Iven)) I parry, then Iven strikes me.",
        "((aether.check melee at Iven)) I parry, then cut the deal with Iven.",
        "((aether.check voidweaving)) I brace, then launch a massive ice spike at Iven.",
    ],
    ids=(
        "period", "multiple-harmful-actions", "other-actor", "false-friend",
        "wrong-capability-domain",
    ),
)
def test_composed_maneuver_never_crosses_an_unowned_or_ambiguous_boundary(text: str):
    result = _run_state(text, _state(), _Rig())

    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)
    assert not any(
        frame["action_class"] == "weapon_attack" and frame["capability_id"] == "swordplay"
        for frame in _frames(result)
    )


def test_composed_maneuver_with_multiple_patients_refuses_before_mechanics():
    state = _state()
    state["entities"]["bo"] = {
        "kind": "npc", "name": "Bo", "present": True, "aliases": [],
    }
    result = _run_state(
        "((aether.check melee at Iven)) I parry, then strike Iven and Bo.",
        state,
        _Rig(),
    )

    frame = _frames(result)[0]
    assert "occurrence.multiple_targets" in frame["ambiguity"]
    assert not any(
        op.get("op") in {"check", "combatant_spawn", "combatant_hp"}
        or (op.get("op") == "scene_set" and op.get("phase") == "climax")
        for op in result.rule_ops
    )


def test_second_structured_check_owns_its_own_later_attack():
    result = _run_state(
        "((aether.check melee at Iven)) I parry, then "
        "((aether.check elementalism)) I launch a massive ice spike at Iven.",
        _state(),
        _Rig(),
    )

    frames = _frames(result)
    attacks = [frame for frame in frames if frame["action_class"] == "weapon_attack"]
    assert [(frame["capability_id"], frame["target_entity_id"]) for frame in attacks] \
        == [("elementalism", "iven")]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven"


@pytest.mark.parametrize(
    "payload",
    [
        "pointed questions",
        "sharp criticism",
        "searing insults",
        "caustic commentary",
    ],
)
def test_dangerous_adjective_without_physical_carrier_never_becomes_hp_harm(
    payload: str,
):
    result = _run(
        f"((aether.check elementalism)) I rain down {payload} on Iven."
    )

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("skill_check", None)]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


@pytest.mark.parametrize(
    "payload",
    [
        "a wave of pointed ice",
        "sharp metal shards",
        "searing fire",
        "caustic acid",
    ],
)
def test_dangerous_physical_carrier_remains_a_targeted_attack(payload: str):
    result = _run(
        f"((aether.check elementalism)) I rain down {payload} on Iven."
    )

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("weapon_attack", "iven")]
    assert len([op for op in result.rule_ops if op.get("op") == "combatant_hp"]) == 1


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check elementalism)) I launch a massive ice spike at Iven.",
        "((aether.check elementalism)) I launch a heavy stone shard toward Iven.",
    ],
    ids=("ice-spike", "stone-shard"),
)
def test_harmful_projectile_delivery_constructs_one_targeted_attack(text: str):
    result = _run(text)

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("weapon_attack", "iven")]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "iven"
    assert damage[0]["delta"] < 0


@pytest.mark.parametrize("delivery", ["launch", "fire", "throw", "shoot"])
@pytest.mark.parametrize(
    "payload",
    [
        "ice criticism spike",
        "criticism ice spike",
        "fire insult spike",
        "stone question spike",
        "acid commentary dart",
        "lightning debate bolt",
        "ice-criticisms spike",
        "fire-insulting spike",
        "stone-questioning spike",
        "acidcommentary dart",
        "lightningdebating bolt",
        "stone-question-spike",
        "insult-fire-spike",
        "verbal ice spike",
    ],
    ids=(
        "material-before-criticism", "criticism-before-material", "insult", "question",
        "commentary", "debate", "criticism-morphology", "insult-morphology",
        "question-morphology", "commentary-compact-modifier", "debate-compact-modifier",
        "fully-hyphenated", "fully-hyphenated-abstract-first", "explicitly-verbal",
    ),
)
def test_abstract_communication_projectile_compounds_never_deal_hp(
    delivery: str,
    payload: str,
):
    result = _run_state(
        f"((aether.check elementalism)) I {delivery} a {payload} at Iven.",
        _state(),
        _Rig(),
    )

    assert [frame["action_class"] for frame in _frames(result)] == ["skill_check"]
    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"}
        or op.get("contract_id") == "weapon_attack/1"
        for op in result.rule_ops
    )


def test_abstract_communication_compounds_are_bounded_not_arbitrary_substrings():
    accepted = (
        "acidcommentary", "commentaryacid", "lightningdebating", "debatinglightning",
        "stonequestioning", "questioningstone",
    )
    unrelated = (
        "precriticismpost", "insultation", "questionstonework", "commentaryboard",
        "debateclubhouse", "nonverbalizer",
    )

    assert all(tier0._abstract_communication_tokens(value) for value in accepted)
    assert not any(tier0._abstract_communication_tokens(value) for value in unrelated)


@pytest.mark.parametrize(
    ("delivery", "payload"),
    [
        ("launch", "massive ice spike"),
        ("fire", "searing fire bolt"),
        ("throw", "heavy stone spear"),
        ("shoot", "lightning dart"),
    ],
)
def test_each_projectile_delivery_verb_preserves_literal_physical_harm(
    delivery: str,
    payload: str,
):
    result = _run_state(
        f"((aether.check elementalism)) I {delivery} a {payload} at Iven.",
        _state(),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert (frame["action_class"], frame["target_entity_id"]) == ("weapon_attack", "iven")
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven"


@pytest.mark.parametrize("separator", [", then ", " and then ", " and "])
def test_abstract_topic_ends_before_a_real_renewed_projectile_action(separator: str):
    result = _run_state(
        "((aether.check elementalism)) I rain down pointed questions about ice"
        f"{separator}launch a real stone spear at Iven.",
        _state(),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert (frame["action_class"], frame["target_entity_id"]) == ("weapon_attack", "iven")
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "iven"


@pytest.mark.parametrize(
    ("setup", "payload"),
    [
        ("", "pointed questions about ice"),
        ("", "searing criticism of fire"),
        ("", "caustic insults about acid"),
        ("", "pointed questions about ice and fire"),
        ("", "searing criticisms regarding ancient fire"),
        ("", "caustic commentary concerning elemental acid"),
        ("", "pointed questions about when to strike"),
        ("", "searing criticism of firing"),
        ("", "caustic insults about shooting"),
        ("brace, then ", "pointed questions about ice"),
        ("brace; then ", "searing criticism of fire"),
        ("brace and then ", "caustic insults about acid"),
        ("brace, then ", "pointed questions about striking"),
    ],
    ids=(
        "questions-about-ice", "criticism-of-fire", "insults-about-acid",
        "coordinated-topics", "inflected-criticism-topic", "modified-commentary-topic",
        "topic-strike", "topic-firing", "topic-shooting",
        "composed-questions-about-ice", "composed-criticism-of-fire",
        "composed-insults-about-acid", "composed-topic-striking",
    ),
)
def test_topical_physical_carriers_never_authorize_harm(
    setup: str,
    payload: str,
):
    result = _run_state(
        f"((aether.check elementalism)) I {setup}rain down {payload} on Iven.",
        _state(),
        _Rig(),
    )

    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"}
        or op.get("contract_id") == "weapon_attack/1"
        for op in result.rule_ops
    )


def test_named_genitive_existence_is_a_typed_essential_self_patient():
    result = _run(
        "((aether.check voidweaving)) I strike at Iven's very existence."
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "iven"
    assert frame["target_locus"] == "existence"
    assert frame["target_locus_owner_id"] == "iven"
    action_frame = result.semantic_turn.frames[0]
    assert [row["source"] for row in action_frame.occurrence["targets"]] == [
        "essential_self_locus_owner"
    ]
    assert len([op for op in result.rule_ops if op.get("op") == "combatant_hp"]) == 1


def test_hollow_domain_capability_authorizes_essential_self_but_not_ice():
    accepted = _run(
        "((aether.check holloweaving use hollow_focus)) "
        "I strike at Iven's very existence."
    )
    refused = _run(
        "((aether.check holloweaving use hollow_focus)) "
        "I launch a massive ice spike at Iven."
    )

    accepted_frame = _frames(accepted)[0]
    assert accepted_frame["invoked_capability_ids"] == ["hollow_focus"]
    assert accepted_frame["target_locus"] == "existence"
    assert "capability.action_unbound" not in accepted_frame["ambiguity"]
    assert len([op for op in accepted.rule_ops if op.get("op") == "combatant_hp"]) == 1

    refused_frame = _frames(refused)[0]
    assert "capability.action_unbound" in refused_frame["ambiguity"]
    assert not any(op.get("op") == "combatant_hp" for op in refused.rule_ops)


def test_ordinary_named_possession_does_not_promote_owner_to_hp_patient():
    result = _run("((aether.check voidweaving)) I strike Iven's sword.")

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] is None
    assert frame["possessed_object"] == "sword"
    assert frame["linguistic_possessor_id"] == "iven"
    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"} for op in result.rule_ops
    )


@pytest.mark.parametrize(
    ("reference", "target"),
    [
        ("the first hollow", "hollowed"),
        ("Hollowed #2", "hollowed#2"),
    ],
    ids=("ordinal-morphology", "visible-row-label"),
)
def test_combat_row_reference_binds_exactly_one_projectile_patient(
    reference: str,
    target: str,
):
    result = _run_state(
        f"((aether.check elementalism)) I launch a massive ice spike at {reference}.",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == target
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == target
    assert damage[0]["delta"] < 0


def test_send_elemental_lance_towards_first_enemy_binds_one_combat_patient():
    result = _run_state(
        "((aether.check elementalism use ice_focus)) "
        "I send a lance of ice towards the first hollow.",
        _cohort_state(),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert frame["capability_id"] == "elementalism"
    assert frame["invoked_capability_ids"] == ["ice_focus"]
    assert frame["target_entity_id"] == "hollowed"
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "hollowed"
    assert damage[0]["delta"] < 0


def test_cast_elemental_tornado_at_first_enemy_binds_one_combat_patient():
    result = _run_state(
        "((aether.check elementalism use ice_focus)) "
        "I cast a tornado of ice at the first hollow.",
        _cohort_state(),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert frame["action_class"] == "weapon_attack"
    assert frame["target_entity_id"] == "hollowed"
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "hollowed"
    assert damage[0]["delta"] < 0


@pytest.mark.parametrize(
    "payload",
    ["message", "vote", "blessing of fire", "lance of praise"],
)
def test_send_towards_combat_reference_requires_a_physical_harmful_spell_payload(payload: str):
    result = _run_state(
        f"((aether.check elementalism)) I send a {payload} towards the first hollow.",
        _cohort_state(),
        _Rig(),
    )

    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


def test_tier0_discovers_every_state_owned_ordinal_surface_through_twenty_seven():
    state = _large_cohort_state()
    for ordinal in _COMBAT_REFERENCE_ORDINALS:
        text = f"strike the {ordinal} baser hollow"
        expected = f"the {ordinal} baser hollow"
        spans = tier0._combat_reference_surface_spans(state, text, 0, len(text))
        assert expected in {text[start:end] for start, end in spans}, ordinal


@pytest.mark.parametrize(
    ("reference", "target"),
    [
        ("twenty first baser hollow", "baser_hollow#21"),
        ("twenty-seventh baser hollow", "baser_hollow#27"),
        ("21st baser hollow", "baser_hollow#21"),
    ],
    ids=("spaced-twenty-first", "hyphenated-twenty-seventh", "numeric-twenty-first"),
)
def test_state_owned_high_ordinals_bind_the_exact_combat_row(
    reference: str,
    target: str,
):
    result = _run_state(
        f"((aether.check elementalism)) I launch an ice spike at the {reference}.",
        _large_cohort_state(),
        _Rig(),
    )

    frame = _frames(result)[0]
    assert frame["target_entity_id"] == target
    assert "combat_reference.unknown" not in frame["ambiguity"]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == target


def test_combat_row_genitive_extracts_owner_before_essential_self_locus():
    result = _run_state(
        "((aether.check voidweaving)) "
        "I strike at Hollowed #1's very existence.",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert frame["target_entity_id"] == "hollowed"
    assert frame["target_locus"] == "existence"
    assert frame["target_locus_owner_id"] == "hollowed"
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1
    assert damage[0]["target"] == "hollowed"


@pytest.mark.parametrize(
    ("reference", "status"),
    [
        ("Hollowed", "ambiguous"),
        ("Hollowed #1", "defeated"),
        ("Hollowed #4", "queued"),
        ("the first stranger", "unknown"),
    ],
)
def test_nonresolved_combat_reference_keeps_its_exact_refusal_reason(
    reference: str,
    status: str,
):
    state = _cohort_state()
    if status == "defeated":
        state["combat"]["combatants"]["hollowed"]["defeated"] = True
        state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 0
    result = _run_state(
        f"((aether.check elementalism)) I launch a massive ice spike at {reference}.",
        state,
    )

    frame = _frames(result)[0]
    assert frame["target_entity_id"] is None
    assert f"combat_reference.{status}" in frame["ambiguity"]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


def test_combat_row_possession_is_not_promoted_to_the_hp_patient():
    result = _run_state(
        "((aether.check voidweaving)) I strike Hollowed #1's sword.",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert frame["target_entity_id"] is None
    assert frame["possessed_object"] == "sword"
    assert frame["linguistic_possessor_id"] == "hollowed"
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


def test_explicit_macro_only_ambiguity_retains_typed_reference_evidence():
    result = _run_state(
        "((aether.check elementalism at hollowed))",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert set(frame["ambiguity"]) == {"hollowed", "hollowed#2", "hollowed#3"}
    references = [
        row for row in frame["evidence"]
        if row["kind"] == "target_reference"
    ]
    assert [row["value"] for row in references] == [
        "ambiguous:Hollowed #1|Hollowed #2|Hollowed #3|Hollowed #4"
    ]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


@pytest.mark.parametrize(
    "targets",
    [
        "Hollowed #1 and Hollowed #2",
        "Hollowed #1 or Hollowed #2",
        "Hollowed #1 and also Hollowed #2",
        "Hollowed #1 plus Hollowed #2",
        "Hollowed #1 as well as Hollowed #2",
        "Hollowed #1 but also Hollowed #2",
        "Hollowed #1 and then Hollowed #2",
        "Hollowed #1, Hollowed #2",
        "Hollowed #1 and Hollowed #1",
        "first hollow and Hollowed #1",
        "Hollowed #1 and first hollow",
        "the first hollow plus Hollowed #1",
        "first hollow and Hollowed #2",
        "second hollow and Hollowed #1",
        "first hollow as well as Hollowed #2",
    ],
)
def test_multiple_written_combat_row_patients_never_collapse_to_one_hit(targets: str):
    result = _run_state(
        "((aether.check elementalism)) "
        f"I launch a stone shard at {targets}.",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert "occurrence.multiple_targets" in frame["ambiguity"]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


def test_overlapping_world_name_cannot_hide_combat_row_cardinality():
    state = _cohort_state()
    state["entities"]["hollowed_world"] = {
        "kind": "npc", "name": "Hollowed", "present": True, "aliases": [],
    }

    result = _run_state(
        "((aether.check elementalism)) "
        "I launch a stone shard at Hollowed #1 as well as Hollowed #2.",
        state,
    )

    frame = _frames(result)[0]
    assert "occurrence.multiple_targets" in frame["ambiguity"]
    assert not any(op.get("op") == "combatant_hp" for op in result.rule_ops)


def test_combat_row_essential_self_patient_and_other_possession_stay_one_patient():
    result = _run_state(
        "((aether.check voidweaving)) I strike at Hollowed #1's existence "
        "and Hollowed #2's sword.",
        _cohort_state(),
    )

    frame = _frames(result)[0]
    assert frame["target_entity_id"] == "hollowed"
    assert frame["target_locus"] == "existence"
    assert "occurrence.multiple_targets" not in frame["ambiguity"]
    damage = [op for op in result.rule_ops if op.get("op") == "combatant_hp"]
    assert len(damage) == 1 and damage[0]["target"] == "hollowed"


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check elementalism)) I launch a boat at Iven.",
        "((aether.check elementalism)) I launch the project with Iven.",
        "((aether.check elementalism)) I launch a blistering critique at Iven.",
        "((aether.check elementalism)) I launch a rhetorical dart at Iven.",
        "((aether.check elementalism)) I launch a price spike at Iven.",
        "((aether.check elementalism)) I launch a data spike at Iven.",
    ],
    ids=(
        "boat", "project", "critique", "rhetorical-projectile",
        "price-spike", "data-spike",
    ),
)
def test_nonharmful_launch_constructions_remain_nonimpact_checks(text: str):
    result = _run(text)

    assert [(frame["action_class"], frame["target_entity_id"]) for frame in _frames(result)] \
        == [("skill_check", None)]
    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"} for op in result.rule_ops
    )


@pytest.mark.parametrize(
    "text",
    [
        "((aether.check elementalism)) I do not launch a massive stone shard at Iven.",
        (
            "((aether.check elementalism)) If I launched a massive stone shard at Iven, "
            "it would end this."
        ),
        (
            '((aether.check elementalism)) I say, '
            '"I launch a massive stone shard at Iven."'
        ),
        (
            "((aether.check elementalism)) I metaphorically launch a massive stone shard "
            "at Iven."
        ),
    ],
    ids=("negated", "hypothetical", "quoted", "metaphorical"),
)
def test_nonactual_or_nonliteral_projectile_language_never_executes(text: str):
    result = _run(text)

    assert not any(
        op.get("op") in {"combatant_spawn", "combatant_hp"} for op in result.rule_ops
    )
