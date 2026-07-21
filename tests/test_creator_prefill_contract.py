"""Creator prefill and portable-card admission are honest, state-safe contracts."""
from __future__ import annotations

from copy import deepcopy

from aetherstate.state import apply_delta, current_state


def _session_row(store, external_id: str):
    return store.db.execute(
        "SELECT * FROM sessions WHERE external_id=?", (external_id,),
    ).fetchone()


def _journal_count(store, branch_id: str) -> int:
    del branch_id
    return int(store.db.execute("SELECT COUNT(*) FROM ops_journal").fetchone()[0])


def _head(store, branch_id: str) -> int:
    row = store.db.execute(
        "SELECT head_turn FROM branches WHERE branch_id=?", (branch_id,),
    ).fetchone()
    return int(row["head_turn"])


def _owned_quantity(state: dict, owner: str, name: str) -> int:
    return sum(
        int((item or {}).get("qty", 1))
        for item in (state.get("items") or {}).values()
        if isinstance(item, dict)
        and item.get("owner") == owner
        and item.get("name") == name
        and item.get("loc") != "gone"
    )


async def test_creator_page_is_never_served_from_a_stale_browser_cache(client):
    response = await client.get("/aether/creator")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "new URL(location.href)" in response.text
    assert "searchParams.set(\"fresh\"" in response.text
    assert "const st=await api(`/aether/session/${SID}/state`)" not in response.text
    assert "pre.effects_live||{}" in response.text
    assert "r.seed_fingerprint" in response.text
    assert "File: ${r.filename}" in response.text
    assert "carries=!!r.seeded_world" in response.text
    assert "r.seeded_player?\" + character\":\"\"" in response.text
    assert "r.seeded_player||p.name" not in response.text
    assert "Next: reload SillyTavern" in response.text
    assert "avatar hover shows “${r.filename}”" in response.text


