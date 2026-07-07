"""Creator (doc 09): World Generator + Character Creator.

Deterministic backbone + assist-LLM fill-the-blanks, persisted as SHIPPED ops (entities /
memory-lore / scene / player_seed) — no new op vocabulary, no new storage families. Coverage:
the registry export, deterministic fills (blanks + player-fields-win), freestyle -> FROZEN defs
snapshot, that every generated op is already in the op vocab (invariant 3: nothing new touches
the stream), op persistence via apply_delta(source='user'), replay-purity of the seeded Player
Card, the control routes end-to-end, and a `none`-session no-leak.
"""
from __future__ import annotations

from aetherstate import creator
from aetherstate.compose import render_header
from aetherstate.config import Config
from aetherstate.state import (_SPEC, apply_delta, current_state, empty_state,
                               reduce_state, validate_op)
from aetherstate.store import Store


# ------------------------------ deterministic backbone -----------------------------
def test_registry_export_shape():
    rx = creator.registry_export()
    assert {"STR", "DEX", "INT", "CHA", "CUN", "CON"} <= set(rx["stats"])
    assert "stealth" in rx["skills"] and "power_strike" in rx["abilities"]
    assert rx["mod_policy"] == "dnd5e"
    assert "high_fantasy" in rx["genres"] and "morning" in rx["times"]


def test_deterministic_world_fills_blanks():
    w = creator.deterministic_world({"genre": "cyberpunk", "name": "Rustline"})
    assert w["name"] == "Rustline" and w["genre"] == "cyberpunk"
    assert w["time"] in creator.TIMES and w["setting"]          # blank -> template
    assert len(w["factions"]) >= 3 and w["opening_quest"]


def test_deterministic_world_player_fields_win():
    w = creator.deterministic_world({"genre": "high_fantasy", "setting": "MY PREMISE",
                                     "factions": ["Only Mine"]})
    assert w["setting"] == "MY PREMISE" and w["factions"] == ["Only Mine"]


def test_deterministic_player_filters_unknown_and_freezes_freestyle():
    p = creator.deterministic_player({
        "name": "Vex", "class": "Storm-Touched Skald",
        "skills": {"persuasion": 2, "bogus_skill": 9},
        "abilities": ["silver_tongue", "not_a_real_ability"],
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA", "base_mod": 1},
                              {"name": "Nonsense", "keyed_stat": "ZZZ"}]}})
    assert p["concept"] == "Storm-Touched Skald"
    assert p["skills"] == {"persuasion": 2}                     # bogus id dropped
    assert p["abilities"] == ["silver_tongue"]                 # unknown ability dropped
    assert "stormsong" in p["defs"]["skills"]                  # freestyle FROZEN
    assert p["defs"]["skills"]["stormsong"]["keyed_stat"] == "CHA"
    assert "nonsense" not in p["defs"].get("skills", {})       # invalid keyed_stat rejected


def test_deterministic_player_ranks_and_knows_freestyle_defs():
    """A frozen def skill is rankable and a frozen def ability lands in the KNOWN list — the
    authored custom passive actually applies (registry ∪ defs filtering, not registry-only)."""
    p = creator.deterministic_player({
        "name": "Vex",
        "skills": {"Stormsong": 2, "persuasion": 1, "bogus": 9},
        "abilities": ["Storm Heart", "silver_tongue"],
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA", "base_mod": 1}],
                   "abilities": [{"name": "Storm Heart", "kind": "passive",
                                  "passive_mod": {"skill": "stormsong", "amount": 2}}]}})
    assert p["skills"] == {"stormsong": 2, "persuasion": 1}      # def skill rankable (by name too)
    assert p["abilities"] == ["storm_heart", "silver_tongue"]    # def ability KNOWN, preset kept
    from aetherstate import registry as _reg
    card = {"stats": p["stats"], "skills": p["skills"], "abilities": p["abilities"],
            "defs": p["defs"]}
    # RPG-5 deterministic stat spend: all-baseline stats + ranked skills -> the keyed stat
    # of the top skill gets +2 (CHA 12 -> +1), so: CHA1 + base1 + rank2 + passive2 = 6
    assert _reg.load().effective_mod(card, "stormsong") == 6


