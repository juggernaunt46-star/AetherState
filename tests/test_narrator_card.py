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

from aetherstate import creator, narrator
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
