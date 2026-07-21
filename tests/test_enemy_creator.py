"""Player-facing Enemy Workshop preview and authority-boundary regressions."""
from __future__ import annotations


async def test_enemy_workshop_previews_grounded_moves_without_runtime_authority(client):
    response = await client.post(
        "/aether/enemies/preview",
        json={
            "name": "Vanta Drone",
            "tier": "elite",
            "role": "artillery",
            "type": "combat drone",
            "armament": "plasma carbine",
            "powers": "smoke screen and sensor array",
            "description": "A patient sentry guarding the flooded archive.",
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["schema"] == "aetherstate-enemy-preview/1"
    assert preview["kit"]["schema"] == "enemy-kit/1"
    assert preview["kit"]["tier"] == "elite"
    assert set(preview["kit"]["basis"]) == {"firearm", "technology"}
    assert len(preview["kit"]["moves"]) == 3
    assert all(move["channel"] == "hp" for move in preview["kit"]["moves"])
    assert all(move["target_rule"] == "player" for move in preview["kit"]["moves"])
    assert preview["authority"] == {
        "scope": "authoring_preview_only",
        "supported_channel": "hp",
        "target_rule": "player",
        "target_count": "single",
        "reusable_blueprint": False,
        "runtime_admission": False,
        "settlement": False,
    }


async def test_enemy_workshop_does_not_turn_references_or_resistance_into_powers(client):
    response = await client.post(
        "/aether/enemies/preview",
        json={
            "name": "Demon Hunter",
            "tier": "standard",
            "role": "demon hunter",
            "type": "human",
            "armament": "silver sword",
            "powers": "fire resistance and a laser sight",
            "description": "Studies ghosts and collects broken drone parts.",
        },
    )

    assert response.status_code == 200
    kit = response.json()["kit"]
    assert "martial" in kit["basis"]
    assert not ({"magic", "supernatural", "technology", "firearm"} & set(kit["basis"]))


async def test_enemy_workshop_rejects_invalid_authoring_payloads(client):
    missing = await client.post("/aether/enemies/preview", json={"name": ""})
    wrong_tier = await client.post(
        "/aether/enemies/preview", json={"name": "Gate Guard", "tier": "mythic"}
    )
    unknown = await client.post(
        "/aether/enemies/preview", json={"name": "Gate Guard", "spawn_now": True}
    )

    assert missing.status_code == 422
    assert missing.json()["error"] == "enemy name is required"
    assert wrong_tier.status_code == 422
    assert "tier must be" in wrong_tier.json()["error"]
    assert unknown.status_code == 422
    assert "spawn_now" in unknown.json()["error"]


async def test_creator_exposes_enemy_workshop_and_honest_preview_boundary(client):
    response = await client.get("/aether/creator")

    assert response.status_code == 200
    page = response.text
    for marker in (
        'id="tab_enemy"',
        'id="enemy" class="tab"',
        'id="e_preview"',
        'id="e_add_world"',
        'id="e_home"',
        'id="e_home_locations"',
        'for="e_home"',
        'aria-describedby="e_home_status"',
        'data-field="home" maxlength="80"',
        'aria-live="polite"',
        "function enemyDoc()",
        "async function previewEnemy()",
        "function addEnemyToWorld()",
        "Choose an existing World location before adding this enemy",
        "home:resolvedHome",
        'target.id==="e_home"',
        'target.removeAttribute("aria-invalid")',
        "function resolveWorldLocationName(value)",
        "The World already has 20 NPCs",
        "single-target HP moves",
        "not a spawned or admitted combatant",
    ):
        assert marker in page