def test_world_ops_use_only_shipped_vocab_and_validate():
    ops = creator.world_to_ops({"genre": "high_fantasy", "name": "Aldering"})
    assert ops and all(validate_op(o) is not None for o in ops)
    kinds = {o["op"] for o in ops}
    assert kinds <= set(_SPEC)                                 # no new op vocabulary (invariant 3)
    assert kinds <= {"memory_event", "entity_add", "set_attribute", "scene_set",
                     "time_advance", "quest_add"}     # RPG-5: the opening quest is ledger truth


def test_player_ops_shape_and_validate():
    ops = creator.player_to_ops({"name": "Kara", "class": "Knight", "species": "Human", "sex": "F"})
    assert [o["op"] for o in ops][:2] == ["entity_add", "player_seed"]
    assert all(validate_op(o) is not None for o in ops)
    card = ops[1]["card"]
    assert card["concept"] == "Knight" and set(card["stats"]) >= {"STR", "DEX"}


# ------------------------------ persistence + replay -------------------------------
def _apply(ops, cfg=None, turn=0):
    cfg = cfg or Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="creator-t")
    res = apply_delta(store, sid, bid, turn, ops, "user", cfg)
    return store, bid, res


def test_world_ops_persist_entities_and_lore():
    ops = creator.world_to_ops({"genre": "dark_fantasy", "name": "Gallow",
                                "npcs": [{"name": "The Warden", "role": "jailer"}]})
    store, bid, _ = _apply(ops)
    st = current_state(store, bid)
    kinds = {e.get("kind") for e in st["entities"].values()}
    assert {"faction", "location", "npc"} <= kinds
    assert st["attributes"].get("the_warden", {}).get("role") == "jailer"


def test_player_ops_persist_card_and_defs():
    ops = creator.player_to_ops({
        "name": "Vex", "class": "Skald", "stats": {"CHA": 16},
        "skills": {"persuasion": 2},
        "custom": {"skills": [{"name": "Stormsong", "keyed_stat": "CHA"}]}})
    store, bid, _ = _apply(ops)
    st = current_state(store, bid)
    player = st["player"]["vex"]
    assert player["concept"] == "Skald" and player["stats"]["CHA"] == 16
    assert player["skills"]["persuasion"] == 2
    assert "stormsong" in player["defs"]["skills"]
    assert st["attributes"].get("vex", {}).get("class") == "Skald"


def test_seeded_player_is_replay_pure():
    ops = creator.player_to_ops({"name": "Mara", "class": "Ranger", "stats": {"DEX": 15}})
    store, bid, _ = _apply(ops)
    live = current_state(store, bid)["player"]
    replay = store.state_at(bid, 10**9, reduce_state, empty=empty_state())["player"]
    assert replay == live                                      # journal -> identical player card


def test_creator_seed_inert_under_none():
    cfg = Config()                                             # specialization.name == "none"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="creator-none")
    apply_delta(store, sid, bid, 0,
                creator.player_to_ops({"name": "Vex", "class": "Skald"}), "user", cfg)
    apply_delta(store, sid, bid, 1,
                creator.world_to_ops({"genre": "sci_fi", "name": "Verge"}), "user", cfg)
    h = render_header(current_state(store, bid), cfg)
    assert "[PLAYER]" not in h and "[DIRECTIVE]" not in h and "[QUEST]" not in h


# ------------------------------ control routes (in-process) ------------------------
async def test_registry_route(client):
    r = await client.get("/aether/registry")
    assert r.status_code == 200 and "STR" in r.json()["stats"]


async def test_creator_page_served(client):
    r = await client.get("/aether/creator")
    assert r.status_code == 200 and "Creator" in r.text


async def test_world_and_player_routes_persist(client):
    g = await client.post("/aether/session/route-t/genesis",
                          json={"card": "A wizard's tower.", "greeting": "Welcome."})
    assert g.status_code == 200
    w = await client.post("/aether/session/route-t/world",
                          json={"world": {"genre": "high_fantasy", "name": "Aldreth"}})
    assert w.status_code == 200 and w.json()["applied"] > 0
    p = await client.post("/aether/session/route-t/player",
                          json={"player": {"name": "Rillian", "class": "Rogue"}})
    assert p.status_code == 200 and p.json()["applied"] > 0
    pre = (await client.get("/aether/session/route-t/creator")).json()
    assert pre["player"] and pre["player"]["concept"] == "Rogue"
    assert pre["world_seeded"] is True


