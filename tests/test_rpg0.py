"""RPG-0 fixtures (Q27 / doc 05 §9 phase RPG-0): the [specialization] framework + overlay
resolver, the Player Card record + [PLAYER] block, [QUEST] over existing goal ops, the DM
guard framing, and the /aether/specialization route. Exit criteria:
  - overlay precedence: user-override > profile > base-default;
  - Player Card renders under rpg;
  - [PLAYER]/[QUEST]/DM-guard visible under rpg and ABSENT (byte-identical) under none.
Every RPG surface is gated so a `none` session is unchanged from pre-RPG behaviour.
"""
from __future__ import annotations

import json

from aetherstate.compose import compose, render_guard, render_header
from aetherstate.config import Config, load_config
from aetherstate.genesis import seed_player
from aetherstate.stamps import Stamp
from aetherstate.state import (authority_violation, current_state, empty_state,
                               is_empty, reduce_state, validate_op)
from aetherstate.store import Store
from tests.mock_upstream import Reply


# ------------------------------ config overlay (framework) --------------------------
def test_overlay_precedence_user_beats_profile_beats_default(tmp_path):
    """The heart of the framework: user-override > profile > base-default (doc 05 §7)."""
    assert "quest" not in Config().injection.priorities                # base default (none)

    p = tmp_path / "config.toml"
    p.write_text('[specialization]\nname = "rpg"\n')
    c_profile = load_config(p)
    assert c_profile.specialization.name == "rpg"
    assert c_profile.injection.priorities["quest"] == 70               # profile default
    assert c_profile.injection.priorities["player_card"] == 90         # profile default
    assert c_profile.injection.priorities["state_header"] == 100       # base survives overlay

    p.write_text('[specialization]\nname = "rpg"\n[injection.priorities]\nquest = 77\n')
    c_user = load_config(p)
    assert c_user.injection.priorities["quest"] == 77                  # user beats profile
    assert c_user.injection.priorities["player_card"] == 90            # profile key survives


def test_specialization_env_and_default(tmp_path, monkeypatch):
    assert Config().specialization.name == "none"                      # default is inert
    monkeypatch.setenv("AETHERSTATE_SPECIALIZATION__NAME", "rpg")
    c = load_config(tmp_path / "nope.toml")
    assert c.specialization.name == "rpg"                              # env can select the profile
    assert c.injection.priorities.get("quest") == 70                  # ...and the overlay applies


# ------------------------------ player_seed op (state record) -----------------------
_SEED = {"op": "player_seed", "entity": "Kael",
         "card": {"level": 7, "concept": "Exiled ranger", "stats": {"STR": 14, "DEX": 12},
                  "skills": {"stealth": 3}, "abilities": ["Power Strike"],
                  "resources": {"hp": {"max": 50}, "stamina": {"max": 6}}}}


def test_player_seed_validation():
    assert validate_op(_SEED) is not None
    assert validate_op({"op": "player_seed", "card": {}}) is None       # entity required
    assert validate_op({"op": "player_seed", "entity": "x", "card": 5}) is None  # card must be dict


def test_player_seed_authority_is_privileged():
    cfg, st = Config(), empty_state()
    assert authority_violation(_SEED, "user", st, cfg) is None
    assert authority_violation(_SEED, "genesis", st, cfg) is None
    assert authority_violation(_SEED, "rule", st, cfg) is not None      # rejected
    assert authority_violation(_SEED, "extraction", st, cfg) is not None  # rejected


def test_player_seed_reducer_expands_card():
    st = empty_state()
    st["entities"]["kael"] = {"kind": "character", "name": "Kael", "present": True}
    reduce_state(st, [{"op": "player_seed", "entity": "kael", "card": _SEED["card"], "_turn": 0}])
    rec = st["player"]["kael"]
    assert rec["level"] == 7 and rec["hp"] == {"cur": 50, "max": 50}    # cur defaults to max
    assert rec["resources"]["stamina"] == {"cur": 6, "max": 6}
    assert rec["stats"] == {"STR": 14, "DEX": 12} and rec["skills"] == {"stealth": 3}
    assert st["entities"]["kael"]["kind"] == "player"                   # marked as the player
    assert not is_empty({"player": {"kael": rec}, "entities": {}})      # a lone card still renders


