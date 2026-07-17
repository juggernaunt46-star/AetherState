"""Genesis player-name alignment (2026-07-10, Bean): when a DM card's opening NAMES the player
in second person ("You are Kael"), the Player Card is named Kael (not a generic "Player") AND
stage B never stages a same-name NPC twin. Fixes the ledger-vs-fiction identity mismatch that
confused the DM. RPG-gated, replay-safe, none-inert."""
from __future__ import annotations

from aetherstate.config import Config
from aetherstate.genesis import _parse_ops, _player_name_from_text, seed_player
from aetherstate.state import current_state
from aetherstate.store import Store


def test_player_name_from_text_precision():
    assert _player_name_from_text("You are Kael, a down-on-your-luck sellsword.") == "Kael"
    assert _player_name_from_text("Your name is Aria Vale.") == "Aria Vale"
    assert _player_name_from_text("You're Seraphine, a courtier") == "Seraphine"
    assert _player_name_from_text("playing as Corvin the bold") == "Corvin"   # trailing word cut
    assert _player_name_from_text("You are a wandering knight") == ""         # not a proper noun
    assert _player_name_from_text("You are standing under the eaves") == ""   # common word
    # the DM/narrator naming is skipped in favour of the player naming that follows it
    assert _player_name_from_text("You are the Game Master. The rain falls. You are Kael.") == "Kael"


def _gm_doc():
    return {"messages": [
        {"role": "system", "content": "You are the Game Master of a gritty low-fantasy world."},
        {"role": "assistant", "content": "The rain hasn't let up. You are Kael, a down-on-your-luck "
                                         "sellsword, standing under the eaves of the tavern."}]}


def test_seed_player_names_pc_from_greeting():
    cfg = Config()                                          # no persona name, no explicit seed
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-gn1")
    n = seed_player(store, cfg, sid, branch, _gm_doc())
    assert n >= 1
    st = current_state(store, branch)
    assert "kael" in st["player"]                            # keyed by the fiction's name
    assert st["entities"]["kael"]["name"] == "Kael" and st["entities"]["kael"]["kind"] == "player"
    assert "player" not in st["player"]                      # no generic 'Player' placeholder


def test_persona_name_wins_over_greeting():
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-gn2")
    seed_player(store, cfg, sid, branch, _gm_doc())
    st = current_state(store, branch)
    assert "bean" in st["player"] and "kael" not in st["player"]   # explicit user choice wins


def test_none_session_seeds_no_player():
    cfg = Config()                                           # specialization = none
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-gn3")
    assert seed_player(store, cfg, sid, branch, _gm_doc()) == 0
    assert not current_state(store, branch).get("player")


def test_stage_b_drops_player_twin():
    """The world-seed pass must not stage the player's own name as an NPC, place it in the scene,
    or auto-mint it from a reference — the player track owns the PC."""
    import json
    raw = json.dumps([
        {"op": "entity_add", "name": "Kael", "kind": "character"},   # the player, wrongly as NPC
        {"op": "presence", "entity": "Kael", "present": True},       # ...and staged on scene
        {"op": "entity_add", "name": "Merta", "kind": "character"},  # a real NPC — kept
        {"op": "affinity_adj", "target": "Kael", "delta": 1, "reason": "x"}])   # ref, not a twin
    ops = _parse_ops(raw, speaker="Game Master", player_name="Kael")
    adds = [o for o in ops if o["op"] == "entity_add"]
    assert not any(o.get("name") == "Kael" for o in adds)           # no twin minted or auto-added
    assert any(o.get("name") == "Merta" for o in adds)              # the real NPC survives
    assert not any(o["op"] == "presence" and o.get("entity") == "Kael" for o in ops)


def test_stage_b_drops_narrator_role_but_keeps_real_cast():
    """A narrator-card speaker is UI identity, never a hooded NPC the DM can place in scenes."""
    import json
    raw = json.dumps([
        {"op": "entity_add", "name": "Dungeon Master"},
        {"op": "presence", "entity": "Dungeon Master", "present": True},
        {"op": "goal", "char": "Dungeon Master", "action": "add", "text": "keep score"},
        {"op": "entity_add", "name": "Halvic Orne"},
        {"op": "presence", "entity": "Halvic Orne", "present": True},
        {"op": "scene_set", "location": "tollhouse",
         "participants": ["Dungeon Master", "Halvic Orne"], "phase": "setup"},
    ])

    ops = _parse_ops(raw, speaker="Dungeon Master", narrator_speaker=True)

    assert not any("Dungeon Master" in str(op) for op in ops)
    assert any(op.get("name") == "Halvic Orne" for op in ops)
    scene = next(op for op in ops if op["op"] == "scene_set")
    assert scene["participants"] == ["Halvic Orne"]


def test_stage_b_keeps_character_card_speaker_as_real_person():
    import json
    raw = json.dumps([
        {"op": "entity_add", "name": "Akira"},
        {"op": "presence", "entity": "Akira", "present": True},
    ])
    ops = _parse_ops(raw, speaker="Akira", narrator_speaker=False)
    assert any(op.get("name") == "Akira" for op in ops)
    assert any(op["op"] == "presence" and op.get("entity") == "Akira" for op in ops)