async def test_creator_first_save_mints_session(client):
    """2026-07-06 live repro ('Save failed: HTTP 404'): a brand-new chat has no session row
    until its first message flows through the relay, so a creator-first world/character save
    bounced with 404. The save routes now mint the session by external id — the same row the
    relay adopts when the chat's first stamped message arrives."""
    w = await client.post("/aether/session/st-neverseen1/world",
                          json={"world": {"genre": "high_fantasy", "name": "Firsthold"}})
    assert w.status_code == 200
    j = w.json()
    assert j["applied"] > 0 and j["session_id"]
    p = await client.post("/aether/session/st-neverseen1/player",
                          json={"player": {"name": "Kael", "class": "Warrior"}})
    assert p.status_code == 200 and p.json()["applied"] > 0
    # both saves converged on ONE session, resolvable by the external id
    assert p.json()["session_id"] == j["session_id"]
    sessions = (await client.get("/aether/sessions")).json()["sessions"]
    mine = [s for s in sessions if s["external_id"] == "st-neverseen1"]
    assert len(mine) == 1 and mine[0]["session_id"] == j["session_id"]
    pre = (await client.get("/aether/session/st-neverseen1/creator")).json()
    assert pre["world_seeded"] is True
    assert pre["player"] and pre["player"]["concept"] == "Warrior"


async def test_author_route_no_model_is_honest_error(client):
    """2026-07-06: no reachable model -> source='error' + detail; the window keeps the
    form untouched instead of silently swapping in templates."""
    r = await client.post("/aether/session/route-missing/author",
                          json={"mode": "world", "doc": {"genre": "cyberpunk"}})
    assert r.status_code == 200
    j = r.json()
    assert j["source"] == "error" and "model" in j["detail"]


async def test_author_route_offline_fills_templates_on_request(client):
    """Templates stay one explicit click away (offline:1) — deterministic, no LLM call."""
    r = await client.post("/aether/session/route-missing/author",
                          json={"mode": "world", "offline": 1,
                                "doc": {"genre": "cyberpunk"}})
    j = r.json()
    assert j["source"] == "deterministic" and j["doc"]["genre"] == "cyberpunk"
    assert j["doc"]["setting"]                                  # blanks template-filled
    r2 = await client.post("/aether/session/route-missing/author",
                           json={"mode": "player", "offline": 1, "doc": {"name": "Vex"}})
    assert r2.json()["source"] == "deterministic" and r2.json()["doc"]["name"] == "Vex"


# ------------------------------ model detection (menu + author route) ---------------
def _chat_reply(content: str) -> bytes:
    import json
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


async def test_creator_models_route_lists_and_fails_open(client, mock_upstream):
    import json
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply(body=json.dumps(
        {"data": [{"id": "m-big"}, {"id": "m-small"}]}).encode()))
    r = await client.get("/aether/creator/models")
    assert r.status_code == 200
    eps = r.json()["endpoints"]
    assert eps and eps[0]["models"] == ["m-big", "m-small"]
    # unscripted upstream (500) -> fail-open to an empty list, never an error
    r2 = await client.get("/aether/creator/models")
    assert r2.status_code == 200 and r2.json()["endpoints"][0]["models"] == []


async def test_author_uses_requested_model_without_session(client, mock_upstream):
    """Creator-first flow: no session row yet, but an explicit model pick still AI-authors."""
    import json
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply(body=_chat_reply(
        '{"name":"Rustline","genre":"cyberpunk","setting":"Neon.","factions":["A","B","C"]}')))
    r = await client.post("/aether/session/no-such-session/author",
                          json={"mode": "world", "doc": {"genre": "cyberpunk"},
                                "model": "picked-model"})
    j = r.json()
    assert j["source"] == "llm" and j["model"] == "picked-model"
    assert j["doc"]["name"] == "Rustline"
    assert json.loads(mock_upstream.requests[-1].body)["model"] == "picked-model"