# ------------------------------ [PLAYER]/[QUEST] render + DM guard -------------------
def _rpg_state():
    st = empty_state()
    st["entities"] = {"player": {"name": "Kael", "present": True, "kind": "player"},
                      "seraphine": {"name": "Seraphine", "present": True}}
    st["scene"] = {"location_id": "Tavern", "participants": ["player"]}
    st["player"] = {"player": {"eid": "player", "level": 7, "hp": {"cur": 42, "max": 50},
                    "resources": {"stamina": {"cur": 6, "max": 6}},
                    "stats": {"STR": 14, "DEX": 12}, "skills": {"stealth": 3, "persuasion": 5},
                    "abilities": ["Power Strike"]}}
    st["chars"] = {"player": {"goals": ["Find the amulet", "Rescue the caravan"]}}
    return st


def test_rpg_blocks_render_only_under_rpg():
    cfg = Config()
    assert "[PLAYER]" not in render_header(_rpg_state(), cfg)           # none: no leak
    assert "[QUEST]" not in render_header(_rpg_state(), cfg)
    cfg.specialization.name = "rpg"
    h = render_header(_rpg_state(), cfg)
    assert "[PLAYER] Kael · Lv7 · HP 42/50 · Stamina 6/6" in h
    # effective check mods, precomputed (doc 06 §2.3): stealth DEX12(+1)+rank3; persuasion rank5
    assert "Stats: STR14 DEX12" in h and "Skills: Stealth+4 Persuasion+5" in h
    assert "Abilities: Power Strike" in h
    assert "[QUEST] Find the amulet · Rescue the caravan" in h


def test_rpg_blocks_respect_profile_blocks_list():
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.blocks = ["QUEST"]                              # drop PLAYER from the profile
    h = render_header(_rpg_state(), cfg)
    assert "[PLAYER]" not in h and "[QUEST]" in h


def test_dm_guard_framing_only_under_rpg():
    stamp = Stamp(session="s", user="Kael")
    assert "Game Master" not in render_guard(Config(), stamp, "new_turn")   # none: standard guard
    cfg = Config()
    cfg.specialization.name = "rpg"
    g = render_guard(cfg, stamp, "new_turn")
    assert "Game Master" in g and "Kael is the Player" in g
    cfg.specialization.dm_guard = False
    assert "Game Master" not in render_guard(cfg, stamp, "new_turn")        # knob turns it off


def test_compose_e2e_blocks_gated():
    d = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    cfg = Config()
    out, _ = compose(d, _rpg_state(), cfg, Stamp(session="s", user="Kael"), "new_turn")
    assert "[PLAYER]" not in json.dumps(out)                            # none: absent end-to-end
    cfg.specialization.name = "rpg"
    out, kept = compose(d, _rpg_state(), cfg, Stamp(session="s", user="Kael"), "new_turn")
    blob = json.dumps(out)
    assert "[PLAYER]" in blob and "[QUEST]" in blob and "Game Master" in blob


# ------------------------------ player genesis (track 2) ----------------------------
def test_seed_player_default_idempotent_and_inert():
    cfg = Config()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-rpg")
    doc = {"messages": [{"role": "system", "content": "A dark tavern."}]}

    assert seed_player(store, cfg, sid, branch, doc) == 0              # inert under none
    assert not current_state(store, branch).get("player")

    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"
    n = seed_player(store, cfg, sid, branch, doc)
    assert n >= 1
    pl = current_state(store, branch)["player"]
    (eid, rec), = pl.items()
    assert rec["level"] == 1 and rec["hp"]["max"] == 20 and set(rec["stats"]) == {
        "STR", "DEX", "INT", "CHA", "CUN", "CON"}

    assert seed_player(store, cfg, sid, branch, doc) == 0              # idempotent


