from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "src" / "aetherstate" / "static" / "console.html"


def test_console_exposes_separate_player_lesson_verticals():
    html = CONSOLE.read_text(encoding="utf-8")

    assert '"Player Lessons"' in html
    assert "Narration behavior" in html
    assert "Intent interpretation" in html
    assert "Future lesson types are not active." not in html
    assert "Misunderstanding note (for your record only)" in html
    assert "Correct interpretation note (for your record only)" in html
    assert "never parses these notes" in html
    assert "never sends them to the narrator" in html
    assert "after recognition and before contextual binding" in html
    assert "Exact PlayerLex meaning (required)" in html
    assert "ActionLex" in html and "ReferentLex" in html
    assert "This lesson corrects the ${role}." in html
    assert 'anchor?.lex_id==="action"?"action":anchor?.lex_id==="referent"?"target":null' in html
    assert "Save locally and enable" in html
    assert "Test with sample turn" in html
    assert "Test interpretation" in html
    assert "Would be retrieved" in html
    assert "Application evaluated: no" in html
    assert "Would apply" not in html
    assert "never stored" in html
    assert 'id="lessonsample" maxlength="2000"' in html
    assert 'id="lessonmisunderstanding" maxlength="1000"' in html
    assert 'id="lessonunderstanding" maxlength="1000"' in html
    assert "older revision delivered" in html
    assert "older revision applied" in html
    assert "selected revision" in html and "current revision" in html
    assert "applied revision" in html
    assert "Latest narrator delivery" in html
    assert "provider returned response headers" in html
    assert "does not prove narrator adherence or completion" in html
    assert "Latest application" in html
    assert "explicit local" in html
    assert "may be sent to your configured model provider" in html
    assert "Intent notes stay local" in html
    assert "PlayerLex entry" in html
    assert "Secure removal limit" in html
    assert "External backups" in html
    assert "already received or retained" in html
    assert "type.disabled=true" in html

    for endpoint in (
        '"/aether/player-lessons"',
        "/aether/player-lessons/selections?session_id=",
        "/aether/player-lessons/applications?session_id=",
        '"/aether/player-lessons/test"',
        "/enabled",
    ):
        assert endpoint in html

    for field in (
        'narration_behavior:"Narration behavior"',
        'intent_interpretation:"Intent interpretation"',
        "misunderstanding",
        "correct_interpretation",
        "anchor_entry_id",
        "expected_revision",
        "expected_fingerprint",
        "sample_text",
        "narration_mode",
    ):
        assert field in html

    for scope in ("every_rpg_turn", "exploration", "combat_opening", "combat_exchange"):
        assert scope in html

    sample_modes = html.split('id="lessontestscope"', 1)[1].split("</select>", 1)[0]
    assert "LESSON_MODES.map" in sample_modes
    assert "every_rpg_turn" not in sample_modes

    intent_payload = html.split('if(effect_type==="intent_interpretation")', 1)[1].split(
        "const do_text", 1
    )[0]
    assert "misunderstanding" in intent_payload
    assert "correct_interpretation" in intent_payload
    assert "do:" not in intent_payload and "avoid:" not in intent_payload

    application_view = html.split("function playerLessonApplicationView", 1)[1].split(
        "function playerLessonAnchorView", 1
    )[0]
    assert "delivered" not in application_view

    assert 'else if(renderTab==="Player Lessons")h=await playerLessonsTab()' in html
    assert "if(seq===RENDER_SEQ&&tab===renderTab){v.innerHTML=h;" in html
    assert 'if(renderTab==="PlayerLex")playerLexGuide()' in html
    navigation = html.split("async function go(t){", 1)[1].split("async function render()", 1)[0]
    assert 'if(t==="Player Lessons"||t==="PlayerLex"){S=null;J=null' in navigation
    assert "await load(PRIVILEGED_STATE_TABS.has(t))" in navigation
    assert navigation.index('if(t==="Player Lessons"||t==="PlayerLex")') < navigation.index(
        "await load(PRIVILEGED_STATE_TABS.has(t))"
    )
    lessons_tab = html.split("async function playerLessonsTab(){", 1)[1].split(
        "function playerLessonLatest", 1
    )[0]
    assert lessons_tab.startswith('LESSON_EDIT="";')


