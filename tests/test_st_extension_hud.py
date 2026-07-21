"""ST-extension HUD guards (2026-07-09) — "ensure this never happens again".

Two layers, cheapest first:

1. Static integrity — the extension file must still CONTAIN every render entry point and
   still close its IIFE. Catches silent truncation (the known Cowork/Windows write hazard)
   and accidental deletion of a renderer, with no node dependency.
2. Render smoke (tests/st_hud_smoke.mjs) — runs the REAL index.js in a stub DOM against a
   full synthetic payload: boot-minimized must self-label, expand must re-render, all 8 tabs
   must emit their markers, the war-room lane must render, and a renderer throw must surface
   as a visible error line instead of stale content. Skips when node isn't on PATH (CI floor);
   on Bean's machine node is always present (SillyTavern runs on it).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "st-extension" / "index.js"
STYLE = ROOT / "st-extension" / "style.css"

# Every render entry point the HUD dispatches to — if ANY goes missing the sheet silently
# loses a surface, which is exactly the failure class Bean hit. Extend when adding a tab.
_MARKERS = (
    "function renderHud",
    "function renderCompact",
    "function renderVitals",
    "function renderKnowledge",
    "function renderWarRoom",
    "function renderWorldPulse",
    "function worldSignal",
    "function renderEnemyAction",
    "function worldEffectLine",
    "function capabilityAvailability",
    "function renderQuest",
    "function tabChar",
    "function tabSkills",
    "function tabAbilities",
    "function tabRolls",
    "function tabGear",
    "function tabInventory",
    "function tabStatus",
    "function tabWorld",
    "window.aetherHudExpand",          # the minimized strip's one-tap way home
    "aes-expand",                      # the strip's self-label (a minimized HUD must SAY so)
    "HUD render error",                # visible fail-open inside the renderers
    "combat-reference/composer build", # build tag — bump it when the extension changes
    "world-overlay (2026-07-17)",       # typed event effects are visible in the HUD
    "aes-enemy-intent",                # one visible committed future enemy move
    "aes-enemy-action",                # the exact enemy move that resolved this turn
    "function renderEnemyOptions",      # counterplay has its own plain-language block
    "aetherstate_chat_id",             # bind one SID to one SillyTavern chat
    "aetherstate_parent_sid",          # explicit copied-branch parent identity
    "aetherstate_fork_pos",            # exact copied chat snapshot length
    "function composeRollDraft",       # one UI intent owns one draft mechanic action
    "window.aetherSetSeparateRoll",    # explicit one-shot independent-action path
    "function rollTruthContent",       # backend-owned target-impact text, no tier inference
    "WHAT YOU DID",                    # current-turn Player impacts in the War Room
    "HUD clarity (2026-07-18)",        # tooltip/discoverability/density pass
    "card-seed reliability (2026-07-21)", # verified portable seed precedes genesis
    "Consent boundaries",              # explicit boundaries live under Status
)


def test_extension_has_every_render_surface():
    src = EXT.read_text(encoding="utf-8")
    missing = [m for m in _MARKERS if m not in src]
    assert not missing, (
        f"st-extension/index.js lost {missing} — truncated write or deleted renderer. "
        "Restore from backup; never ship a HUD missing a surface."
    )
    assert src.rstrip().endswith("})();"), (
        "index.js no longer closes its IIFE — the file tail was truncated."
    )


def test_genesis_handoff_preserves_structured_seed_boundary():
    """Creator state stays authoritative and dialogue examples stay illustrative."""
    src = EXT.read_text(encoding="utf-8")
    assert "structured_seed: Boolean(verifiedFingerprint)" in src
    assert "seed_fingerprint: verifiedFingerprint" in src
    assert "sub(ch.mes_example)" not in src


def test_war_room_theme_and_viewport_contract():
    css = STYLE.read_text(encoding="utf-8")
    for undefined in ("var(--accent)", "var(--ok)", "var(--text)"):
        assert undefined not in css, f"War Room uses undefined theme variable {undefined}"
    assert "calc(100vw - 16px)" in css
    assert "calc(100dvh - 16px)" in css
    assert ".aes-war-section-h" in css
    assert ".aes-knowledge-row" in css
    assert ".aes-knowledge-status.history" in css
    assert ".aes-world-effect" in css
    assert ".aes-quest.unavailable" in css
    assert ".aes-rollbtn.world-unavailable" in css
    assert ".aes-world-pulse" in css
    assert ".aes-tab-badge" in css
    assert ".aes-help" in css


def test_installed_copy_not_stale():
    """The copy SillyTavern actually loads must match the source — a stale install shows
    yesterday's HUD no matter how correct the repo is (live repro 2026-07-09)."""
    installed = (ROOT.parent / "SillyTavern" / "data" / "default-user" / "extensions"
                 / "AetherState" / "index.js")
    if not installed.exists():
        pytest.skip("no local SillyTavern install next to the repo")
    norm = lambda b: b.replace(b"\r\n", b"\n")  # noqa: E731 — EOL style differs per mirror
    assert norm(installed.read_bytes()) == norm(EXT.read_bytes()), (
        "SillyTavern's installed AetherState extension differs from st-extension/index.js — "
        "copy the extension over (st-extension -> SillyTavern/data/default-user/extensions/"
        "AetherState) or the UI will run stale code."
    )
    installed_style = installed.with_name("style.css")
    assert installed_style.exists() and norm(installed_style.read_bytes()) == norm(
        STYLE.read_bytes()), "SillyTavern's installed style.css is stale"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_hud_smoke_renders_every_surface():
    r = subprocess.run(
        ["node", str(ROOT / "tests" / "st_hud_smoke.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, (
        f"HUD render smoke FAILED:\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