def test_seed_player_reads_explicit_seed():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-rpg2")
    doc = {"messages": [], "aetherstate_player": {
        "identity": {"name": "Lyra", "level": 3}, "stats": {"STR": 8, "DEX": 16},
        "skills": {"archery": 2}}}
    seed_player(store, cfg, sid, branch, doc)
    pl = current_state(store, branch)["player"]
    assert "lyra" in pl and pl["lyra"]["level"] == 3 and pl["lyra"]["stats"]["DEX"] == 16


# ------------------------------ live proxy: route + e2e render ----------------------
SENT = "<<AETHER:v=1;session={s};turn={t};type=normal;speaker=Dungeon Master;user=Bean>>"


def _payload(session, turn):
    return {"model": "m", "min_p": 0.07, "messages": [
        {"role": "system", "content": SENT.format(s=session, t=turn) + " A cold tavern at dusk."},
        {"role": "user", "content": "I push open the door."}]}


async def test_specialization_route_toggle(client):
    assert (await client.get("/aether/specialization")).json()["name"] == "none"
    r = (await client.post("/aether/specialization", json={"name": "rpg"})).json()
    assert r["name"] == "rpg" and "PLAYER" in r["blocks"] and r["dm_guard"] is True
    assert (await client.get("/aether/specialization")).json()["name"] == "rpg"
    bad = await client.post("/aether/specialization", json={"name": "wat"})
    assert bad.status_code == 422


async def test_rpg_new_session_renders_player_block_e2e(client, mock_upstream, cfg):
    """Flagship RPG-0 exit: a new rpg session seeds the Player Card at genesis and the very
    first forwarded request already carries [PLAYER] and the DM guard."""
    cfg.specialization.name = "rpg"
    cfg.user_guard.name = "Bean"                                        # the user's persona name
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-rpg", 1))
    doc = json.loads(mock_upstream.requests[0].body)
    header = next(m for m in doc["messages"] if "[PLAYER]" in str(m.get("content")))
    assert "[PLAYER] Bean" in header["content"]                         # user persona is the Player
    assert "Game Master" in header["content"]                          # DM guard framing
    sid = (await client.get("/aether/sessions")).json()["sessions"][0]["session_id"]
    now = (await client.get(f"/aether/session/{sid}/state")).json()
    assert now["state"]["player"]                                       # Player Card persisted


async def test_none_session_has_no_rpg_leak_e2e(client, mock_upstream, cfg):
    """A `none` session is byte-identical to pre-RPG: no Player Card, no RPG blocks."""
    assert cfg.specialization.name == "none"
    mock_upstream.enqueue(Reply())
    await client.post("/v1/chat/completions", json=_payload("chat-none", 1))
    body = mock_upstream.requests[0].body
    assert b"[PLAYER]" not in body and b"[QUEST]" not in body and b"Game Master" not in body


# ------------------------ one player per session (2026-07-06 live repro) -------------
def test_player_seed_replaces_genesis_default():
    """The genesis placeholder must VANISH when the real character arrives. Live repro:
    'Player — Player lv1' rode alongside 'Player — Juna lv5' in [PLAYER] and the narrator
    treated the real character as a companion NPC."""
    from aetherstate.creator import player_to_ops
    from aetherstate.state import apply_delta
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="t-oneplayer")
    doc = {"messages": [{"role": "system", "content": "A dark tavern."}]}
    seed_player(store, cfg, sid, branch, doc)                    # chat-open placeholder
    st = current_state(store, branch)
    assert len(st["player"]) == 1
    default_eid = next(iter(st["player"]))
    assert st["player"][default_eid].get("genesis_default") is True
    ops = player_to_ops({"name": "Juna", "class": "Blessed Maiden", "level": 5}, cfg)
    apply_delta(store, sid, branch, 1, ops, "user", cfg)          # the Creator's save
    st = current_state(store, branch)
    assert list(st["player"]) == ["juna"]                         # ONE player, the real one
    assert default_eid not in st["entities"]                      # placeholder left no ghost
    assert st["entities"]["juna"]["kind"] == "player"
    assert not st["player"]["juna"].get("genesis_default")


