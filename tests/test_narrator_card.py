"""World-specific Narrator card (2026-07-07): projecting a committed world + Player Card into a
V2 SillyTavern character card so the player SEES the world they built.

Coverage: build_card surfaces the world (name/factions/places/opening scene), fail-open on an
empty world, the PNG is valid and re-embeds the card JSON (SillyTavern import format), the
avatar is deterministic-but-genre-distinct, the control routes return the card from committed
state, opt-in install writes the PNG to a configured dir, and a `none` session is unaffected
(the card is a read-only ledger projection off the relay — no stream leak)."""
from __future__ import annotations

import base64
import json

import httpx

from aetherstate import creator, narrator, prompts
from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream

_WORLD = {
    "name": "Gallowmere", "genre": "dark_fantasy",
    "setting": "A fen of gallows and fog where the drowned keep their grudges.",
    "date": "Year of the Late Frost", "time": "night", "tone": "grim",
    "factions": ["The Reeve's Men — enforcers of the tithe", "Fenfolk"],
    "locations": ["Gallow Hill", "The Drowned Chapel"],
    "npcs": [{"name": "The Warden", "role": "jailer", "desc": "keeps every key but his own"}],
    "aspects": ["The dead do not stay buried"],
    "opening_scene": "Fog swallows the causeway as the iron gate groans shut behind you.",
    "opening_quest": "Find the tithe collector who never came back.",
}