async def test_author_autodetects_model_from_endpoint(client, mock_upstream):
    """Fresh session, nothing proxied yet: the author route detects a model via GET /models
    instead of posting model='' (the bug: backend 400 -> silent deterministic fallback)."""
    import json
    from tests.mock_upstream import Reply
    mock_upstream.enqueue(Reply(body=json.dumps({"data": [{"id": "auto-m"}]}).encode()))
    mock_upstream.enqueue(Reply(body=_chat_reply('{"name":"Verge","genre":"sci_fi"}')))
    r = await client.post("/aether/session/still-no-session/author",
                          json={"mode": "world", "doc": {}})
    j = r.json()
    assert j["source"] == "llm" and j["model"] == "auto-m"
    chat = [q for q in mock_upstream.requests if q.path.endswith("/chat/completions")]
    assert json.loads(chat[-1].body)["model"] == "auto-m"


# ------------------------------ filled boxes ride as context (2026-07-06) -----------
def test_world_user_prompt_carries_every_filled_field():
    seed = {"setting": "S" * 3000, "npcs": [{"name": "Maren", "role": "witch", "desc": "creditor"}],
            "opening_scene": "A skiff at dawn", "opening_quest": "Pay the debt",
            "notes": "gothic tides"}
    u = creator._world_user(seed)
    assert "Maren" in u and "witch" in u             # npcs were dropped before the fix
    assert "A skiff at dawn" in u and "Pay the debt" in u
    assert "gothic tides" in u
    assert "S" * 2500 in u                           # long settings ride (not clipped to 2000)


def test_char_user_prompt_carries_world_and_custom():
    world = {"genre": "sci_fi", "aspects": ["FTL is rationed"],
             "locations": ["Dock 12"], "npcs": [{"name": "Vala", "role": "fixer", "desc": ""}]}
    seed = {"concept": "Void Cantor", "skills": {"stealth": 2},
            "custom": {"abilities": [{"name": "Null Hymn", "kind": "active"}]}}
    u = creator._char_user(seed, world)
    assert "FTL is rationed" in u and "Dock 12" in u and "Vala" in u
    assert "stealth=2" in u and "Null Hymn" in u


def test_json_or_none_strips_fences_and_prose():
    """2026-07-06 live repro: GLM fenced its creator JSON — parse must survive fences
    and prose prefixes."""
    from aetherstate.assist import _json_or_none
    assert _json_or_none('```json\n{"name":"Tidefall"}\n```') == {"name": "Tidefall"}
    assert _json_or_none('Here you go:\n{"name":"Tidefall"}') == {"name": "Tidefall"}
    assert _json_or_none("no json at all") is None


# ------------------------------ presets + committed-state review (2026-07-06) -------
async def test_presets_roundtrip(client):
    r = await client.post("/aether/presets", json={
        "kind": "world", "name": "Tidefall", "doc": {"genre": "dark_fantasy", "name": "Tidefall"}})
    assert r.status_code == 200
    pid = r.json()["preset_id"]
    lst = (await client.get("/aether/presets")).json()["presets"]
    assert any(p["preset_id"] == pid and p["kind"] == "world" for p in lst)
    got = (await client.get(f"/aether/presets/{pid}")).json()
    assert got["doc"]["genre"] == "dark_fantasy"
    # upsert by (kind, name) — same id, new doc
    r2 = await client.post("/aether/presets", json={
        "kind": "world", "name": "Tidefall", "doc": {"genre": "sci_fi"}})
    assert r2.json()["preset_id"] == pid
    assert (await client.get(f"/aether/presets/{pid}")).json()["doc"]["genre"] == "sci_fi"
    assert (await client.delete(f"/aether/presets/{pid}")).status_code == 200
    assert (await client.get(f"/aether/presets/{pid}")).status_code == 404
    bad = await client.post("/aether/presets", json={"kind": "nope", "name": "x", "doc": {}})
    assert bad.status_code == 422