async def test_world_prefill_separates_authored_source_from_live_projection(
    client, proxy_app, cfg,
):
    sid = "prefill-world-source"
    saved = await client.post(f"/aether/session/{sid}/world", json={"world": {
        "name": "Glasswake",
        "genre": "modern",
        "setting": "A storm-lit harbor where testimony is public currency.",
        "factions": ["Harbor Compact"],
        "locations": ["Witness Quay"],
        "npcs": [{"name": "Mara Venn", "role": "registrar"}],
        "aspects": ["Every claim keeps its named source."],
    }})
    assert saved.status_code == 200 and saved.json()["applied"] > 0

    store = proxy_app.state.store
    row = _session_row(store, sid)
    branch = row["active_branch"]
    apply_delta(
        store,
        row["session_id"],
        branch,
        _head(store, branch) + 1,
        [{"op": "entity_add", "name": "Tide Wisp", "kind": "npc"}],
        "user",
        cfg,
    )

    prefill = (await client.get(f"/aether/session/{sid}/creator")).json()
    source_names = {npc["name"] for npc in prefill["world"]["npcs"]}
    live_names = {npc["name"] for npc in prefill["world_live"]["npcs"]}
    assert "Tide Wisp" not in source_names
    assert "Tide Wisp" in live_names

    before_events = _journal_count(store, branch)
    unchanged = await client.post(
        f"/aether/session/{sid}/world", json={"world": prefill["world"]},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["applied"] == 0
    assert unchanged.json()["already_committed"] is True
    assert _journal_count(store, branch) == before_events

    changed = deepcopy(prefill["world"])
    changed["setting"] = "A replacement setting must not rewrite committed source truth."
    conflict = await client.post(
        f"/aether/session/{sid}/world", json={"world": changed},
    )
    assert conflict.status_code == 409
    assert "immutable" in conflict.json()["error"].lower()
    assert _journal_count(store, branch) == before_events


async def test_player_prefill_resave_preserves_live_hp_and_never_duplicates_gear(
    client, proxy_app, cfg,
):
    sid = "prefill-player-source"
    saved = await client.post(f"/aether/session/{sid}/player", json={"player": {
        "name": "Rook",
        "concept": "claim runner",
        "resources": {"hp": {"max": 10}},
        "gear": ["Audit Knife"],
    }})
    assert saved.status_code == 200 and saved.json()["applied"] > 0

    store = proxy_app.state.store
    row = _session_row(store, sid)
    branch = row["active_branch"]
    apply_delta(
        store,
        row["session_id"],
        branch,
        _head(store, branch) + 1,
        [{"op": "player_seed", "entity": "rook", "card": {
            "hp": {"cur": 3, "max": 10},
        }}],
        "user",
        cfg,
    )

    prefill = (await client.get(f"/aether/session/{sid}/creator")).json()
    assert prefill["player"]["resources"]["hp"] == {"cur": 10, "max": 10}
    assert prefill["player_live"]["resources"]["hp"] == {"cur": 3, "max": 10}
    assert prefill["effects_live"] == current_state(store, branch)["effects"]

    # This is the exact shape the browser currently posts after applyPlayer: it keeps max HP
    # but has no current-HP input. Loading a committed card and pressing Save must be neutral.
    ui_roundtrip = deepcopy(prefill["player"])
    ui_roundtrip["resources"]["hp"] = {"max": 10}
    unchanged = await client.post(
        f"/aether/session/{sid}/player", json={"player": ui_roundtrip},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["applied"] == 0
    assert unchanged.json()["already_committed"] is True

    state = current_state(store, branch)
    assert state["player"]["rook"]["hp"] == {"cur": 3, "max": 10}
    assert _owned_quantity(state, "rook", "Audit Knife") == 1

    # An intentional authored edit remains supported, but it preserves live counters and treats
    # starting gear as a seed set rather than granting the same item again.
    ui_roundtrip["concept"] = "senior claim runner"
    edited = await client.post(
        f"/aether/session/{sid}/player", json={"player": ui_roundtrip},
    )
    assert edited.status_code == 200 and edited.json()["applied"] > 0
    state = current_state(store, branch)
    assert state["player"]["rook"]["concept"] == "senior claim runner"
    assert state["player"]["rook"]["hp"] == {"cur": 3, "max": 10}
    assert _owned_quantity(state, "rook", "Audit Knife") == 1


async def test_session_narrator_card_uses_authored_source_not_live_projection(
    client, proxy_app, cfg,
):
    sid = "session-card-authored-source"
    world = await client.post(f"/aether/session/{sid}/world", json={"world": {
        "name": "Sourcewake",
        "world_id": "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "locations": ["Source Quay"],
        "npcs": [{
            "name": "Mara",
            "role": "harbor guide",
            "home": "Source Quay",
        }],
    }})
    player = await client.post(f"/aether/session/{sid}/player", json={"player": {
        "name": "Rook",
        "resources": {"hp": {"max": 10}},
        "gear": ["Audit Knife"],
    }})
    assert world.status_code == 200 and world.json()["applied"] > 0
    assert player.status_code == 200 and player.json()["applied"] > 0

    store = proxy_app.state.store
    row = _session_row(store, sid)
    branch = row["active_branch"]
    apply_delta(
        store,
        row["session_id"],
        branch,
        max(1, _head(store, branch) + 1),
        [
            {"op": "entity_add", "name": "Tide Wisp", "kind": "npc"},
            {"op": "player_seed", "entity": "rook", "card": {
                "hp": {"cur": 3, "max": 10},
            }},
        ],
        "user",
        cfg,
    )

    prefill = (await client.get(f"/aether/session/{sid}/creator")).json()
    assert prefill["player"]["resources"]["hp"] == {"cur": 10, "max": 10}
    assert prefill["player_live"]["resources"]["hp"] == {"cur": 3, "max": 10}
    assert [npc["name"] for npc in prefill["world"]["npcs"]] == ["Mara"]
    assert sorted(npc["name"] for npc in prefill["world_live"]["npcs"]) == [
        "Mara",
        "Tide Wisp",
    ]

    response = await client.get(f"/aether/session/{sid}/narrator-card.json")
    assert response.status_code == 200
    seed = response.json()["data"]["extensions"]["aetherstate"]["seed"]
    observed = {
        "hp": seed["player"]["resources"]["hp"],
        "npcs": sorted(npc["name"] for npc in seed["world"]["npcs"]),
    }
    assert observed == {
        "hp": {"cur": 10, "max": 10},
        "npcs": ["Mara"],
    }


async def test_session_free_card_rejects_cross_document_entity_collision(client):
    response = await client.post("/aether/narrator-card", json={
        "world": {
            "name": "Collision Reach",
            "npcs": [{"name": "Mara Venn", "role": "guide"}],
        },
        "player": {"name": "Mara-Venn", "concept": "scout"},
    })

    assert response.status_code == 422
    assert "entity namespace collision" in response.json()["error"]


async def test_seed_rejection_is_non_2xx_and_reports_post_apply_truth(client):
    sid = "portable-seed-collision"
    response = await client.post(f"/aether/session/{sid}/seed", json={"seed": {
        "world": {
            "name": "Collision Reach",
            "npcs": [{"name": "Mara Venn", "role": "guide"}],
        },
        "player": {"name": "Mara-Venn", "concept": "scout"},
    }})

    assert response.status_code in {409, 422}
    body = response.json()
    assert body["world_seeded"] is False
    assert body["player_seeded"] is False
    assert body["complete"] is False
    assert body["applied"] == 0
    prefill = (await client.get(f"/aether/session/{sid}/creator")).json()
    assert prefill["world_seeded"] is False
    assert prefill["player"] is None


async def test_seed_idempotence_reports_confirmed_presence_not_attempt_flags(client):
    sid = "portable-seed-idempotent"
    seed = {
        "world": {"name": "Stable Reach", "world_id": "world_11111111111111111111111111111111"},
        "player": {"name": "Rook", "concept": "witness"},
    }
    first = await client.post(f"/aether/session/{sid}/seed", json={"seed": seed})
    second = await client.post(f"/aether/session/{sid}/seed", json={"seed": seed})

    assert first.status_code == 200
    assert first.json()["world_seeded"] and first.json()["player_seeded"]
    assert first.json()["complete"] and first.json()["applied"] > 0
    assert second.status_code == 200
    assert second.json()["world_seeded"] and second.json()["player_seeded"]
    assert second.json()["complete"] and second.json()["applied"] == 0
    assert second.json()["already_present"] is True


async def test_seed_requires_the_requested_world_and_player_identities(client):
    first = {
        "world": {"name": "First Reach", "world_id": "world_22222222222222222222222222222222"},
        "player": {"name": "Rook"},
    }
    wrong = {
        "world": {"name": "Other Reach", "world_id": "world_33333333333333333333333333333333"},
        "player": {"name": "Mara"},
    }
    admitted = await client.post(
        "/aether/session/seed-identity/seed", json={"seed": first},
    )
    refused = await client.post(
        "/aether/session/seed-identity/seed", json={"seed": wrong},
    )

    assert admitted.status_code == 200 and admitted.json()["complete"]
    assert refused.status_code == 409
    assert refused.json()["world_seeded"] is False
    assert refused.json()["player_seeded"] is False
    assert refused.json()["complete"] is False
    assert refused.json()["applied"] == 0


async def test_genesis_never_trusts_an_unverified_structured_seed_flag(client):
    unverified = await client.post(
        "/aether/session/unverified-structured/genesis",
        json={
            "card": "Narrator prose without an admitted Creator seed.",
            "greeting": "Opening.",
            "speaker": "Narrator",
            "card_role": "narrator",
            "structured_seed": True,
        },
    )
    assert unverified.status_code == 200
    assert unverified.json()["structured_seed"] is False

    seed = {
        "world": {
            "name": "Verified Reach",
            "world_id": "world_55555555555555555555555555555555",
        },
        "player": {"name": "Rook"},
    }
    admitted = await client.post(
        "/aether/session/verified-structured/seed", json={"seed": seed},
    )
    assert admitted.status_code == 200 and admitted.json()["complete"]
    verified = await client.post(
        "/aether/session/verified-structured/genesis",
        json={
            "card": "Narrator prose backed by an admitted Creator seed.",
            "greeting": "Opening.",
            "speaker": "Narrator",
            "card_role": "narrator",
            "structured_seed": True,
        },
    )
    assert verified.status_code == 200
    assert verified.json()["structured_seed"] is True
