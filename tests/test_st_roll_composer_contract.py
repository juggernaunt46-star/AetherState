"""Static guardrails for the SillyTavern roll-composer ownership boundary.

The runtime behavior is exercised by ``st_hud_smoke.mjs``.  These checks keep the
visible one-action default and explicit independent-action control from silently
disappearing during unrelated HUD work.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "st-extension" / "index.js"
STYLE = ROOT / "st-extension" / "style.css"


def test_roll_composer_has_one_action_default_and_explicit_separate_path() -> None:
    src = EXT.read_text(encoding="utf-8")
    assert "function composeRollDraft" in src
    assert "window.aetherSetSeparateRoll" in src
    assert "window.aetherSeparateRollArmed" in src
    assert "Add next as a separate action" in src
    assert "upgrades that draft" in src


def test_roll_history_has_stable_visible_row_labels() -> None:
    src = EXT.read_text(encoding="utf-8")
    css = STYLE.read_text(encoding="utf-8")
    assert 'class="aes-roll-history"' in src
    assert "${kind} \u00b7 ${esc(label)}" in src
    assert "Turn ${esc(r.turn)}" in src
    assert ".aes-roll-history" in css
    assert ".aes-roll-separate" in css
