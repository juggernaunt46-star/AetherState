from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_stale_playerlex_render_cannot_replace_the_newly_selected_tab():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console race regression")

    html = (ROOT / "src" / "aetherstate" / "static" / "console.html").read_text(encoding="utf-8")
    go_start = html.index("async function go(t)")
    render_start = html.index("async function render()", go_start)
    boot_start = html.index("async function boot()", render_start)
    go_source = html[go_start:render_start].strip()
    render_source = html[render_start:boot_start].strip()

    script = f"""
import assert from "node:assert/strict";

const view = {{ innerHTML: "initial" }};
const $ = selector => {{
  assert.equal(selector, "#view");
  return view;
}};
let tab = "PlayerLex";
let S = null;
let RENDER_SEQ = 0;
let releasePlayerLex;
const pendingPlayerLex = new Promise(resolve => {{ releasePlayerLex = resolve; }});
async function playerLexTab() {{ return await pendingPlayerLex; }}
function playerLexReset() {{}}
function nav() {{}}
async function load() {{}}
function headerRefresh() {{}}
function playerView() {{ return "PLAYER"; }}
function overview() {{ return "OVERVIEW"; }}
function editTab() {{ return "EDIT"; }}
async function sessionsTab() {{ return "SESSIONS"; }}
async function connectionTab() {{ return "CONNECTION"; }}
async function statusTab() {{ return "STATUS"; }}
async function modelsTab() {{ return "MODELS"; }}
function esc(value) {{ return String(value); }}

const render = eval("(" + {json.dumps(render_source)} + ")");
const go = eval("(" + {json.dumps(go_source)} + ")");

const staleRender = render();
await Promise.resolve();
await go("Overview");
assert.equal(view.innerHTML, "OVERVIEW");

releasePlayerLex("STALE PLAYERLEX");
await staleRender;
assert.equal(view.innerHTML, "OVERVIEW");
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
