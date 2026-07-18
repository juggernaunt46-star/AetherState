"""Player-guided and expert UI faces share one unchanged AetherState surface."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREATOR = ROOT / "src" / "aetherstate" / "static" / "creator.html"
CONSOLE = ROOT / "src" / "aetherstate" / "static" / "console.html"


def test_creator_has_persistent_guided_and_expert_views_without_replacing_actions():
    html = CREATOR.read_text(encoding="utf-8")

    assert 'const CREATOR_VIEW_KEY="aether_creator_view_v1"' in html
    assert "function setCreatorView(mode,announce=true)" in html
    assert 'id="viewGuided"' in html and 'id="viewExpert"' in html
    assert 'document.documentElement.dataset.view=CREATOR_VIEW' in html
    assert "installCreatorHelp()" in html
    assert "What it does:" in html
    assert "Good starting point" in html
    assert "Watch out" in html

    # View changes are presentation-only: the established actions remain the owners.
    for action in (
        'onclick="authorWorld()"',
        'onclick="authorPlayer()"',
        'onclick="genNarratorCard()"',
        'onclick="saveWorld()"',
        'onclick="savePlayer()"',
        'onclick="loadCommitted(\'world\')"',
        'onclick="loadCommitted(\'player\')"',
    ):
        assert action in html


def test_console_guided_navigation_maps_to_existing_tabs_and_expert_keeps_all_tools():
    html = CONSOLE.read_text(encoding="utf-8")

    assert 'const CONSOLE_VIEW_KEY="aether_console_view_v1"' in html
    assert "function setConsoleView(mode,announce=true,rerender=true)" in html
    assert 'const TABS=["Player","PlayerLex","Player Lessons","Overview","Edit","Sessions","Connection","Status","Models","Raw"]' in html
    assert 'const GUIDED_TABS=["Player","PlayerLex","Player Lessons","Overview","Edit","Sessions","Help"]' in html
    assert 'Player:"My Character"' in html
    assert 'PlayerLex:"My Words"' in html
    assert '"Player Lessons":"Story Preferences"' in html
    assert 'Overview:"World & Story"' in html
    assert 'Edit:"Customize This Game"' in html
    assert 'Sessions:"My Games"' in html
    assert "if(UI_VIEW===\"expert\")h+=`<div class=\"card\"><h3>Raw ops (power users)</h3>" in html

    # Guided and Expert faces still route through the same mutation and data loaders.
    assert "async function go(t)" in html
    assert "await load(PRIVILEGED_STATE_TABS.has(t))" in html
    assert "function applyForm()" in html
    assert "function applyRaw()" in html
    assert '$("#ovr").onchange=async e=>' in html


def test_guided_ui_explains_benefit_risk_and_best_practice_without_hover_only_help():
    creator = CREATOR.read_text(encoding="utf-8")
    console = CONSOLE.read_text(encoding="utf-8")

    assert "Why use it" in console
    assert "Watch out" in creator and "Watch out" in console
    assert "Good practice" in console
    assert "Good starting point" in creator
    assert 'class="card guide-card guided-only"' in creator
    assert 'class="card guide-card"' in console
    assert ".help-dot:hover::after,.help-dot:focus-visible::after" in creator


def test_my_words_guidance_is_contextual_collapsible_and_never_auto_overwrites():
    html = CONSOLE.read_text(encoding="utf-8")

    assert 'id="plguide"' in html
    assert "What do these choices mean?" in html
    assert 'const PLAYERLEX_GUIDE_KEY="aether_console_playerlex_guide_v1"' in html
    assert "PLAYERLEX_KIND_HELP" in html and "PLAYERLEX_MEANING_HELP" in html
    assert "Use this template" in html
    assert 'if(field.value.trim())return toast("Your phrase already has text' in html
    assert "playerLexKindChanged()" in html
    assert "playerLexLexChanged();playerLexGuide()" in html


def test_guided_my_games_shows_creation_and_activity_chronology_while_expert_keeps_ids():
    html = CONSOLE.read_text(encoding="utf-8")

    assert 'if(UI_VIEW==="guided"){' in html
    assert "Newest" in html and "Oldest" in html and "Most recently played" in html
    assert "Created:" in html and "Last played:" in html
    assert "sessionAbsolute" in html and "sessionRelative" in html
    assert "Open world &amp; story" in html
    assert '<th>id</th><th>frontend</th><th>turn</th>' in html