async def test_prefill_returns_committed_world_doc(client):
    await client.post("/aether/session/rev-t/genesis",
                      json={"card": "A tower.", "greeting": "Hello."})
    await client.post("/aether/session/rev-t/world", json={"world": {
        "genre": "high_fantasy", "name": "Aldreth", "setting": "Nine feuding baronies.",
        "factions": ["The Ash Order"], "locations": ["Highmoor"],
        "npcs": [{"name": "Serane", "role": "seneschal", "desc": "keeps the keys"}],
        "aspects": ["Magic is taxed."], "opening_quest": "Find the heir."}})
    pre = (await client.get("/aether/session/rev-t/creator")).json()
    w = pre["world"]
    assert pre["world_seeded"] and w
    assert w["name"] == "Aldreth" and w["genre"] == "high_fantasy"
    assert w["setting"] == "Nine feuding baronies."
    assert "The Ash Order" in w["factions"] and "Highmoor" in w["locations"]
    assert w["npcs"] and w["npcs"][0]["name"] == "Serane" and w["npcs"][0]["role"] == "seneschal"
    assert "Magic is taxed." in w["aspects"] and w["opening_quest"] == "Find the heir."


async def test_connection_persists_upstream_default_model(client):
    r = await client.post("/aether/connection", json={"target": "upstream",
                                                      "base_url": "http://mock-upstream/v1",
                                                      "model": "glm-main"})
    assert r.status_code == 200 and r.json()["upstream"]["model"] == "glm-main"
    g = (await client.get("/aether/connection")).json()
    assert g["upstream"]["model"] == "glm-main"


# ------------------------------ roomier authoring clamps ---------------------------
def test_wider_authoring_clamps_still_clamp():
    w = creator.deterministic_world({"genre": "modern", "setting": "x" * 5000,
                                     "factions": [f"f{i}" for i in range(40)]})
    assert len(w["setting"]) == 2000                     # roomier prose clamp, still a clamp
    assert len(w["factions"]) == 20                      # roomier list cap, still a cap
    p = creator.deterministic_player({
        "name": "Vex",
        "custom": {"skills": [{"name": "Storm Song", "keyed_stat": "CHA"}],
                   "abilities": [{"name": "Iron Hide", "kind": "passive",
                                  "passive_mod": {"skill": "Storm Song", "amount": 9}}]}})
    pm = p["defs"]["abilities"]["iron_hide"]["passive_mod"]
    assert pm == {"skill": "storm_song", "amount": 5}    # name slugged to id; amount clamped ±5
    # (2026-07-06: the target must EXIST now — a dead reference no longer freezes as real)


# ------------------------------ 2026-07-06 live-playtest fixes ------------------------------
def test_split_name_desc_mints_clean_entity_ids():
    """'Name — description' faction/location lines: the NAME is the entity, the description
    becomes an attribute — no more 80-char slug ids (Creator cousin of the vael_cora bug)."""
    ops = creator.world_to_ops({
        "genre": "sci_fi", "name": "Kessler Deep",
        "factions": ["The Lattice Combine — salvage cartel that controls the docking spines"],
        "locations": ["Spindle Market — pressurized bazaar, neutral ground by treaty"]})
    adds = [o for o in ops if o["op"] == "entity_add"]
    names = {o["name"] for o in adds}
    assert "The Lattice Combine" in names and "Spindle Market" in names
    assert all("—" not in o["name"] and len(o["name"]) <= 80 for o in adds)
    descs = [o for o in ops if o["op"] == "set_attribute" and o["key"] == "description"]
    assert any(o["entity"] == "the_lattice_combine" for o in descs)
    scene = [o for o in ops if o["op"] == "scene_set"]
    assert scene and scene[0]["location"] == "spindle_market"


def test_split_name_desc_plain_lines_untouched():
    name, desc = creator._split_name_desc("Rookhollow")
    assert name == "Rookhollow" and desc == ""


def test_passive_target_resolves_or_dies():
    """GLM authored a passive boosting 'vac_ops' while minting 'vacuum_operations' — the mod
    never applied and LOOKED real. Now: resolved when unambiguous, dropped when not."""
    p = creator.deterministic_player({
        "name": "Rell",
        "custom": {"skills": [{"name": "Vacuum Operations", "keyed_stat": "CON"},
                              {"name": "Neural Lace Intrusion", "keyed_stat": "INT"}],
                   "abilities": [
                       {"name": "Wrongfix", "kind": "passive",
                        "passive_mod": {"skill": "vac_ops", "amount": 1}},
                       {"name": "Ghost Step", "kind": "passive",
                        "passive_mod": {"skill": "lace_intrusion", "amount": 1}},
                       {"name": "Dead Ref", "kind": "passive",
                        "passive_mod": {"skill": "warp_gazing", "amount": 2}}]}})
    ab = p["defs"]["abilities"]
    assert ab["wrongfix"]["passive_mod"]["skill"] == "vacuum_operations"
    assert ab["ghost_step"]["passive_mod"]["skill"] == "neural_lace_intrusion"
    assert "passive_mod" not in ab["dead_ref"]          # unresolvable -> dropped, not fake