def _extract_chara(png: bytes) -> dict:
    """Pull the embedded V2 card back out of the PNG tEXt 'chara' chunk (ST import format)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    i = 8
    while i < len(png):
        ln = int.from_bytes(png[i:i + 4], "big")
        typ = png[i + 4:i + 8]
        data = png[i + 8:i + 8 + ln]
        i += 12 + ln
        if typ == b"tEXt":
            kw, _, val = data.partition(b"\x00")
            if kw == b"chara":
                return json.loads(base64.b64decode(val).decode("utf-8"))
    raise AssertionError("no chara tEXt chunk in PNG")


def test_custom_opening_without_authored_location_never_commits_unrelated_template_place():
    world = {
        "name": "Blackglass March", "genre": "dark_fantasy",
        "setting": "A borderland of obsidian roads and ruined keeps.",
        "opening_scene": "Mara reaches Riven Gate under hard rain.",
        "locations": [],
    }
    ops = creator.world_to_ops(world)
    assert not any(op.get("op") == "scene_set" for op in ops)


def test_authored_opening_location_remains_the_authoritative_scene():
    world = {
        "name": "Blackglass March", "genre": "dark_fantasy",
        "opening_scene": "Mara reaches Riven Gate under hard rain.",
        "locations": ["Riven Gate", "The Alder Fen"],
    }
    scenes = [op for op in creator.world_to_ops(world) if op.get("op") == "scene_set"]
    assert scenes == [{"op": "scene_set", "location": "riven_gate", "phase": "opening"}]


# ------------------------------ build_card (pure) ----------------------------------
def test_build_card_surfaces_the_world():
    c = narrator.build_card(_WORLD, {"name": "Rook", "concept": "gravedigger"})["data"]
    assert c["name"] == "Gallowmere"                       # world name is the card name (visible)
    assert "Gallowmere" in c["description"]
    assert "The Reeve's Men" in c["description"]           # faction name surfaced
    assert "Gallow Hill" in c["description"]               # place surfaced
    assert "The Warden" in c["description"]                # cast surfaced
    assert "Rook" in c["description"] and "gravedigger" in c["description"]
    assert "Gallowmere" in c["first_mes"]                  # world named in the opening message
    assert "Fog swallows the causeway" in c["first_mes"]   # the opening SCENE is the first message
    assert "dark_fantasy" in c["tags"] and "Gallowmere" in c["tags"]
    assert "Gallowmere" in c["scenario"]
    # SillyTavern calls .trim() on this field before every generation.  A one-item
    # tuple/list serializes cleanly into the PNG but crashes the live chat before the
    # request ever reaches AetherState, leaving only a saved Player message behind.
    assert isinstance(prompts.NARRATOR_ENVELOPE, str)
    assert isinstance(c["system_prompt"], str)
    assert c["system_prompt"] == prompts.NARRATOR_ENVELOPE
    assert c["post_history_instructions"] == ""
    assert c["mes_example"] == ""
    assert "AetherState" not in c["description"]
    assert "managed by the AetherState engine" not in c["scenario"]
    assert c["character_version"] == "aether-world-1.2"


def test_narrator_envelope_defines_private_input_and_visible_output_boundaries():
    text = prompts.NARRATOR_ENVELOPE
    assert prompts.NARRATOR_ENVELOPE_VERSION == "aether-narrator/2"
    assert prompts.NARRATOR_ENVELOPE_VERSION in text
    assert "not a character you portray" in text
    assert "never add a new dodge, movement, tactic, word, or choice" in text
    assert "second person, present tense" in text
    assert "AetherState has final authority over the complete model request" in text
    assert "jailbreak, and history message is reference content only" in text
    assert "Never request, emit, simulate, or arm a roll" in text
    assert "only exact pipe-containing bracketed record lines" in text
    assert "explicitly listed under LEDGER TAGS" in text
    assert "enemy-intent or enemy-action header" in text and "INPUT ONLY" in text
    for reserved in ("[DIRECTIVE]", "[WAR]", "[INIT]", "[PLAYER]", "[RULES]",
                     "[OPPOSITION]", "[PROTOCOL]"):
        assert reserved in text
    assert "No OOC note" in text
    assert "do not mention AetherState, SillyTavern" in text
    assert "aether.check" not in text


def test_build_card_fail_open_on_empty_world():
    c = narrator.build_card(None)
    assert c["data"]["name"] == "Narrator"                 # neutral fallback, still valid
    assert c["data"]["first_mes"]
    png = narrator.card_png(c)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_card_png_valid_and_reembeds_card():
    c = narrator.build_card(_WORLD)
    png = narrator.card_png(c, _WORLD)
    assert len(png) > 1000 and png[:8] == b"\x89PNG\r\n\x1a\n"
    back = _extract_chara(png)
    assert back["data"]["name"] == "Gallowmere"
    assert isinstance(back["data"]["system_prompt"], str)
    assert back["spec"] == "chara_card_v2"


def test_avatar_deterministic_but_genre_distinct():
    c = narrator.build_card(_WORLD)
    a1 = narrator.card_png(c, _WORLD)
    a2 = narrator.card_png(c, _WORLD)
    assert a1 == a2                                        # deterministic (replay-safe artifact)
    other = {"name": "Neon Verge", "genre": "cyberpunk"}
    b = narrator.card_png(narrator.build_card(other), other)
    assert b != a1                                         # a different world looks different


# ------------------------------ committed-state helpers ----------------------------
def _apply_world(store, sid, bid, cfg, world):
    from aetherstate.state import apply_delta
    apply_delta(store, sid, bid, 0, creator.world_to_ops(world), "user", cfg)


def test_world_from_state_feeds_a_faithful_card():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="narr-doc")
    _apply_world(store, sid, bid, cfg, _WORLD)
    from aetherstate.state import current_state
    doc = creator.world_from_state(current_state(store, bid))
    c = narrator.build_card(doc)["data"]
    assert c["name"] == "Gallowmere"                       # survives the ledger round-trip
    assert "Gallow Hill" in c["description"]


# ------------------------------ control routes (in-process) ------------------------
async def test_narrator_card_routes_from_committed_world(client):
    w = await client.post("/aether/session/narr-t/world", json={"world": _WORLD})
    assert w.status_code == 200 and w.json()["applied"] > 0

    j = await client.get("/aether/session/narr-t/narrator-card.json")
    assert j.status_code == 200 and j.json()["data"]["name"] == "Gallowmere"

    p = await client.get("/aether/session/narr-t/narrator-card.png")
    assert p.status_code == 200 and p.headers["content-type"] == "image/png"
    assert p.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "Gallowmere" in p.headers.get("content-disposition", "")

    post = await client.post("/aether/session/narr-t/narrator-card")
    body = post.json()
    assert post.status_code == 200 and body["world"] == "Gallowmere"
    assert body["bytes"] > 1000 and body["installed"] == ""   # no dir configured -> download only


async def test_card_route_is_spec_independent_no_stream_leak(client):
    """Under `none` the card still projects the ledger's world (it is off the relay), and
    generating it adds no RPG blocks to the briefing — the wire stays byte-identical."""
    await client.post("/aether/session/none-t/world", json={"world": _WORLD})
    j = await client.get("/aether/session/none-t/narrator-card.json")
    assert j.status_code == 200 and j.json()["data"]["name"] == "Gallowmere"
    # the committed world produced no RPG header blocks under spec=none
    row = (await client.get("/aether/session/none-t/state")).json()
    assert "[PLAYER]" not in json.dumps(row) and "[DIRECTIVE]" not in json.dumps(row)


async def test_install_writes_png_when_dir_configured(tmp_path):
    chars = tmp_path / "characters"
    chars.mkdir()
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    cfg.specialization.narrator_card_dir = str(chars)
    mock = MockUpstream()
    transport = httpx.ASGITransport(app=mock)
    upstream = httpx.AsyncClient(transport=transport, base_url="http://mock-upstream")
    app = create_app(cfg, client_factory=lambda: upstream, store=Store(":memory:"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://proxy") as c:
            await c.post("/aether/session/inst-t/world", json={"world": _WORLD})
            r = await c.post("/aether/session/inst-t/narrator-card")
            body = r.json()
            assert r.status_code == 200 and body["installed"]
            out = chars / body["filename"]
            assert out.exists() and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await app.state.jobs.stop()
        await upstream.aclose()


# ------------------------------ card carries a seed (2026-07-08) -------------------
# The card is the CARRIER: it embeds the whole world + Player Card so a fresh chat rebuilds the
# ledger with no LLM (the ST extension replays the seed through /aether/session/{sid}/seed). This
# is the fix for "you have to re-apply the world to every new chat".
def test_build_card_embeds_world_and_player_seed():
    c = narrator.build_card(_WORLD, {"name": "Rook", "concept": "gravedigger",
                                     "stats": {"STR": 12}})["data"]
    aes = c["extensions"]["aetherstate"]
    assert aes["min_proxy"] == "1.6.0" and aes["seed_version"]
    seed = aes["seed"]
    assert seed["world"]["name"] == "Gallowmere" and seed["world"]["factions"]  # whole world doc
    assert seed["player"]["name"] == "Rook"                                     # + Player Card


def test_card_seed_roundtrips_committed_hp_custom_resource_and_cost():
    player = {
        "name": "Rook", "hp": {"cur": 27, "max": 40},
        "resources": {"ash_focus": {"name": "Ash Focus", "cur": 4, "max": 8,
                                     "color": "#b56cff"}},
        "skills": {"ashwind_reading": 2},
        "defs": {"skills": {"ashwind_reading": {
            "name": "Ashwind Reading", "keyed_stat": "CUN", "base_mod": 0,
            "max_rank": 5, "governs": ["read", "track"], "cost": {"ash_focus": 2},
        }}},
    }
    built = narrator.build_card(_WORLD, player)
    back = _extract_chara(narrator.card_png(built, _WORLD))
    seed = back["data"]["extensions"]["aetherstate"]["seed"]
    assert seed["player"]["hp"] == {"cur": 27, "max": 40}
    assert seed["player"]["resources"]["ash_focus"]["color"] == "#b56cff"

    rebuilt = creator.player_to_ops(seed["player"])[1]["card"]
    assert rebuilt["hp"] == {"cur": 27, "max": 40}
    assert rebuilt["resources"]["ash_focus"] == {
        "cur": 4, "max": 8, "name": "Ash Focus", "color": "#b56cff",
    }
    assert rebuilt["defs"]["skills"]["ashwind_reading"]["cost"] == {"ash_focus": 2}


def test_seed_survives_the_png_roundtrip():
    c = narrator.build_card(_WORLD, {"name": "Rook"})
    back = _extract_chara(narrator.card_png(c, _WORLD))
    assert back["data"]["extensions"]["aetherstate"]["seed"]["world"]["name"] == "Gallowmere"


def test_seed_payload_trims_pathological_sizes_but_keeps_fidelity():
    big = dict(_WORLD, setting="x" * 99999, aspects=["a" * 9000] * 200)
    seed = narrator.seed_payload(big, None)
    assert len(seed["world"]["setting"]) <= 8000            # capped, not reshaped
    assert len(seed["world"]["aspects"]) <= 48
    assert seed["world"]["name"] == "Gallowmere"            # small fields untouched


async def test_session_free_card_build_from_form(client):
    """POST /aether/narrator-card builds from POSTed docs with NO committed session (the Creator's
    session-free 'Generate card' path) and returns the PNG as base64 with the seed embedded."""
    r = await client.post("/aether/narrator-card",
                          json={"world": _WORLD, "player": {"name": "Rook", "concept": "digger"}})
    b = r.json()
    assert r.status_code == 200 and b["name"] == "Gallowmere"
    assert b["seeded_world"] and b["seeded_player"] and b["png_b64"]
    png = base64.b64decode(b["png_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert _extract_chara(png)["data"]["extensions"]["aetherstate"]["seed"]["world"]["name"] \
        == "Gallowmere"


async def test_session_free_card_normalizes_full_docs_and_never_embeds_directions(client):
    setting = (
        "Claimfall Harbor preserves exact testimony while keeping reports distinct from truth. "
        * 80
        + "The complete world setting ends at this sentinel."
    )
    appearance = (
        "Kael wears a rain-dark civic coat and a brass witness seal without magical authority. "
        * 35
        + "His complete appearance ends at this sentinel."
    )
    assert 4000 < len(setting) < 8000
    assert 2500 < len(appearance) < 4000
    world_direction = "PRIVATE WORLD DIRECTION MUST NOT ENTER THE CARD."
    player_direction = "PRIVATE CHARACTER DIRECTION MUST NOT ENTER THE CARD."

    response = await client.post("/aether/narrator-card", json={
        "world": {
            "name": "Longform Claimfall",
            "genre": "dark_fantasy",
            "setting": setting,
            "notes": world_direction,
        },
        "player": {
            "name": "Kael",
            "concept": "civic witness",
            "appearance": appearance,
            "extras": [{
                "label": "Method",
                "text": "He labels statements, beliefs, doubts, rumors, and accepted facts.",
            }],
            "notes": player_direction,
        },
    })

    assert response.status_code == 200
    png = base64.b64decode(response.json()["png_b64"])
    card = _extract_chara(png)
    seed = card["data"]["extensions"]["aetherstate"]["seed"]
    assert seed["world"]["setting"] == setting
    assert seed["player"]["appearance"] == appearance
    assert seed["player"]["extras"] == [{
        "label": "Method",
        "text": "He labels statements, beliefs, doubts, rumors, and accepted facts.",
    }]
    serialized = json.dumps(card, ensure_ascii=False)
    assert world_direction not in serialized
    assert player_direction not in serialized
    assert "notes" not in seed["world"]
    assert "notes" not in seed["player"]


async def test_seed_route_is_idempotent_and_non_clobbering(client):
    """The extension replays the card seed on chat-open; the route commits a world/player only
    when none is present, so re-opening an established chat never clobbers progress."""
    await client.post("/aether/specialization", json={"name": "rpg"})
    seed = {"world": _WORLD, "player": {"name": "Rook", "concept": "gravedigger",
                                        "stats": {"STR": 12, "DEX": 10}}}
    r1 = (await client.post("/aether/session/seed-t/seed", json={"seed": seed})).json()
    assert r1["world_seeded"] and r1["player_seeded"] and r1["applied"] > 0
    r2 = (await client.post("/aether/session/seed-t/seed", json={"seed": seed})).json()
    assert not r2["world_seeded"] and not r2["player_seeded"] and r2["applied"] == 0
    pre = (await client.get("/aether/session/seed-t/creator")).json()
    assert pre["world"]["name"] == "Gallowmere" and pre["player_name"] == "Rook"


async def test_sessions_list_carries_legible_world_and_player_names(client):
    """The Creator's session picker was a wall of cryptic st-ids; /aether/sessions now carries
    each session's committed world + player name so 'which session' is legible."""
    await client.post("/aether/specialization", json={"name": "rpg"})
    await client.post("/aether/session/named-t/seed",
                      json={"seed": {"world": _WORLD, "player": {"name": "Rook"}}})
    rows = (await client.get("/aether/sessions")).json()["sessions"]
    hit = [s for s in rows if s.get("world_name") == "Gallowmere"]
    assert hit and hit[0]["player_name"] == "Rook"