def test_console_intent_payload_uses_dedicated_fields_and_typed_anchor(tmp_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console payload regression")

    html = CONSOLE.read_text(encoding="utf-8")

    def function_source(name: str, next_name: str) -> str:
        start = html.index(f"function {name}(")
        end = html.index(f"\nfunction {next_name}(", start)
        return html[start:end].strip()

    role_source = function_source("playerLessonIntentRole", "playerLessonEligibleAnchors")
    chosen_source = function_source("playerLessonChosenAnchor", "playerLessonFormPayload")
    payload_source = function_source("playerLessonFormPayload", "playerLessonSelections")
    script = f"""
import assert from "node:assert/strict";

const fields = {{
  "#lessontype": {{ value: "intent_interpretation" }},
  "#lessontitle": {{ value: "Keep cover references out of the target slot" }},
  "#lessonscope": {{ value: "combat_exchange" }},
  "#lessonanchor": {{ value: "action-entry" }},
  "#lessonmisunderstanding": {{ value: "AetherState treats the cover reference as my target." }},
  "#lessonunderstanding": {{ value: "The action is taking cover; the named person is not attacked." }},
  "#lessondo": {{ value: "narration-only do" }},
  "#lessonavoid": {{ value: "narration-only avoid" }},
}};
const $ = selector => fields[selector] || null;
const LESSON_SCOPES = {{ every_rpg_turn: "Every RPG turn", exploration: "Exploration", combat_opening: "Combat opening", combat_exchange: "Combat exchange" }};
const LESSON_ANCHORS = [
  {{ entry_id: "action-entry", lex_id: "action" }},
  {{ entry_id: "target-entry", lex_id: "referent" }},
  {{ entry_id: "scene-entry", lex_id: "scene" }},
];
const messages = [];
function toast(message, bad) {{ messages.push([message, bad]); }}

const playerLessonIntentRole = eval("(" + {json.dumps(role_source)} + ")");
const playerLessonChosenAnchor = eval("(" + {json.dumps(chosen_source)} + ")");
const playerLessonFormPayload = eval("(" + {json.dumps(payload_source)} + ")");

assert.equal(playerLessonIntentRole(LESSON_ANCHORS[0]), "action");
assert.equal(playerLessonIntentRole(LESSON_ANCHORS[1]), "target");
assert.equal(playerLessonIntentRole(LESSON_ANCHORS[2]), null);

const intent = playerLessonFormPayload();
assert.deepEqual(intent, {{
  effect_type: "intent_interpretation",
  title: "Keep cover references out of the target slot",
  scope: "combat_exchange",
  misunderstanding: "AetherState treats the cover reference as my target.",
  correct_interpretation: "The action is taking cover; the named person is not attacked.",
  anchor_entry_id: "action-entry",
}});
assert.equal(Object.hasOwn(intent, "do"), false);
assert.equal(Object.hasOwn(intent, "avoid"), false);

fields["#lessonanchor"].value = "";
assert.equal(playerLessonFormPayload(), null);
assert.match(messages.at(-1)[0], /ActionLex or ReferentLex anchor/);

fields["#lessonanchor"].value = "scene-entry";
assert.equal(playerLessonFormPayload(), null);
assert.match(messages.at(-1)[0], /requires an ActionLex or ReferentLex anchor/);

fields["#lessontype"].value = "narration_behavior";
fields["#lessonanchor"].value = "";
const narration = playerLessonFormPayload();
assert.equal(narration.effect_type, "narration_behavior");
assert.equal(narration.do, "narration-only do");
assert.equal(narration.avoid, "narration-only avoid");
assert.equal(Object.hasOwn(narration, "misunderstanding"), false);
assert.equal(Object.hasOwn(narration, "correct_interpretation"), false);
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_console_intent_application_is_separate_from_narrator_delivery():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console application regression")

    html = CONSOLE.read_text(encoding="utf-8")

    def function_source(name: str, next_name: str) -> str:
        start = html.index(f"function {name}(")
        end = html.index(f"\nfunction {next_name}(", start)
        return html[start:end].strip()

    latest_source = function_source("playerLessonLatestApplication", "playerLessonApplicationView")
    view_source = function_source("playerLessonApplicationView", "playerLessonAnchorView")
    script = f"""
import assert from "node:assert/strict";

const playerLessonId = entry => entry?.lesson_id || entry?.id || "";
const playerLessonRevision = entry => entry?.revision ?? entry?.lesson_revision;
const esc = value => String(value ?? "");
let sid = "session-1";
let LESSON_APPLICATION_ERROR = "";
let LESSON_APPLICATIONS = [{{
  lesson_id: "lesson-1",
  lesson_revision: 1,
  turn_index: 4,
  reason: "scope_and_anchor_match",
  applied: true,
  application_stage: "post_contextual_binding",
  delivered: true,
}}];

const playerLessonLatestApplication = eval("(" + {json.dumps(latest_source)} + ")");
const playerLessonApplicationView = eval("(" + {json.dumps(view_source)} + ")");

const historical = playerLessonApplicationView({{ lesson_id: "lesson-1", revision: 2 }});
assert.match(historical, /older revision applied/);
assert.match(historical, /receipt confirmed after contextual binding/);
assert.match(historical, /applied revision 1, current revision 2/);
assert.doesNotMatch(historical, /delivered/);

const current = playerLessonApplicationView({{ lesson_id: "lesson-1", revision: 1 }});
assert.match(current, /applied before contextual binding/);
assert.doesNotMatch(current, /delivered/);

LESSON_APPLICATIONS[0].applied = false;
LESSON_APPLICATIONS[0].status = "matched";
const abstained = playerLessonApplicationView({{ lesson_id: "lesson-1", revision: 1 }});
assert.match(abstained, /matched, not applied/);
assert.doesNotMatch(abstained, /delivered/);
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_console_anchor_view_distinguishes_two_approvals_for_the_same_meaning():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console anchor regression")

    html = CONSOLE.read_text(encoding="utf-8")
    start = html.index("function playerLessonAnchorView(")
    end = html.index("\nfunction playerLessonProvenance(", start)
    source = html[start:end].strip()
    script = f"""
import assert from "node:assert/strict";

const esc = value => String(value ?? "");
const playerLessonAnchor = entry => entry?.anchor || null;
const playerLessonType = entry => entry?.effect_type || "narration_behavior";
const fingerprint = "sha256:" + "a".repeat(64);
const LESSON_ANCHORS = [
  {{ entry_id: "entry-a", lex_id: "action", concept_id: "movement", meaning_fingerprint: fingerprint, surface: "Glass Read" }},
  {{ entry_id: "entry-b", lex_id: "action", concept_id: "movement", meaning_fingerprint: fingerprint, surface: "Crystal Scan" }},
];
const playerLessonAnchorView = eval("(" + {json.dumps(source)} + ")");

const first = playerLessonAnchorView({{
  anchor_status: "current",
  anchor: {{ entry_id: "entry-a", lex_id: "action", concept_id: "movement", meaning_fingerprint: fingerprint }},
}});
assert.match(first, /Glass Read/);
assert.match(first, /entry entry-a/);
assert.doesNotMatch(first, /Crystal Scan/);

const second = playerLessonAnchorView({{
  anchor_status: "current",
  anchor: {{ entry_id: "entry-b", lex_id: "action", concept_id: "movement", meaning_fingerprint: fingerprint }},
}});
assert.match(second, /Crystal Scan/);
assert.match(second, /entry entry-b/);
assert.doesNotMatch(second, /Glass Read/);
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_console_javascript_has_valid_syntax(tmp_path: Path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console syntax regression")

    html = CONSOLE.read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script_path = tmp_path / "console.js"
    script_path.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_stale_player_lessons_render_cannot_replace_a_newer_tab():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the Console race regression")

    html = CONSOLE.read_text(encoding="utf-8")
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
let tab = "Player Lessons";
let S = null;
let RENDER_SEQ = 0;
let UI_VIEW = "guided";
const PRIVILEGED_STATE_TABS = new Set(["Overview", "Edit"]);
const loadCalls = [];
let releaseLessons;
const pendingLessons = new Promise(resolve => {{ releaseLessons = resolve; }});
async function playerLessonsTab() {{ return await pendingLessons; }}
async function playerLexTab() {{ return "PLAYERLEX"; }}
function playerLexReset() {{}}
function playerLessonReset() {{}}
function guidedIntro() {{ return ""; }}
function nav() {{}}
async function load(privileged) {{ loadCalls.push(privileged); }}
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
assert.deepEqual(loadCalls, [true]);

releaseLessons("STALE PLAYER LESSONS");
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
