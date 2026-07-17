"""Authority and lifecycle regressions for arbitrary Player resource pools."""
from __future__ import annotations

import pytest

from aetherstate.config import Config
from aetherstate.state import (apply_delta, authority_violation, current_state, empty_state,
                               reduce_state, validate_op)
from aetherstate.store import Store
from aetherstate.tier0 import resource_change_op


def _seeded_store() -> tuple[Store, str, str, Config]:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="resource-lifecycle")
    seeded = apply_delta(store, sid, bid, 0, [
        {"op": "entity_add", "name": "Kael"},
        {"op": "player_seed", "entity": "Kael", "card": {
            "resources": {
                "focus": {"name": "Focus", "cur": 3, "max": 5, "color": "#b56cff"},
                "spoolcharge": {"name": "Spool Charge", "cur": 4, "max": 8},
                "stamina": {"cur": 2, "max": 12},
                "mana": {"cur": 2, "max": 10},
            },
        }},
    ], "user", cfg)
    assert not seeded.quarantined
    return store, sid, bid, cfg


def test_custom_pools_do_not_inherit_builtin_recovery_or_level_growth():
    state = empty_state()
    state["entities"]["kael"] = {
        "kind": "player", "name": "Kael", "present": True,
    }
    reduce_state(state, [{"op": "player_seed", "entity": "kael", "card": {
        "resources": {
            "stamina": {"cur": 2, "max": 12},
            "mana": {"cur": 2, "max": 10},
            "heat": {"name": "Heat", "cur": 3, "max": 8},
            "rage": {"name": "Rage", "cur": 1, "max": 6},
        },
    }, "_turn": 0}])

    reduce_state(state, [{"op": "scene_set", "location": "docks", "_turn": 1}])
    pools = state["player"]["kael"]["resources"]
    assert pools["stamina"]["cur"] == 5                 # historical +25% scene recovery
    assert pools["mana"]["cur"] == 4                    # historical +25% scene recovery
    assert pools["heat"]["cur"] == 3 and pools["rage"]["cur"] == 1

    reduce_state(state, [{"op": "time_advance", "to_time_of_day": "morning", "_turn": 2}])
    assert pools["stamina"]["cur"] == 12                # historical full rest recovery
    assert pools["mana"]["cur"] == 9                    # historical +50% rest recovery
    assert pools["heat"]["cur"] == 3 and pools["rage"]["cur"] == 1

    reduce_state(state, [{"op": "level_up", "char": "kael",
                          "_grants": {"pool": 2}, "_turn": 3}])
    assert pools["stamina"] == {"cur": 14, "max": 14}
    assert pools["mana"] == {"cur": 11, "max": 12}
    assert pools["heat"] == {"cur": 3, "max": 8, "name": "Heat"}
    assert pools["rage"] == {"cur": 1, "max": 6, "name": "Rage"}


def test_resource_change_builder_is_internal_exact_and_non_hp():
    op = resource_change_op("kael", "focus", "spend", 2)
    assert op == {"op": "resource_change", "char": "kael", "resource": "focus",
                  "action": "spend", "amount": 2}
    assert validate_op(op) is not None

    with pytest.raises(ValueError):
        resource_change_op("kael", "Focus", "gain", 1)       # display names are not ids
    with pytest.raises(ValueError):
        resource_change_op("kael", "hp", "spend", 1)         # HP keeps its receipt path
    with pytest.raises(ValueError):
        resource_change_op("kael", "focus", "gain", True)


def test_resource_change_is_rule_only_and_nonlive_safe():
    cfg = Config()
    cfg.specialization.name = "rpg"
    op = resource_change_op("kael", "focus", "gain", 1)
    state = empty_state()
    assert authority_violation(op, "rule", state, cfg) is None
    for source in ("user", "genesis", "extraction"):
        why = authority_violation(op, source, state, cfg)
        assert why is not None and "code-owned" in why

    state["scene"]["mode"] = "dream"
    why = authority_violation(op, "rule", state, cfg)
    assert why is not None and "flashback/dream" in why

    none_cfg = Config()
    why = authority_violation(op, "rule", empty_state(), none_cfg)
    assert why is not None and "specialization=none" in why

    from aetherstate.extraction import EXTRACTION_OPS_RPG, _OP_ALLOWED
    assert "resource_change" not in EXTRACTION_OPS_RPG
    assert "resource_change" not in _OP_ALLOWED


