// st_hud_smoke.mjs — the ST-extension HUD render guard (2026-07-09, Bean's directive:
// "ensure this never happens again").
//
// WHY THIS EXISTS: after v1.13.0 the HUD appeared to show "nothing beyond hp/stamina/mana".
// Nothing was actually broken — the HUD had been left MINIMIZED (settings.hud.compact=true)
// and the strip carried no label saying so. `node --check` can never catch that class of bug
// (nor a runtime throw inside a renderer, nor a truncated file that still parses). This
// harness runs the REAL st-extension/index.js in a stub DOM against a full-fat SYNTHETIC
// payload and asserts EVERY render path emits its real content:
//   boot minimized -> the strip must label itself + offer expand; expand must change content;
//   all 8 tabs must render their markers; the war-room lane must render; a renderer throw
//   must surface as a VISIBLE error line, never stale content.
// Run directly (`node tests/st_hud_smoke.mjs`) or via tests/test_st_extension_hud.py.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = fs.readFileSync(path.join(ROOT, "st-extension", "index.js"), "utf8");

process.on("unhandledRejection", (e) => { fail("unhandled rejection: " + (e && e.stack || e)); done(); });

// ------------------------------ synthetic full-fat payload ------------------------------
// Every field every tab renders, no real campaign data. Extend when a renderer grows a field.
const payload = {
  spec: "rpg", frozen: false, turn: 12,
  scene: { location: "old_mill", phase: "combat", mode: "", time_of_day: "dusk", day: 3,
           calendar_note: "", present: ["Mira", "Raider"] },
  players: [{
    eid: "player_testa", name: "Testa Vector", level: 3, xp: 140, stat_points: 1,
    mood: "level · steady", appearance: "A scarred courier with road-bleached hair.",
    concept: "Wasteland courier", species: "human", pronouns: "she/her", sex: "female",
    hp: { cur: 14, max: 20 },
    resources: { stamina: { cur: 8, max: 12 }, mana: { cur: 5, max: 10 } },
    stats: [
      { key: "might", val: 12, mod: 1 }, { key: "agility", val: 14, mod: 2 },
      { key: "wits", val: 11, mod: 0 }, { key: "will", val: 10, mod: 0 },
      { key: "presence", val: 9, mod: -1 }, { key: "vitality", val: 13, mod: 1 }],
    skills: [
      { id: "blades", label: "Blades", mod: 4, keyed_stat: "might", group: "",
        bracket: "apprentice", mastery: 3, cost: "", gated: false, basis_met: false, basis_name: "" },
      { id: "hexing", label: "Hexing", mod: 1, keyed_stat: "will", group: "Spells",
        bracket: "", mastery: 0, cost: "mana 2", gated: true, basis_met: false, basis_name: "Witch-Sight" }],
    abilities: [
      { id: "surge", name: "Adrenal Surge", active: true, applies_id: "blades",
        applies_to: "Blades", cost: "stamina 2", cooldown: 3, on_cd: 0, group: "technique",
        mechanic_label: "advantage on the roll", desc: "Burn stamina to push a strike." },
      { id: "tough", name: "Wasteland-Tough", active: false, applies_to: "all checks",
        group: "talent", mechanic_label: "", desc: "" },
      { id: "optic", name: "Optic Zoom", active: false, applies_to: "perception",
        group: "Cyber-Ware", mechanic_label: "", desc: "Telescopic eye implant." }],
    effects: [{ key: "bleeding", name: "Bleeding", valence: "negative", kind_label: "Condition",
                glyph: "🩸", note: "took a knife", remaining: 2, stacks: 1, mods: "-1 might" }],
    gear_slots: [
      { slot: "mainhand", label: "Main hand", kind: "weapon",
        item: { iid: "i1", name: "Rusty Machete", mods: "+1 blades" } },
      { slot: "offhand", label: "Off hand", kind: "weapon", item: null },
      { slot: "body", label: "Body", kind: "armor",
        item: { iid: "i5", name: "Patched Jacket", mods: "" } },
      { slot: "head", label: "Head", kind: "armor", item: null },
      { slot: "neck", label: "Neck", kind: "trinket", item: null },
      { slot: "waist", label: "Waist", kind: "armor", item: null }],
    stowed_gear: [{ container: "Backpack",
      items: [{ iid: "i2", name: "Patch Kit", qty: 1, type: "tool", slot: "" }] }],
    inventory: [{ container: "Backpack", items: [
      { iid: "i3", name: "Ration", qty: 2, type: "consumable", consumable: true, slot: "" },
      { iid: "i4", name: "Iron Key", qty: 1, type: "key" }] }],
    gear: [],
    drives: { obsessions: [{ target: "the Spire", target_kind: "location", intensity: 40 }],
              cravings: [{ substance: "synthale", level: 2, withdrawal: false }],
              goals: ["Reach the coast"] },
  }],
  cast: [{ eid: "mira", name: "Mira", present: true, rel_tier: "Friend", mood: "warm · lively",
           arousal: 0, location: "",
           effects: [{ key: "winded", name: "Winded", valence: "negative", kind_label: "Status",
                       glyph: "💨", note: "", remaining: 1, stacks: 1 }],
           drives: { goals: ["Guard Testa"], obsessions: [], cravings: [] },
           rel_dims: [{ dim: "trust", val: 35 }], worn: ["Leather cloak"], exposed: [] }],
  quests: [
    { name: "Cross the Amber Road", status: "active", stakes: "the caravan's survival",
      note: "reach the mill by dusk" },
    { name: "Find water", status: "done", stakes: "", note: "" }],
  rolls: [{ skill: "blades", spec: "", result: 11, mod: 4, tier: "success",
            tier_label: "Success", note: "clean hit" }],
  relations: [{ name: "Mira", tier: "Friend" }],
  factions: [{ name: "Roadwrights", tier: "Ally", circumstances: "debt=paid" }],
  relationships: [{ a: "Testa", b: "Mira", dims: [{ dim: "trust", val: 35 }] }],
  world_flags: { amber_road_open: true },
  memories: [{ turn: 11, text: "Raiders struck the caravan at dusk." }],
  consent: [{ pair: "Testa ↔ Mira", category: "romance", level: "yes", cap: null }],
  rules: { dice: "2d6", keep: 2,
    thresholds: [
      { range: "10+", tier: "Success", desc: "you do it" },
      { range: "7–9", tier: "Partial", desc: "success at a cost" },
      { range: "≤6", tier: "Failure", desc: "it goes wrong" }],
    crits: "a natural 12 crits", check_syntax: "((aether.check <skill>))", note: "",
    mechanics: [{ mechanic: "advantage", label: "roll an extra die, keep the best" }] },
  war_room: { active: true, round: 2, last: null, clashes: [], combatants: [
    { cid: "c1", name: "Raider", side: "enemy", kind: "extra", tier: "standard",
      hp: { cur: 9, max: 14 }, armament: "pipe club", defeated: false, dropped: [],
      die: { total: 11, tier: "HITS", dmg: 3 } },
    { cid: "c2", name: "Mira", side: "ally", kind: "tracked", tier: "elite",
      hp: { cur: 20, max: 26 }, armament: "knife", defeated: false, dropped: [],
      die: { total: 6, tier: "GRAZES", dmg: 1 } },
    { cid: "c3", name: "Grub", side: "enemy", kind: "extra", tier: "minion",
      hp: { cur: 0, max: 6 }, armament: "", defeated: true, dropped: ["3x Coin"] },
    { cid: "c4", name: "Silent One", side: "enemy", kind: "extra", tier: "standard",
      hp: { cur: 5, max: 14 }, armament: "", defeated: false, dropped: [] }] },
};

