"""Player Console privacy and explicit owner/debug inspection boundaries."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "src" / "aetherstate" / "static" / "console.html"


def test_console_player_and_raw_use_only_the_whitelisted_hud_projection():
    html = CONSOLE.read_text(encoding="utf-8")

    assert 'const PRIVILEGED_STATE_TABS=new Set(["Overview","Edit"])' in html
    assert "if(privileged){" in html
    assert "}else{S=null;J=null}" in html
    assert "await load(false)" in html
    assert "HUD.player_safe_raw||HUD" in html
    assert "JSON.stringify(HUD.player_safe_raw,null,2)" in html
    assert "JSON.stringify(S.state,null,2)" not in html
    assert 'e.cause_visible===true&&e.cause?e.cause:"cause not known"' in html

    player_view = html.split("function playerView(){", 1)[1].split(
        "/* ---------- render", 1
    )[0]
    assert "uniqueDisplayProse([sk.desc])" in player_view
    assert "Array.isArray(sk.governs)" in player_view
    assert "Used for:" in player_view
    assert "uniqueDisplayProse([a.effect,a.desc])" in player_view
    assert "g.aura" in player_view
    assert "p.stowed_gear" in player_view


def test_stale_console_owner_cookie_is_refreshed_before_privileged_state_retry():
    html = CONSOLE.read_text(encoding="utf-8")

    assert "async function refreshOwnerInspection()" in html
    assert 'await fetch("/aether/console",{credentials:"same-origin"})' in html
    assert "if(r.status===403&&ownerInspectionRoute(u)&&!retried)" in html
    assert "return j(u,o,true)" in html


def test_session_picker_prefers_legible_world_and_player_names():
    html = CONSOLE.read_text(encoding="utf-8")

    assert "x.label||[x.world_name,x.player_name].filter(Boolean).join(" in html
    assert "x.external_id||x.session_id" in html


async def test_raw_state_and_journal_require_console_owner_capability(client):
    await client.post("/aether/session/privacy-route/world", json={
        "world": {"name": "Privacy Route", "locations": ["Harbor"]},
    })

    assert (await client.get("/aether/session/privacy-route/state")).status_code == 200
    assert (await client.get("/aether/session/privacy-route/journal")).status_code == 200

    client.cookies.clear()
    denied_state = await client.get("/aether/session/privacy-route/state")
    denied_journal = await client.get("/aether/session/privacy-route/journal")
    assert denied_state.status_code == 403
    assert denied_journal.status_code == 403
    assert "owner/debug inspection" in denied_state.json()["error"]

    player = await client.get("/aether/session/privacy-route/hud")
    assert player.status_code == 200
    assert player.json()["player_safe_raw"]["schema"] == "aetherstate-player-inspection/1"

    opened = await client.get("/aether/console")
    assert opened.status_code == 200
    cookie = opened.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert (await client.get("/aether/session/privacy-route/state")).status_code == 200