def test_rule_resource_gain_spend_set_and_visible_transactional_rejections():
    store, sid, bid, cfg = _seeded_store()

    spent = apply_delta(
        store, sid, bid, 1, [resource_change_op("kael", "focus", "spend", 2)], "rule", cfg)
    assert [op["op"] for op in spent.applied] == ["resource_change"]
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"] == {
        "cur": 1, "max": 5, "name": "Focus", "color": "#b56cff",
    }

    rows_before = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]
    overspent = apply_delta(
        store, sid, bid, 2, [resource_change_op("kael", "focus", "spend", 2)], "rule", cfg)
    assert not overspent.applied
    assert overspent.quarantined[0]["reason"] == \
        "not enough focus: 1/2 available for code-owned spend"
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == rows_before
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"]["cur"] == 1

    missing = apply_delta(
        store, sid, bid, 3, [resource_change_op("kael", "charge", "gain", 1)], "rule", cfg)
    assert not missing.applied
    assert missing.quarantined[0]["reason"] == "undeclared Player resource 'charge'"
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == rows_before

    gained = apply_delta(
        store, sid, bid, 4, [resource_change_op("kael", "focus", "gain", 99)], "rule", cfg)
    assert gained.applied
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"]["cur"] == 5

    set_exact = apply_delta(
        store, sid, bid, 5, [resource_change_op("kael", "focus", "set", 2)], "rule", cfg)
    assert set_exact.applied
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"]["cur"] == 2

    set_oob = apply_delta(
        store, sid, bid, 6, [resource_change_op("kael", "focus", "set", 6)], "rule", cfg)
    assert not set_oob.applied
    assert set_oob.quarantined[0]["reason"] == \
        "cannot set focus to 6: declared maximum is 5"
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"]["cur"] == 2


def test_free_form_sources_cannot_commit_resource_changes():
    store, sid, bid, cfg = _seeded_store()
    op = resource_change_op("kael", "focus", "gain", 2)
    rows_before = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]

    for turn, source in enumerate(("extraction", "user", "genesis"), start=1):
        result = apply_delta(store, sid, bid, turn, [op], source, cfg)
        assert not result.applied
        assert result.quarantined and "code-owned" in result.quarantined[0]["reason"]

    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == rows_before
    assert current_state(store, bid)["player"]["kael"]["resources"]["focus"]["cur"] == 3


def test_extraction_cannot_shadow_player_resources_or_code_owned_structures():
    store, sid, bid, cfg = _seeded_store()
    protected = [
        "hp", "resources", "stats.INT", "skills", "abilities", "cooldowns", "level", "xp",
        "focus", "spoolcharge", "Spool Charge",
    ]
    hp_before = dict(current_state(store, bid)["player"]["kael"]["hp"])
    rows_before = store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0]

    result = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "set_attribute", "entity": "Kael", "key": key, "value": 8}
         for key in protected],
        "extraction",
        cfg,
    )

    assert not result.applied
    assert len(result.quarantined) == len(protected)
    assert all("Player" in row["reason"] and "shadow" in row["reason"]
               for row in result.quarantined)
    assert store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0] == rows_before
    state = current_state(store, bid)
    assert state["attributes"].get("kael", {}) == {}
    assert state["player"]["kael"]["hp"] == hp_before
    assert state["player"]["kael"]["resources"]["focus"]["cur"] == 3
    assert state["player"]["kael"]["resources"]["spoolcharge"]["cur"] == 4


def test_extraction_can_still_commit_a_descriptive_player_attribute():
    store, sid, bid, cfg = _seeded_store()

    result = apply_delta(
        store,
        sid,
        bid,
        1,
        [{"op": "set_attribute", "entity": "Kael", "key": "demeanor",
          "value": "steadfast"}],
        "extraction",
        cfg,
    )

    assert not result.quarantined
    assert result.applied == [
        {"op": "set_attribute", "entity": "kael", "key": "demeanor",
         "value": "steadfast", "_turn": 1},
    ]
    assert current_state(store, bid)["attributes"]["kael"]["demeanor"] == "steadfast"