// ------------------------------ stub DOM / ST / network ------------------------------
const registry = new Map();
function makeEl(id) {
  const cls = new Set();
  const el = {
    _id: id || "", _html: "", style: {}, dataset: {}, value: "", textContent: "",
    title: "", children: [],
    classList: {
      add: (...c) => c.forEach((x) => cls.add(x)),
      remove: (...c) => c.forEach((x) => cls.delete(x)),
      toggle: (c) => { if (cls.has(c)) { cls.delete(c); return false; } cls.add(c); return true; },
      contains: (c) => cls.has(c),
    },
    appendChild(c) { el.children.push(c); return c; },
    addEventListener() {}, dispatchEvent() {}, focus() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 300, height: 200 }; },
    setAttribute() {}, removeAttribute() {},
  };
  Object.defineProperty(el, "id", {
    get() { return el._id; }, set(v) { el._id = v; registry.set(v, el); } });
  Object.defineProperty(el, "className", {
    get() { return [...cls].join(" "); },
    set(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => cls.add(c)); } });
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; }, set(v) { el._html = String(v); } });
  if (id) registry.set(id, el);
  return el;
}
const GUARD_NULL = new Set(["aes_hud_launch", "aetherstate_panel"]);
const documentStub = {
  readyState: "complete", activeElement: null, body: makeEl(""),
  addEventListener() {},
  createElement: () => makeEl(""),
  getElementById(id) {
    if (registry.has(id)) return registry.get(id);
    if (GUARD_NULL.has(id)) return null;
    return makeEl(id);
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
};
async function fetchStub(url) {
  const j = String(url).includes("/hud") ? payload
    : String(url).includes("/aether/status")
      ? { version: "smoke", mode: "relay", extraction: { mode: "off" } } : {};
  return { ok: true, json: async () => j };
}
// PARTIAL saved hud on purpose: only open+compact — the per-key default merge must fill the
// rest (tab, theme, hideTags…). A wholesale-replace regression makes settings.hud.tab
// undefined and this harness fails loudly.
const ctx = {
  extensionSettings: { aetherstate: { enabled: true, hud: { open: true, compact: true } } },
  saveSettingsDebounced() {}, chatMetadata: {}, saveMetadataDebounced() {},
  eventSource: { on() {} }, event_types: {}, characters: [], characterId: 0,
  substituteParams: () => "Player",
};
const sandbox = {
  console, document: documentStub, fetch: fetchStub,
  SillyTavern: { getContext: () => ctx },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  AbortController, Event: class {}, encodeURIComponent, decodeURIComponent,
  addEventListener() {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// ------------------------------ checks ------------------------------
let failures = 0;
const fail = (msg) => { failures += 1; console.error("FAIL: " + msg); };
const ok = (msg) => console.log(" ok : " + msg);
const expect = (cond, msg) => (cond ? ok(msg) : fail(msg));
const mustContain = (html, needles, where) => {
  for (const n of [].concat(needles)) {
    if (!html.includes(n)) fail(`${where}: missing ${JSON.stringify(n)}`);
    else ok(`${where}: has ${JSON.stringify(n)}`);
  }
};
const tick = () => new Promise((r) => setImmediate(r));
function done() {
  console.log(failures ? `\n${failures} FAILURE(S)` : "\nALL HUD RENDER CHECKS PASSED");
  process.exit(failures ? 1 : 0);
}

try {
  vm.runInContext(SRC, sandbox, { filename: "st-extension/index.js" });
} catch (e) {
  fail("index.js failed to LOAD (truncated or syntax-broken?): " + (e && e.stack || e));
  done();
}
await tick(); await tick(); await tick(); await tick();

const body = registry.get("aes_hud_body");
const hud = registry.get("aes_hud");
if (!body || !hud) { fail("HUD was never built (aes_hud/aes_hud_body missing)"); done(); }

// 1) boot MINIMIZED — the strip must label itself and carry vitals (the 2026-07-09 bug class)
const strip = body._html;
mustContain(strip, ["HP 14/20", "STAMINA 8/12", "MANA 5/10"], "compact strip vitals");
mustContain(strip, ["aes-expand", "expand"], "compact strip self-label (minimized must SAY so)");
expect(typeof sandbox.aetherHudExpand === "function", "window.aetherHudExpand exists");
expect(registry.get("aes_hud_min")._html !== undefined, "min button present");

// 2) expand — content must CHANGE and become the full tabbed sheet (default tab from merge)
sandbox.aetherHudExpand();
await tick(); await tick(); await tick();
expect(body._html !== strip, "expand actually re-rendered (stale-body guard)");
mustContain(body._html, ["aes-tabs"], "full sheet tab bar");
expect(settings_tab_defined(), "per-key hud merge filled missing keys (tab defined)");
function settings_tab_defined() {
  return typeof ctx.extensionSettings.aetherstate.hud.tab === "string";
}
// war-room lane rides above the tabs while combat is active
mustContain(body._html, ["WAR ROOM", "Raider", "9/14", "tracked", "down", "Silent One"],
            "war-room lane");

// 3) every tab renders its markers
const TAB_MARKERS = {
  char: ["Attributes", "might", "obsession", "Reach the coast"],
  skills: ["Blades", "Witch-Sight", "Recent checks", "Success"],
  abilities: ["Adrenal Surge", "Wasteland-Tough", "Cyber-Ware", "advantage"],
  rolls: ["aes-rollbtn", "Custom roll", "Adrenal Surge"],
  gear: ["Rusty Machete", "Patched Jacket", "Patch Kit", "open:"],
  inventory: ["Ration", "Iron Key"],
  status: ["Bleeding", "Condition"],
  world: ["Mira", "Cross the Amber Road", "amber_road_open", "Raiders struck", "Roadwrights"],
};
for (const [tab, markers] of Object.entries(TAB_MARKERS)) {
  try {
    sandbox.aetherHudTab(tab);
    if (!body._html || body._html.length < 80) fail(`tab ${tab}: rendered ${body._html.length} chars`);
    mustContain(body._html, markers, "tab " + tab);
  } catch (e) { fail(`tab ${tab} THREW: ` + (e && e.stack || e)); }
}

// 4) a renderer throw must be VISIBLE, never stale content (poison then render)
payload.rules = { dice: "2d6", keep: 2, thresholds: "boom" };   // .map on a string -> throw
sandbox.aetherHudTab("skills");
mustContain(body._html, ["HUD render error"], "visible render-error fallback");

// 5) compact war-room strip (minimized mid-fight still shows the foes)
payload.rules = { dice: "2d6", keep: 2, thresholds: [] };
registry.get("aes_hud_min").onclick();       // back to compact
await tick(); await tick(); await tick();
mustContain(body._html, ["Raider 9/14", "aes-expand"], "compact war-room strip");

done();