def test_defs_requires_ability_kept_when_satisfiable():
    p = creator.deterministic_player({
        "name": "Rell",
        "custom": {"skills": [{"name": "Systems Intrusion", "keyed_stat": "INT",
                               "requires_ability": "Neural Lace"},
                              {"name": "Ghost Craft", "keyed_stat": "INT",
                               "requires_ability": "nonexistent_gift"}],
                   "abilities": [{"name": "Neural Lace", "kind": "basis",
                                  "effect": "Basis for Systems Intrusion."}]}})
    sk = p["defs"]["skills"]
    assert sk["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert "requires_ability" not in sk["ghost_craft"]  # unsatisfiable gate stripped
    assert p["defs"]["abilities"]["neural_lace"]["kind"] == "basis"


def test_registry_export_carries_genre_packs():
    rx = creator.registry_export()
    assert "sci_fi" in rx["genre_packs"]
    pack = rx["genre_packs"]["sci_fi"]
    assert "spellcraft" in pack["hide_skills"]          # no Spellcraft on a sci-fi sheet
    assert pack["skills"]["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert rx["concept_hints"]["sci_fi"]


def test_inject_pack_defs_freezes_ranked_pack_entries():
    """Ranks on genre-pack ids must survive deterministic_player: the pack entry (and the
    basis ability its gate needs) is frozen into custom defs before the fill."""
    pack = creator.GENRE_PACKS["sci_fi"]
    doc = creator._inject_pack_defs(
        {"name": "Rell", "skills": {"gunnery": 2, "systems_intrusion": 3},
         "abilities": ["combat_stims"]}, pack)
    p = creator.deterministic_player(doc)
    assert p["skills"]["gunnery"] == 2 and p["skills"]["systems_intrusion"] == 3
    assert "gunnery" in p["defs"]["skills"]
    assert p["defs"]["skills"]["systems_intrusion"]["requires_ability"] == "neural_lace"
    assert "neural_lace" in p["defs"]["abilities"]      # basis auto-frozen with its gate
    assert "combat_stims" in p["defs"]["abilities"]
    assert "combat_stims" in p["abilities"]


def test_pack_gate_is_live_after_seed():
    """End-to-end: a pack-built card carries the def's requires_ability and the eligibility
    gate reads it. Ranking the gated skill at CREATION auto-freezes its basis (intended:
    creation is where capability is built); without that def the gate stays closed."""
    from aetherstate import registry as _registry
    pack = creator.GENRE_PACKS["sci_fi"]
    reg = _registry.load(None)
    doc = creator._inject_pack_defs({"name": "Rell", "skills": {"systems_intrusion": 2}}, pack)
    p = creator.deterministic_player(doc)
    card = {"skills": p["skills"], "abilities": p["abilities"], "defs": p["defs"],
            "stats": p["stats"]}
    entry = reg.skill_entry("systems_intrusion", card)
    assert entry.get("requires_ability") == "neural_lace"
    assert reg.has_ability(card, "neural_lace")          # basis frozen with the ranked skill
    # A card whose def carries the gate but NOT the basis: the gate stays closed (non-move).
    bare = creator.deterministic_player({
        "name": "Kel",
        "custom": {"skills": [dict(pack["skills"]["systems_intrusion"],
                                   id="systems_intrusion",
                                   requires_ability="neural_lace")]}})
    # requires_ability survives only when satisfiable — freezing the gate without its basis
    # would brick the skill, so _coerce_defs strips it (validated design, see F5 post-pass).
    bcard = {"skills": bare["skills"], "abilities": [], "defs": bare["defs"], "stats": bare["stats"]}
    assert not reg.has_ability(bcard, "neural_lace")     # no basis def, no ownership
