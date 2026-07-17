"""Browser-neutral contract checks for the single-file Creator UI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_creator_ui_keeps_limits_visible_and_roundtrips_complete_structures() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "creator_resource_contract.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        "Creator UI contract failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "creator resource contract smoke: PASS" in result.stdout
