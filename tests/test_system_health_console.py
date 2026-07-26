from __future__ import annotations

from pathlib import Path


CONSOLE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "aetherstate"
    / "static"
    / "console.html"
)


def _status_tab_source() -> str:
    source = CONSOLE.read_text(encoding="utf-8")
    return source.split("async function statusTab()", 1)[1].split(
        "async function modelsTab()", 1
    )[0]


def test_status_tab_adds_health_without_displacing_existing_proxy_controls() -> None:
    source = _status_tab_source()

    assert "<h3>Proxy</h3>" in source
    assert "proofreader" in source
    assert "director" in source
    assert "prompt cache" in source
    assert "System Health" in source
    assert "/aether/health/diagnostics" in source


def test_status_tab_tolerates_pre_health_responses() -> None:
    source = _status_tab_source().replace(" ", "")

    assert "d.health||" in source
    assert "conditions||[]" in source


def test_every_dynamic_health_identity_uses_the_console_escape_boundary() -> None:
    source = _status_tab_source().replace(" ", "")

    assert "esc(condition.subsystem)" in source
    assert "esc(condition.error_code)" in source
    assert "esc(condition.classification)" in source
    assert "esc(condition.correlation_id)" in source