def test_player_seed_demotes_authored_predecessor():
    """Replacing a REAL previous character keeps them in the world as an NPC."""
    st = empty_state()
    st["entities"]["kael"] = {"kind": "character", "name": "Kael", "present": True}
    reduce_state(st, [{"op": "player_seed", "entity": "kael",
                       "card": {"level": 2}, "_turn": 0}])
    st["entities"]["juna"] = {"kind": "character", "name": "Juna", "present": True}
    reduce_state(st, [{"op": "player_seed", "entity": "juna",
                       "card": {"level": 5}, "_turn": 1}])
    assert list(st["player"]) == ["juna"]
    assert st["entities"]["kael"]["kind"] == "npc"                # predecessor stays in-world
    assert st["entities"]["juna"]["kind"] == "player"


def test_stage_b_never_mints_players():
    """Genesis stage B seeds the WORLD; the Player Card has its own track (seed_player /
    the Creator). An assist model reading a card that talks about 'the Player' must not be
    able to invent one."""
    from aetherstate.genesis import _parse_ops
    raw = json.dumps([
        {"op": "entity_add", "name": "Player", "kind": "player"},
        {"op": "player_seed", "entity": "Player", "card": {"level": 1}},
        {"op": "entity_add", "name": "Mira", "kind": "npc"},
    ])
    ops = _parse_ops(raw)
    assert ("entity_add", "Mira") in [(o["op"], o.get("name")) for o in ops]
    assert all(o["op"] != "player_seed" for o in ops)
    assert all(not (o["op"] == "entity_add" and o.get("kind") == "player") for o in ops)


def test_stage_b_presence_needs_basis():
    """Stage-B presence=true requires the name in the card/prompt text — notable NPCs stay
    known-but-offstage until the fiction brings them on (2026-07-06 live repro)."""
    from aetherstate.genesis import _presence_with_basis
    ops = [{"op": "presence", "entity": "Mira", "present": True},
           {"op": "presence", "entity": "Varo", "present": True},
           {"op": "presence", "entity": "Ghost", "present": False},
           {"op": "memory_event", "text": "x"}]
    card = "The city guard captain Mira waits at the gate."
    out = _presence_with_basis(ops, card, "", speaker="Narrator")
    names = [(o.get("entity"), o.get("present")) for o in out if o["op"] == "presence"]
    assert ("Mira", True) in names                     # named in the card: basis exists
    assert ("Varo", True) not in names                 # no basis: stays offstage
    assert ("Ghost", False) in names                   # leaving is always allowed
    assert any(o["op"] == "memory_event" for o in out)  # non-presence ops untouched


def test_dm_contract_v2_in_fiction_and_known_vs_present():
    """dm-rules/2 (2026-07-06): no out-of-character endings; state-block NPCs are known,
    not on-scene."""
    from aetherstate.prompts import DM_CONTRACT_VERSION, DM_RULES_CONTRACT
    assert DM_CONTRACT_VERSION == "dm-rules/4"
    assert "settles THIS attempt NOW" in DM_RULES_CONTRACT   # RPG-5: no directive-dodging
    assert "What will you do?" in DM_RULES_CONTRACT
    assert "KNOWN, not on-scene" in DM_RULES_CONTRACT
    assert "CALL FOR the check" in DM_RULES_CONTRACT         # dm-rules/4: drive the mechanics
