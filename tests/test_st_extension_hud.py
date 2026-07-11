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

# Every render entry point the HUD dispatches to — if ANY goes missing the sheet silently
# loses a surface, which is exactly the failure class Bean hit. Extend when adding a tab.
_MARKERS = (
    "function renderHud",
    "function renderCompact",
    "function renderVitals",
    "function renderWarRoom",
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
    "auto-compact toggle build",       # build tag — bump it when the extension changes
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_hud_smoke_renders_every_surface():
    r = subprocess.run(
        ["node", str(ROOT / "tests" / "st_hud_smoke.mjs")],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, (
        f"HUD render smoke FAILED:\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
