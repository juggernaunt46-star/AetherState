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
  transport_error: { status: 400, turn: 12,
    message: "unsupported request field: include_reasoning" },
  scene: { location: "old_mill", phase: "combat", mode: "", time_of_day: "dusk", day: 3,
           calendar_note: "", present: ["Mira", "Raider"],
           world_circumstance: "Ashfall has closed the eastern road.",
           location_circumstance: "The old mill floor is flooded." },
  players: [{
    eid: "player_testa", name: "Testa Vector", level: 3, xp: 140, stat_points: 1,
    mood: "level · steady", appearance: "A scarred courier with road-bleached hair.",
    concept: "Wasteland courier", species: "human", pronouns: "she/her", sex: "female",
    hp: { cur: 14, max: 20 },
    resources: {
      stamina: { name: "Stamina", cur: 8, max: 12 },
      mana: { name: "Mana", cur: 5, max: 10 },
      ash_focus: { name: "Ash Focus", cur: 4, max: 8, color: "#B56CFF" },
      unsafe_pool: { name: "Unsafe Pool", cur: 1, max: 2, color: "red;display:none" },
    },
    stats: [
      { key: "might", val: 12, mod: 1 }, { key: "agility", val: 14, mod: 2 },
      { key: "wits", val: 11, mod: 0 }, { key: "will", val: 10, mod: 0 },
      { key: "presence", val: 9, mod: -1 }, { key: "vitality", val: 13, mod: 1 }],
    skills: [
      { id: "blades", label: "Blades", mod: 4, keyed_stat: "might", group: "",
        bracket: "apprentice", mastery: 3, cost: "Ash Focus 3", gated: false,
        basis_met: false, basis_name: "", desc: "Measured cuts and defensive parries.",
        governs: ["slash", "parry"] },
      { id: "hexing", label: "Hexing", mod: 1, keyed_stat: "will", group: "Spells",
        bracket: "", mastery: 0, cost: "Ash Focus 2", gated: true, basis_met: false,
        basis_name: "Witch-Sight", desc: "Reads <sealed> patterns without proving them true.",
        governs: ["read signs"] },
      { id: "sealed_path", label: "Sealed Path", mod: 2, keyed_stat: "wits", group: "",
        bracket: "trained", mastery: 1, cost: "", gated: false, basis_met: true,
        basis_name: "", eligible: false, desc: "Finds a lawful path through a closed road.",
        governs: ["trace route"] }],
    abilities: [
      { id: "surge", name: "Adrenal Surge", active: true, applies_id: "blades",
        applies_to: "Blades", cost: "Ash Focus 2", cooldown: 3, on_cd: 0, group: "technique",
        mechanic_label: "advantage on the roll", effect: "Burn stamina to push a strike.",
        desc: "  burn   stamina to push a strike.  " },
      { id: "cooldown_dash", name: "Cooldown Dash", active: true, applies_id: "blades",
        applies_to: "Blades", cost: "Stamina 1", cooldown: 3, on_cd: 2, group: "technique",
        mechanic_label: "extra movement", desc: "Still recharging." },
      { id: "world_locked", name: "World-Locked Technique", active: true, applies_id: "blades",
        applies_to: "Blades", cost: "", cooldown: 0, on_cd: 0, group: "technique",
        mechanic_label: "cross the sealed road", desc: "Blocked by the active world state.",
        eligible: false },
      { id: "tough", name: "Wasteland-Tough", active: false, applies_to: "all checks",
        group: "talent", mechanic_label: "", desc: "" },
      { id: "optic", name: "Optic Zoom", active: false, applies_to: "perception",
        group: "Cyber-Ware", mechanic_label: "", effect: "Magnifies distant details.",
        desc: "Telescopic eye implant." }],
    effects: [{ key: "bleeding", name: "Bleeding", valence: "negative", kind_label: "Condition",
                glyph: "🩸", note: "took a knife", remaining: 2, stacks: 1, mods: "-1 might" }],
    gear_slots: [
      { slot: "mainhand", label: "Main hand", kind: "weapon",
        item: { iid: "i1", name: "Rusty Machete", mods: "+1 blades",
          aura: "Its balance steadies defensive cuts." } },
      { slot: "offhand", label: "Off hand", kind: "weapon", item: null },
      { slot: "body", label: "Body", kind: "armor",
        item: { iid: "i5", name: "Patched Jacket", mods: "" } },
      { slot: "head", label: "Head", kind: "armor", item: null },
      { slot: "neck", label: "Neck", kind: "trinket", item: null },
      { slot: "waist", label: "Waist", kind: "armor", item: null }],
    stowed_gear: [{ container: "Backpack",
      items: [{ iid: "i2", name: "Patch Kit", qty: 1, type: "tool", slot: "",
        aura: "Repairs one torn field layer." }] }],
    inventory: [{ container: "Backpack", items: [
      { iid: "i3", name: "Ration", qty: 2, type: "consumable", consumable: true, slot: "" },
      { iid: "i4", name: "Iron Key", qty: 1, type: "key" }] }],
    gear: [],
    drives: { obsessions: [{ target: "the Spire", target_kind: "location", intensity: 40 }],
              cravings: [{ substance: "synthale", level: 2, withdrawal: false }],
              goals: ["Reach the coast"] },
  }],
  cast: [{ eid: "mira", name: "Mira", present: true, rel_tier: "Friend", mood: "warm · lively",
           arousal: 0, location: "", world_condition: "Marked by the ash storm.",
           effects: [{ key: "winded", name: "Winded", valence: "negative", kind_label: "Status",
                       glyph: "💨", note: "", remaining: 1, stacks: 1 }],
           drives: { goals: ["Guard Testa"], obsessions: [], cravings: [] },
           rel_dims: [{ dim: "trust", val: 35 }], worn: ["Leather cloak"], exposed: [] }],
  quests: [
    { name: "Cross the Amber Road", status: "active", stakes: "the caravan's survival",
      note: "reach the mill by dusk", available: false },
    { name: "Find water", status: "done", stakes: "", note: "", available: true }],
  rolls: [
    { turn: 10, kind: "check", label: "Older Roll Sentinel", skill: "older_roll",
      spec: "", result: 5, mod: 1, tier: "fail", tier_label: "Failure",
      note: "older check", impact: { kind: "damage", target_id: "raider",
        target_label: "Raider", damage: 2, text: "2 damage to Raider" } },
    { turn: 12, kind: "check", label: "Newest Roll Sentinel", skill: "newest_roll",
      spec: "", result: 11, mod: 4, tier: "success", tier_label: "Success",
      note: "clean check", impact: { kind: "none", target_id: null,
        target_label: null, damage: null, text: "No target impact" } },
  ],
  relations: [{ name: "Mira", tier: "Friend", reputation_modifier: 7 }],
  factions: [{ name: "Roadwrights", tier: "Ally", circumstances: "debt=paid",
    reputation_modifier: -5, world_circumstance: "Their river toll is suspended." }],
  relationships: [{ a: "Testa", b: "Mira", dims: [{ dim: "trust", val: 35 }],
    world_modifier: -12 }],
  world_flags: { amber_road_open: true },
  memories: [{ turn: 11, text: "Raiders struck the caravan at dusk." }],
  knowledge: {
    raw_prose: "RAW_CHAT_PROSE_SENTINEL",
    claims: [{ speaker: "Mara", addressee: "Testa", class: "report",
      proposition: "The eastern gate is open.", polarity: "positive", modality: "asserted",
      raw_prose: "RAW_CLAIM_PROSE_SENTINEL" }],
    epistemics: [
      { holder: "Testa", stance: "knows", statement: "The bell rang.", source: "fact:bell" },
      { holder: "Mira", stance: "believes", statement: "The eastern gate is open.", source: "claim:mara" },
      { holder: "Vosk", stance: "doubts", statement: "The eastern gate is open.", source: "claim:mara" },
      { holder: "Roadwrights", stance: "rumor", statement: "A second caravan is coming.", source: "claim:docks" },
    ],
    facts: [{ status: "accepted", statement: "The bell rang.", authority: "rule",
      cause: "settlement:bell" }],
    events: [
      { id: "event.gate", status: "active", what_happened: "The eastern gate opened.",
        cause_visible: true, cause: "front:roadwrights", affected_domains: ["world", "location"] },
      { id: "event.veil", status: "expired_by_duration", what_happened: "The storm veil faded.",
        cause_visible: false, cause: "HIDDEN_CAUSE_SENTINEL", affected_domains: ["world"] },
      { id: "event.curfew", status: "reversal", what_happened: "The curfew was lifted.",
        cause_visible: false, cause: "HIDDEN_REVERSAL_CAUSE_SENTINEL", affected_domains: ["faction"] },
      { id: "event.treaty", status: "supersession", what_happened: "The old treaty no longer governs.",
        cause_visible: false, cause: "HIDDEN_SUPERSESSION_CAUSE_SENTINEL",
        affected_domains: ["faction", "relationship"], relation_target: "Roadwrights" },
    ],
  },
  consent: [{ pair: "Testa ↔ Mira", category: "romance", level: "yes", cap: null }],
  rules: { dice: "2d6", keep: 2,
    thresholds: [
      { range: "10+", tier: "Success", desc: "you do it" },
      { range: "7–9", tier: "Partial", desc: "success at a cost" },
      { range: "≤6", tier: "Failure", desc: "it goes wrong" }],
    crits: "a natural 12 crits", check_syntax: "((aether.check <skill>))", note: "",
    mechanics: [{ mechanic: "advantage", label: "roll an extra die, keep the best" }] },
  war_room: { active: true, round: 2, last: null, clashes: [],
    order: [{ name: "Testa Vector", side: "player" }, { name: "Raider", side: "enemy" }],
    battle: { name: "Caravan Ambush", tide: "losing", waves: 1,
      cohort: { name: "Baser Hollow", total: 6, active: 3, defeated: 0, queued: 3 } },
    player_impacts: [
      { turn: 12, kind: "check", label: "PLAYER_NO_IMPACT_SENTINEL", result: 11,
        mod: 4, tier: "success", tier_label: "Success", note: "settled without a target",
        impact: { kind: "none", target_id: null, target_label: null, damage: null,
          text: "PLAYER_NO_TARGET_IMPACT_TEXT" } },
      { turn: 12, kind: "check", label: "PLAYER_DAMAGE_SENTINEL", result: 5,
        mod: 1, tier: "fail", tier_label: "Failure", note: "exact target impact",
        impact: { kind: "damage", target_id: "c1", target_label: "Raider", damage: 2,
          text: "PLAYER_EXACT_DAMAGE_TEXT" } },
    ],
    opposition: { schema: "enemy-action/1", intent_id: "intent_resolved_smoke",
      actor: "c1", actor_name: "RESOLVED_ACTOR_SENTINEL", target: "testa",
      target_name: "RESOLVED_TARGET_SENTINEL", move_id: "martial_driving_advance",
      move_name: "Driving Advance", delivery: "RESOLVED_DELIVERY_SENTINEL",
      tier: "MISSES", total: 5, damage: 0, hp_cur: 20, hp_max: 20,
      current_hp_cur: 20, current_hp_max: 20 },
    intent: { schema: "enemy-intent/1", id: "intent_smoke", actor: "c1",
      actor_name: "INTENT_ACTOR_SENTINEL", target: "testa", target_name: "INTENT_TARGET_SENTINEL",
      move_id: "martial_hooking_sweep", move_name: "Hooking Sweep", danger: "high",
      delivery: "DELIVERY_PIPE_SENTINEL", timing: "TIMING_COMMITTED_SENTINEL",
      cadence: "CADENCE_SETUP_SENTINEL", risk: "RISK_RECOVERY_SENTINEL",
      tell: "the club drops low and the Raider's shoulders turn into the hook",
      counterplay: ["step outside the hook", "jam the club before it gathers speed"],
      reaction: { schema: "enemy-reaction/1", kind: "brace", cost: "whole_action",
        effect: "halve_committed_hp", trigger: "I brace." } },
    combatants: [
    { cid: "c1", name: "Raider", side: "enemy", kind: "extra", tier: "standard",
      hp: { cur: 9, max: 14 }, armament: "pipe club", defeated: false, dropped: [] },
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
    _id: id || "", _html: "", style: { setProperty(k, v) { this[k] = v; } }, dataset: {}, value: "", textContent: "",
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
const temporarilyMissingIds = new Set();
const messageTexts = [];
const sendTextarea = makeEl("send_textarea");
const documentStub = {
  readyState: "complete", activeElement: null, body: makeEl(""),
  addEventListener() {},
  createElement: () => makeEl(""),
  getElementById(id) {
    if (temporarilyMissingIds.has(id)) return null;
    if (registry.has(id)) return registry.get(id);
    if (GUARD_NULL.has(id)) return null;
    return makeEl(id);
  },
  querySelector(selector) { return selector === "#send_textarea" ? sendTextarea : null; },
  querySelectorAll(selector) { return selector === "#chat .mes_text" ? messageTexts : []; },
};
const fetchCalls = [];
async function fetchStub(url, options = {}) {
  fetchCalls.push({ url: String(url), options });
  const j = String(url).includes("/hud") ? payload
    : String(url).includes("/aether/status")
      ? { version: "smoke", mode: "relay", extraction: { mode: "off" } } : {};
  return { ok: true, json: async () => j };
}
// PARTIAL saved hud on purpose: only open+compact — the per-key default merge must fill the
// rest (tab, theme, hideTags…). A wholesale-replace regression makes settings.hud.tab
// undefined and this harness fails loudly.
const eventHandlers = new Map();
const eventRegistrationCounts = new Map();
const activeEventHandlers = new Map();
const intervalCallbacks = [];
const windowHandlers = new Map();
let nextIntervalId = 1;
let fakeNow = 1_000_000;
class FakeDate extends Date {
  static now() { return fakeNow; }
}
const ctx = {
  extensionSettings: { aetherstate: { enabled: true, hud: { open: true, compact: true } } },
  saveSettingsDebounced() {}, chatMetadata: {}, saveMetadataDebounced() {}, chat: [],
  chatId: "source-chat",
  eventSource: {
    on(name, fn) {
      eventHandlers.set(name, fn);
      eventRegistrationCounts.set(name, (eventRegistrationCounts.get(name) || 0) + 1);
      if (!activeEventHandlers.has(name)) activeEventHandlers.set(name, new Set());
      activeEventHandlers.get(name).add(fn);
    },
    off(name, fn) {
      activeEventHandlers.get(name)?.delete(fn);
      if (eventHandlers.get(name) === fn) {
        const remaining = [...(activeEventHandlers.get(name) || [])];
        if (remaining.length) eventHandlers.set(name, remaining.at(-1));
        else eventHandlers.delete(name);
      }
    },
    removeListener(name, fn) {
      activeEventHandlers.get(name)?.delete(fn);
      if (eventHandlers.get(name) === fn) {
        const remaining = [...(activeEventHandlers.get(name) || [])];
        if (remaining.length) eventHandlers.set(name, remaining.at(-1));
        else eventHandlers.delete(name);
      }
    },
  },
  event_types: {
    CHAT_COMPLETION_PROMPT_READY: "chat_completion_prompt_ready",
    CHAT_CHANGED: "chat_changed",
    MESSAGE_SWIPED: "message_swiped",
    GENERATION_STARTED: "generation_started",
    GENERATION_AFTER_COMMANDS: "generation_after_commands",
    GENERATE_AFTER_DATA: "generate_after_data",
    GENERATION_ENDED: "generation_ended",
    GENERATION_STOPPED: "generation_stopped",
    MESSAGE_RECEIVED: "message_received",
    STREAM_TOKEN_RECEIVED: "stream_token_received",
  },
  characters: [], characterId: 0,
  substituteParams: () => "Player",
};
const toastCalls = [];
const sandbox = {
  console, document: documentStub, fetch: fetchStub,
  SillyTavern: { getContext: () => ctx },
  toastr: {
    warning(message, title) { toastCalls.push({ kind: "warning", message, title }); },
    error(message, title) { toastCalls.push({ kind: "error", message, title }); },
  },
  Date: FakeDate,
  setTimeout: () => 0, clearTimeout() {},
  setInterval(fn, delay) {
    const entry = { id: nextIntervalId++, fn, delay, active: true };
    intervalCallbacks.push(entry);
    return entry.id;
  },
  clearInterval(id) {
    const entry = intervalCallbacks.find((candidate) => candidate.id === id);
    if (entry) entry.active = false;
  },
  AbortController, Event: class {}, encodeURIComponent, decodeURIComponent,
  addEventListener(name, fn) { windowHandlers.set(name, fn); },
  removeEventListener(name, fn) {
    if (windowHandlers.get(name) === fn) windowHandlers.delete(name);
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);

// ------------------------------ checks ------------------------------
let failures = 0;
const fail = (msg) => { failures += 1; console.error("FAIL: " + msg); };
const ok = (msg) => console.log(" ok : " + msg);
const expect = (cond, msg) => (cond ? ok(msg) : fail(msg));
const occurrences = (text, needle) => String(text).split(needle).length - 1;
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

// SillyTavern's duplicate character-editor controls can turn V2 scalar fields into
// [realValue, ""]. The guard must repair only AetherState-generated Narrators before prompt
// assembly, including their V1 mirrors, while leaving native arrays and every other card alone.
const generationStarted = eventHandlers.get("generation_started");
expect(typeof generationStarted === "function", "generated Narrator scalar guard registered");
const generatedTags = ["aetherstate", "narrator"];
const generatedGreetings = ["First alternate", "Second alternate"];
const malformedNarrator = {
  name: "Guard Test Narrator",
  description: "description stays scalar",
  personality: ["steady", ""],
  scenario: ["at the gate,", ""],
  first_mes: "opening stays scalar",
  mes_example: ["", ""],
  data: {
    name: "Guard Test Narrator",
    description: "description stays scalar",
    personality: ["steady", ""],
    scenario: ["at the gate,", ""],
    first_mes: "opening stays scalar",
    mes_example: ["", ""],
    system_prompt: ["SYSTEM PROMPT SENTINEL,", ""],
    post_history_instructions: ["", ""],
    creator_notes: ["CREATOR NOTES SENTINEL", ""],
    creator: ["AetherState Creator", ""],
    character_version: ["aether-world-1.2", ""],
    tags: generatedTags,
    alternate_greetings: generatedGreetings,
    extensions: { aetherstate: { generated: true, role: "narrator" } },
  },
};
ctx.characters = [malformedNarrator];
ctx.characterId = 0;
generationStarted("normal", {}, false);
expect(malformedNarrator.data.system_prompt === "SYSTEM PROMPT SENTINEL,"
       && malformedNarrator.data.post_history_instructions === ""
       && malformedNarrator.data.creator_notes === "CREATOR NOTES SENTINEL",
       "generated Narrator V2 scalar arrays repaired before prompt assembly");
expect(malformedNarrator.personality === "steady" && malformedNarrator.scenario === "at the gate,"
       && malformedNarrator.mes_example === "",
       "generated Narrator V1 scalar mirrors repaired");
expect(malformedNarrator.data.tags === generatedTags
       && malformedNarrator.data.alternate_greetings === generatedGreetings,
       "native generated-card arrays remain byte-for-byte owned by SillyTavern");
expect(toastCalls.some((call) => call.kind === "warning" && call.message.includes("Generation can continue")),
       "repaired Narrator receives a visible recovery notice");

const ordinaryCard = {
  personality: ["ordinary", ""],
  data: {
    personality: ["ordinary", ""], system_prompt: ["ordinary prompt", ""],
    tags: ["ordinary"], extensions: {},
  },
};
const ordinarySnapshot = JSON.stringify(ordinaryCard);
ctx.characters = [ordinaryCard];
generationStarted("normal", {}, false);
expect(JSON.stringify(ordinaryCard) === ordinarySnapshot,
       "non-AetherState character cards are never normalized");

const ambiguousNarrator = {
  data: {
    system_prompt: ["first authored value", "second authored value"],
    extensions: { aetherstate: { generated: true, role: "narrator" } },
  },
};
ctx.characters = [ambiguousNarrator];
generationStarted("normal", {}, false);
expect(Array.isArray(ambiguousNarrator.data.system_prompt)
       && ambiguousNarrator.data.system_prompt.length === 2,
       "ambiguous Narrator arrays are warned about, never guessed");
expect(toastCalls.some((call) => call.kind === "error" && call.message.includes("Re-import a fresh card")),
       "ambiguous Narrator receives a clear recovery instruction");
ctx.characters = [];

// The HUD pulse describes only a foreground narrator reply. SillyTavern emits GENERATION_STARTED
// before command handling, connection checks, prompt assembly, and several early-abort paths. It
// emits GENERATE_AFTER_DATA only after those gates and immediately before the real request. A start
// must therefore remain pending until that second event; otherwise an early failure can leave an
// ever-growing timer even though no model request exists. ST provides no universal generation ID,
// so the lifecycle uses the expected message index/chat length to reject stale terminals where the
// public payload can distinguish them.
const generationEnded = eventHandlers.get("generation_ended");
const generationStopped = eventHandlers.get("generation_stopped");
const messageReceived = eventHandlers.get("message_received");
const generationAfterData = eventHandlers.get("generate_after_data");
const messageSwiped = eventHandlers.get("message_swiped");
const chatChangedForPulse = eventHandlers.get("chat_changed");
const pulseInterval = intervalCallbacks.find((entry) => entry.delay === 1000)?.fn;
expect(typeof generationEnded === "function" && typeof generationStopped === "function"
       && typeof messageReceived === "function" && typeof pulseInterval === "function",
       "narrating pulse lifecycle hooks registered");
expect(typeof generationAfterData === "function",
       "narrating pulse activates only at SillyTavern request-data-ready boundary");
expect(typeof messageSwiped === "function" && typeof chatChangedForPulse === "function",
       "swipe and chat replacement lifecycle hooks registered");

const afterData = (dryRun = false) => {
  if (typeof generationAfterData === "function") generationAfterData({}, dryRun);
};
const pulseVisible = () => {
  pulseInterval();
  return pulse.style.display === "" && pulse.textContent.includes("narrating");
};
const pulseHidden = () => {
  pulseInterval();
  return pulse.style.display === "none";
};

generationEnded(0);
generationStopped();
chatChangedForPulse();
const pulse = documentStub.getElementById("aes_pulse");
expect(payload.rolls.length > 0 && pulseHidden(),
       "prior HUD narration and roll history never starts the live narrator pulse");

// A normal start is only a pending proposal. Early command cancellation, connection failure, and
// thrown prompt errors can all occur before GENERATE_AFTER_DATA and must remain visibly idle.
ctx.chat = [{ is_user: true, is_system: false, mes: "pending request" }];
generationStarted("normal", {}, false);
expect(pulseHidden(), "GENERATION_STARTED alone remains pending across early abort/error paths");
afterData(false);
expect(pulseVisible(), "normal non-stream request becomes visible only after request data is ready");
fakeNow += 4_000;
pulseInterval();
expect(pulse.textContent.includes("4s"), "active narrator pulse reports elapsed time from one stable start");
ctx.chat.push({ is_user: false, is_system: false, mes: "committed non-stream reply" });
messageReceived(1, "normal");
expect(pulseHidden(), "normal non-stream MESSAGE_RECEIVED retires the matching pulse before ENDED");
fakeNow += 20_000;
expect(pulseHidden(), "completed reply cannot keep an elapsed timer growing in the HUD");
generationEnded(2);
expect(pulseHidden(), "late non-stream ENDED is idempotent");

// Streaming finishes in the reverse order: ENDED first, then MESSAGE_RECEIVED. Both orders must
// converge on the same idle state without a negative or resurrected timer.
ctx.chat = [{ is_user: true, mes: "stream request" }, { is_user: false, mes: "older reply" }];
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "streaming foreground request shows one live pulse");
// Streaming inserts its assistant row at onStartStreaming, before markUIGenStopped emits ENDED.
ctx.chat.push({ is_user: false, is_system: false, mes: "committed streaming reply" });
generationEnded(3);
expect(pulseHidden(), "streaming ENDED retires before MESSAGE_RECEIVED");
messageReceived(2, "normal");
expect(pulseHidden(), "late streaming MESSAGE_RECEIVED is idempotent");

// STOPPED is SillyTavern's public abort surface and may arrive with or without a preceding ENDED.
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "aborted foreground request starts exactly one pulse");
generationStopped();
expect(pulseHidden(), "STOPPED retires an abort even when ENDED is absent");
generationStopped();
generationEnded(ctx.chat.length);
messageReceived(ctx.chat.length, "normal");
expect(pulseHidden(), "duplicate abort/error terminals remain idempotent");

// SillyTavern has no public generation-error event and does not expose isGenerating() through the
// extension context. A failure after request-data-ready but before any terminal must therefore be
// bounded by a generous orphan watchdog instead of growing forever.
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "post-activation error case begins as real foreground work");
fakeNow += 30 * 60 * 1_000;
expect(pulseHidden(), "orphaned post-activation work is retired within thirty minutes");
generationStopped();

generationStarted("quiet", { quiet_prompt: "background helper" }, false);
afterData(false);
expect(pulseHidden(), "explicit quiet background generation never claims narrator pulse");
generationStarted(undefined, { quiet_prompt: "implicit background helper" }, false);
afterData(false);
expect(pulseHidden(), "missing generation type plus quiet_prompt remains background work");
generationStarted("normal", { quiet_prompt: "recursive background helper", quietToLoud: false }, false);
afterData(false);
expect(pulseHidden(), "tool-recursed normal type retains its quiet background authority");
generationStarted(undefined, { quiet_prompt: "promoted foreground helper", quietToLoud: true }, false);
afterData(false);
expect(pulseVisible(), "missing type plus quiet-to-loud is promoted to a visible normal fallback");
generationStopped();
generationStarted("normal", { quiet_prompt: "foreground system reply", quietToLoud: true }, false);
afterData(false);
expect(pulseVisible(), "quiet-to-loud normal request remains visible foreground work");
generationStopped();
generationStarted("normal", {}, true);
afterData(true);
expect(pulseHidden(), "dry-run prompt assembly never claims narrator pulse");

const enabledToggle = documentStub.getElementById("aes_enabled");
expect(typeof enabledToggle.onchange === "function", "extension enabled toggle is wired");
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "enabled extension may show proven foreground work");
enabledToggle.checked = false;
enabledToggle.onchange({ target: enabledToggle });
expect(pulseHidden(), "disabling AetherState immediately retires active narrator work");
generationStarted("normal", {}, false);
afterData(false);
expect(pulseHidden(), "disabled AetherState cannot start a narrator pulse");
enabledToggle.checked = true;
enabledToggle.onchange({ target: enabledToggle });

// Two foreground requests can reach AFTER_DATA at the same chat/session coordinate before either
// one commits a reply row. ST publishes no request id on MESSAGE_RECEIVED or GENERATION_ENDED, so
// a late terminal from A must not retire B merely because both predicted index 1 / length 2. The
// stale A signals below deliberately arrive while that coordinate is still absent from live chat;
// B's terminal arrives only after its assistant row is committed, matching ST's real save order.
const startSameCoordinateNormalPair = (label) => {
  ctx.chat = [{ is_user: true, is_system: false, mes: `${label} player request` }];
  generationStarted("normal", {}, false);
  afterData(false);
  generationStarted("normal", {}, false);
  afterData(false);
  expect(pulseVisible(), `${label}: superseding B owns the same-coordinate pulse`);
};
const commitSameCoordinateReply = (label) => {
  ctx.chat.push({ is_user: false, is_system: false, mes: `${label} B reply` });
};

startSameCoordinateNormalPair("stale-message-only");
messageReceived(1, "normal");
expect(pulseVisible(), "uncorroborated same-coordinate A MESSAGE cannot retire B");
commitSameCoordinateReply("stale-message-only");
messageReceived(1, "normal");
expect(pulseHidden(), "authoritative B MESSAGE retires immediately after one stale A MESSAGE");
generationEnded(2);
expect(pulseHidden(), "B MESSAGE+ENDED retires promptly after one stale A MESSAGE");

startSameCoordinateNormalPair("stale-ended-only");
generationEnded(2);
expect(pulseVisible(), "uncorroborated same-coordinate A ENDED cannot retire B");
commitSameCoordinateReply("stale-ended-only");
generationEnded(2);
expect(pulseHidden(), "authoritative B ENDED retires immediately after one stale A ENDED");
messageReceived(1, "normal");
expect(pulseHidden(), "B ENDED+MESSAGE retires promptly after one stale A ENDED");

startSameCoordinateNormalPair("stale-pair-message-first");
messageReceived(1, "normal");
generationEnded(2);
expect(pulseVisible(), "uncorroborated A MESSAGE+ENDED pair cannot retire B");
commitSameCoordinateReply("stale-pair-message-first");
messageReceived(1, "normal");
expect(pulseHidden(), "authoritative B MESSAGE retires immediately after the complete stale A pair");
generationEnded(2);
expect(pulseHidden(), "B MESSAGE+ENDED retires after the complete stale A pair");

startSameCoordinateNormalPair("stale-pair-ended-first");
generationEnded(2);
messageReceived(1, "normal");
expect(pulseVisible(), "uncorroborated A ENDED+MESSAGE pair cannot retire B");
commitSameCoordinateReply("stale-pair-ended-first");
generationEnded(2);
expect(pulseHidden(), "authoritative B ENDED retires immediately after the reverse stale A pair");
messageReceived(1, "normal");
expect(pulseHidden(), "B ENDED+MESSAGE retires after the reverse stale A pair");

startSameCoordinateNormalPair("zero-a-message-first");
commitSameCoordinateReply("zero-a-message-first");
messageReceived(1, "normal");
expect(pulseHidden(), "authoritative B MESSAGE retires immediately when A emits no terminal");
generationEnded(2);
expect(pulseHidden(), "B MESSAGE+ENDED retires promptly when superseded A emits no terminal");

startSameCoordinateNormalPair("zero-a-ended-first");
commitSameCoordinateReply("zero-a-ended-first");
generationEnded(2);
expect(pulseHidden(), "authoritative B ENDED retires immediately when A emits no terminal");
messageReceived(1, "normal");
expect(pulseHidden(), "B ENDED+MESSAGE retires promptly when superseded A emits no terminal");

// Information-theoretic residual: ordinary ST buttons serialize foreground generation behind
// is_send_press, but direct/programmatic Generate calls can construct an overlap. Once B has created
// or mutated the same reply slot, the public observation for "late A terminal" is byte-for-byte the
// observation for "B terminal": same chat, index/type or length, and same live row. The extension
// therefore accepts it. These controls keep that limitation explicit instead of overstating the
// chat-mutation predicate as a request identity or final-stream proof.
startSameCoordinateNormalPair("post-mutation-ended-collision");
commitSameCoordinateReply("post-mutation-ended-collision");
generationEnded(2);
expect(pulseHidden(),
       "post-mutation same-slot ENDED is explicitly indistinguishable at ST's public boundary");
startSameCoordinateNormalPair("post-mutation-message-collision");
commitSameCoordinateReply("post-mutation-message-collision");
messageReceived(1, "normal");
expect(pulseHidden(),
       "post-mutation same-slot MESSAGE is explicitly indistinguishable at ST's public boundary");

// A new normal request supersedes the previous request. Its expected reply index/terminal chat
// length fences late completion signals from A so they cannot clear active B.
ctx.chat = [{ is_user: true, mes: "request A" }];
generationStarted("normal", {}, false);
afterData(false); // A expects message index 1 / terminal chat length 2
ctx.chat.push({ is_user: false, mes: "reply A" });
generationStarted("normal", {}, false);
generationStarted("normal", {}, false); // duplicate start must not create another pulse timer
afterData(false); // B expects message index 2 / terminal chat length 3
expect(pulseVisible(), "superseding request B owns one active narrator pulse");
messageReceived(1, "normal");
generationEnded(2);
expect(pulseVisible(), "stale completion from request A cannot clear active request B");
ctx.chat.push({ is_user: false, is_system: false, mes: "reply B" });
messageReceived(2, "normal");
generationEnded(3);
expect(pulseHidden(), "matching request B completion retires exactly once");

// MESSAGE_SWIPED fires whether the user selects an existing swipe or starts an overswipe. It first
// retires old work; a subsequent swipe/regenerate START+AFTER_DATA may then claim a fresh pulse.
generationStarted("normal", {}, false);
afterData(false);
messageReceived(5, "extension");
expect(pulseVisible(), "unrelated extension message does not clear active narrator pulse");
messageSwiped(1);
expect(pulseHidden(), "selecting or beginning a swipe retires the prior pulse");
generationStarted("swipe", {}, false);
afterData(false);
expect(pulseVisible(), "overswipe generation starts a fresh pulse after request data is ready");
const swipeReplyIndex = ctx.chat.length - 1;
ctx.chat[swipeReplyIndex].mes = "committed swipe replacement";
ctx.chat[swipeReplyIndex].swipe_id = 1;
messageReceived(swipeReplyIndex, "swipe");
expect(pulseHidden(), "swipe completion retires its fresh pulse");
generationStarted("swipe", {}, false);
afterData(false);
expect(pulseVisible(), "stream swipe starts one fresh pulse against the retained assistant row");
ctx.chat[swipeReplyIndex].mes = "committed streaming swipe replacement";
ctx.chat[swipeReplyIndex].swipe_id = 2;
generationEnded(ctx.chat.length);
expect(pulseHidden(), "stream swipe ENDED retires only after its retained row changes");
messageReceived(swipeReplyIndex, "swipe");
expect(pulseHidden(), "late stream swipe MESSAGE remains idempotent");

// Regenerate deletes the current assistant before AFTER_DATA, so its replacement is a normal append
// coordinate even though MESSAGE_RECEIVED may report either regenerate or normal in ST internals.
ctx.chat.pop();
generationStarted("regenerate", {}, false);
afterData(false);
expect(pulseVisible(), "regeneration supersedes prior lifecycle with one fresh pulse");
// ST removes the old assistant before GENERATE_AFTER_DATA, then writes the regenerated reply at
// the current chat length. Regenerate therefore has normal append indices here, unlike swipe and
// continue, which retain and replace/append the existing assistant row.
const regenerateReplyIndex = ctx.chat.length;
ctx.chat.push({ is_user: false, is_system: false, mes: "committed regenerate reply" });
messageReceived(regenerateReplyIndex, "regenerate");
expect(pulseHidden(), "non-stream regeneration completion matches its post-deletion reply index");
generationEnded(regenerateReplyIndex + 1);
expect(pulseHidden(), "late non-stream regeneration ENDED remains idempotent");
ctx.chat.pop();
generationStarted("regenerate", {}, false);
afterData(false);
expect(pulseVisible(), "stream regeneration starts one fresh pulse");
const streamRegenerateReplyIndex = ctx.chat.length;
ctx.chat.push({ is_user: false, is_system: false, mes: "committed streaming regenerate reply" });
generationEnded(ctx.chat.length);
expect(pulseHidden(), "stream regeneration ENDED matches its post-deletion terminal length");
messageReceived(streamRegenerateReplyIndex, "regenerate");
expect(pulseHidden(), "late stream regeneration MESSAGE_RECEIVED remains idempotent");

// Continue retains the final assistant and mutates it before either public completion order.
const continueReplyIndex = ctx.chat.length - 1;
generationStarted("continue", {}, false);
afterData(false);
expect(pulseVisible(), "stream continue starts against the retained assistant row");
ctx.chat[continueReplyIndex].mes += " + streamed continuation";
generationEnded(ctx.chat.length);
expect(pulseHidden(), "stream continue ENDED retires after the retained row changes");
messageReceived(continueReplyIndex, "continue");
expect(pulseHidden(), "late stream continue MESSAGE remains idempotent");
generationStarted("continue", {}, false);
afterData(false);
expect(pulseVisible(), "non-stream continue starts one fresh pulse");
ctx.chat[continueReplyIndex].mes += " + non-stream continuation";
messageReceived(continueReplyIndex, "appendFinal");
expect(pulseVisible(), "appendFinal MESSAGE is non-authoritative until ST emits its completion window");
generationEnded(ctx.chat.length);
expect(pulseHidden(), "non-stream continue remains bounded by committed ENDED");

generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "pre-chat-change generation is visible");
chatChangedForPulse();
generationEnded(ctx.chat.length);
messageReceived(ctx.chat.length, "normal");
expect(pulseHidden(), "chat change retires active and pending work despite late old-chat terminals");

// Repeated starts/finishes across turns and chats must reuse the single installed interval and one
// listener per ST event. Teardown must retire the pulse and stop that interval deterministically.
expect(intervalCallbacks.filter((entry) => entry.delay === 1000).length === 1,
       "narrating lifecycle owns exactly one one-second interval");
for (const eventName of ["generation_started", "generate_after_data", "generation_ended",
  "generation_stopped", "message_received", "message_swiped", "chat_changed"]) {
  expect((eventRegistrationCounts.get(eventName) || 0) === 1,
         `${eventName} listener is registered exactly once`);
}
// The OpenAI prompt builder can fail closed inside SillyTavern after the exact Player bubble has
// already been saved to ctx.chat. AetherState must recover that owned text on normal sends and
// retry generations, while never synthesizing it for continue/impersonate/quiet or duplicating a
// current user message that survived.
const promptReady = eventHandlers.get("chat_completion_prompt_ready");
expect(typeof promptReady === "function", "final-prompt recovery hook registered");
ctx.chat = [{ is_user: true, is_system: false, mes: "CURRENT PLAYER SENTINEL" }];

const missingCurrent = { chat: [], dryRun: false };
promptReady(missingCurrent);
expect(missingCurrent.chat.filter((m) => m.role === "user").length === 1,
       "missing current Player message recovered exactly once");
expect(missingCurrent.chat.at(-1).content === "CURRENT PLAYER SENTINEL",
       "recovered Player message remains the answer target");

const staleOnly = { chat: [{ role: "user", content: "STALE PLAYER MESSAGE" }], dryRun: false };
promptReady(staleOnly);
expect(staleOnly.chat.filter((m) => m.role === "user").length === 2
       && staleOnly.chat.at(-1).content === "CURRENT PLAYER SENTINEL",
       "stale user history does not masquerade as the current Player message");

const alreadyCurrent = {
  chat: [{ role: "user", content: [{ type: "text", text: "Player: CURRENT PLAYER SENTINEL" }] }],
  dryRun: false,
};
promptReady(alreadyCurrent);
expect(alreadyCurrent.chat.filter((m) => m.role === "user").length === 1,
       "current multimodal Player message is not duplicated");

await sandbox.aetherstateInterceptor([], 0, null, "swipe");
const swipePrompt = { chat: [], dryRun: false };
promptReady(swipePrompt);
promptReady(swipePrompt);
expect(swipePrompt.chat.filter((m) => m.role === "user").length === 1
       && swipePrompt.chat.filter((m) => m.role === "user")[0].content === "CURRENT PLAYER SENTINEL",
       "swipe recovers the exact saved Player answer target once");

await sandbox.aetherstateInterceptor([], 0, null, "regenerate");
const regeneratePrompt = { chat: [{ role: "user", content: "STALE PLAYER MESSAGE" }], dryRun: false };
promptReady(regeneratePrompt);
promptReady(regeneratePrompt);
expect(regeneratePrompt.chat.filter((m) => m.role === "user").length === 2
       && regeneratePrompt.chat.at(-1).content === "CURRENT PLAYER SENTINEL",
       "regenerate replaces stale answer targeting with the exact saved Player message once");

for (const excludedType of ["continue", "impersonate", "quiet"]) {
  await sandbox.aetherstateInterceptor([], 0, null, excludedType);
  const excludedPrompt = { chat: [], dryRun: false };
  promptReady(excludedPrompt);
  expect(excludedPrompt.chat.every((m) => m.role !== "user"),
         `${excludedType} never synthesizes a Player action`);
}

await sandbox.aetherstateInterceptor([], 0, null, "swipe");
const dryPrompt = { chat: [], dryRun: true };
promptReady(dryPrompt);
expect(dryPrompt.chat.length === 0, "dry-run retry prompt remains untouched");
await sandbox.aetherstateInterceptor([], 0, null, "normal");

// SillyTavern creates a branch by copying the whole parent metadata object and adding main_chat.
// The extension must rotate that copied SID exactly once, preserve explicit lineage at the exact
// branch snapshot length, remain stable on reopen, and repeat correctly for a nested branch. A
// branch inherits its parent's ledger, so it must never automatically seed or genesis a blank one.
const chatChanged = eventHandlers.get("chat_changed");
expect(typeof chatChanged === "function", "chat identity hook registered");
const lineageCard = {
  name: "Lineage Narrator", description: "A deterministic branch test narrator.",
  personality: "steady", scenario: "at the fork", first_mes: "Opening.",
  data: { extensions: { aetherstate: {
    role: "narrator", seed: { world: { name: "Lineage World" } },
  } } },
};
ctx.characters = [lineageCard];
ctx.characterId = 0;
ctx.chatId = "source-chat";
ctx.chatMetadata = {};
ctx.chat = [
  { is_user: false, mes: "Opening." },
  { is_user: true, mes: "T1" },
  { is_user: false, mes: "A1" },
  { is_user: true, mes: "T2" },
  { is_user: false, mes: "A2" },
];
fetchCalls.length = 0;
chatChanged();
await tick(); await tick(); await tick(); await tick();
const sourceSid = ctx.chatMetadata.aetherstate_sid;
expect(/^st-[a-z0-9]+$/.test(sourceSid), "source chat receives one AetherState session ID");
expect(ctx.chatMetadata.aetherstate_chat_id === "source-chat",
       "source session identity is bound to SillyTavern chatId");
expect(!("aetherstate_parent_sid" in ctx.chatMetadata)
       && !("aetherstate_fork_pos" in ctx.chatMetadata),
       "source chat carries no branch lineage");

const sourceMetadata = { ...ctx.chatMetadata };
ctx.chatId = "branch-chat";
ctx.chatMetadata = { ...sourceMetadata, main_chat: "source-chat" };
fetchCalls.length = 0;
chatChanged();
await tick(); await tick(); await tick(); await tick();
const childSid = ctx.chatMetadata.aetherstate_sid;
expect(childSid !== sourceSid && /^st-[a-z0-9]+$/.test(childSid),
       "copied branch rotates to a distinct child session");
expect(ctx.chatMetadata.aetherstate_parent_sid === sourceSid
       && ctx.chatMetadata.aetherstate_fork_pos === 5,
       "child records exact parent session and five-message fork snapshot");
expect(ctx.chatMetadata.aetherstate_chat_id === "branch-chat",
       "child session is bound to the branch chatId");
expect(fetchCalls.every((call) => !/\/seed(?:\?|$)|\/genesis(?:\?|$)/.test(call.url)),
       "derived branch performs no automatic seed or genesis");

const branchPrompt = { chat: [], dryRun: false };
promptReady(branchPrompt);
const branchSentinel = branchPrompt.chat.find((message) => message.role === "system")?.content || "";
expect(branchSentinel.includes(`session=${childSid};parent=${sourceSid};fork=5;`),
       "branch sentinel carries explicit parent and fork lineage");

const savedChildMetadata = { ...ctx.chatMetadata };
fetchCalls.length = 0;
ctx.chatMetadata = { ...savedChildMetadata };
chatChanged();
await tick(); await tick(); await tick();
expect(ctx.chatMetadata.aetherstate_sid === childSid,
       "reopening the same branch keeps its child session stable");
expect(fetchCalls.every((call) => !/\/seed(?:\?|$)|\/genesis(?:\?|$)/.test(call.url)),
       "reopened branch still performs no automatic seed or genesis");

ctx.chatId = "nested-branch-chat";
ctx.chat = [...ctx.chat, { is_user: true, mes: "T3" }, { is_user: false, mes: "A3" }];
ctx.chatMetadata = { ...savedChildMetadata, main_chat: "branch-chat" };
fetchCalls.length = 0;
chatChanged();
await tick(); await tick(); await tick();
const nestedSid = ctx.chatMetadata.aetherstate_sid;
expect(nestedSid !== childSid && nestedSid !== sourceSid,
       "nested branch rotates to a third session");
expect(ctx.chatMetadata.aetherstate_parent_sid === childSid
       && ctx.chatMetadata.aetherstate_fork_pos === 7
       && ctx.chatMetadata.aetherstate_chat_id === "nested-branch-chat",
       "nested branch points to its immediate parent and exact seven-message snapshot");
expect(fetchCalls.every((call) => !/\/seed(?:\?|$)|\/genesis(?:\?|$)/.test(call.url)),
       "nested branch performs no automatic seed or genesis");

const body = registry.get("aes_hud_body");
const hud = registry.get("aes_hud");
if (!body || !hud) { fail("HUD was never built (aes_hud/aes_hud_body missing)"); done(); }

// 1) boot MINIMIZED — the strip must label itself and carry vitals (the 2026-07-09 bug class)
const strip = body._html;
mustContain(strip, ["HP 14/20", "Stamina 8/12", "Mana 5/10", "Ash Focus 4/8",
  "Unsafe Pool 1/2", "#b56cff", "No target impact"],
  "compact strip vitals, custom resources, and latest impact truth");
expect(!strip.includes('class="aes-bar ash_focus"') && !strip.includes('class="aes-bar unsafe_pool"'),
  "resource IDs never become CSS classes");
expect(!strip.includes("red;display:none"), "unsafe resource color is rejected");
mustContain(strip, ["HTTP 400", "unsupported request field"], "compact upstream error");
mustContain(strip, ["aes-expand", "expand"], "compact strip self-label (minimized must SAY so)");
mustContain(strip, ["Hooking Sweep", "INTENT_ACTOR_SENTINEL", "INTENT_TARGET_SENTINEL", "high",
  "the club drops low", "step outside the hook", "I brace."], "compact enemy intent");
expect(occurrences(strip, "Hooking Sweep") === 1, "compact enemy intent appears exactly once");
for (const clutter of ["WHAT YOU DID", "WHAT HAPPENED", "PLAYER_DAMAGE_SENTINEL",
  "Driving Advance", "ON THE FIELD"]) {
  expect(!strip.includes(clutter), `compact War Room omits prior-exchange clutter: ${clutter}`);
}
expect(typeof sandbox.aetherHudExpand === "function", "window.aetherHudExpand exists");
expect(registry.get("aes_hud_min")._html !== undefined, "min button present");

// 2) expand — content must CHANGE and become the full tabbed sheet (default tab from merge)
sandbox.aetherHudExpand();
await tick(); await tick(); await tick();
expect(body._html !== strip, "expand actually re-rendered (stale-body guard)");
mustContain(body._html, ["aes-tabs"], "full sheet tab bar");
mustContain(body._html, ["HTTP 400", "unsupported request field"], "full HUD upstream error");
expect(settings_tab_defined(), "per-key hud merge filled missing keys (tab defined)");
function settings_tab_defined() {
  return typeof ctx.extensionSettings.aetherstate.hud.tab === "string";
}
// war-room lane rides above the tabs while combat is active
mustContain(body._html, ["WAR ROOM", "Raider", "9/14", "tracked", "down", "Silent One"],
            "war-room lane");
mustContain(body._html, ["Ash Focus 4/8", "Unsafe Pool 1/2", "#b56cff"],
  "full HUD custom resources");
expect(!body._html.includes("Newest Roll Sentinel"),
  "full combat HUD does not duplicate the latest roll already owned by WHAT YOU DID");
expect(!body._html.includes("red;display:none"), "full HUD rejects unsafe resource color");
mustContain(body._html, ["Hooking Sweep", "INTENT_ACTOR_SENTINEL", "INTENT_TARGET_SENTINEL", "high",
  "DELIVERY_PIPE_SENTINEL", "the club drops low", "step outside the hook", "jam the club",
  "I brace."], "full enemy intent");
expect(occurrences(body._html, "Hooking Sweep") === 1, "full enemy intent appears exactly once");
mustContain(body._html, ["WHAT HAPPENED", "WHAT IS COMING", "WHAT YOU CAN DO", "ON THE FIELD",
  "Driving Advance", "RESOLVED_ACTOR_SENTINEL", "RESOLVED_TARGET_SENTINEL",
  "RESOLVED_DELIVERY_SENTINEL", "MISSES", "no damage", "Impact HP", "20/20"],
  "resolved enemy action");
mustContain(body._html, ["This move is committed as the next enemy threat",
  "Danger is the committed threat level", "Grounded responses to the committed move",
  "Code-owned action order", "Player-facing code truth for the current combat exchange"],
  "War Room hover explanations");
mustContain(body._html, ["WHAT YOU DID", "PLAYER_NO_IMPACT_SENTINEL",
  "PLAYER_NO_TARGET_IMPACT_TEXT", "PLAYER_DAMAGE_SENTINEL", "PLAYER_EXACT_DAMAGE_TEXT",
  'class="aes-roll-impact none"', 'class="aes-roll-impact damage"'],
  "current-turn Player impact truth");
const playerNoImpactAt = body._html.indexOf("PLAYER_NO_IMPACT_SENTINEL");
const playerDamageAt = body._html.indexOf("PLAYER_DAMAGE_SENTINEL");
expect(playerNoImpactAt >= 0 && playerNoImpactAt < playerDamageAt,
  "current-turn Player impacts preserve backend order");
expect(occurrences(body._html, "PLAYER_NO_IMPACT_SENTINEL") === 1 &&
       occurrences(body._html, "PLAYER_DAMAGE_SENTINEL") === 1,
  "current-turn Player impacts render exactly once each");
mustContain(body._html, ["Caravan Ambush", "Baser Hollow", "3 active", "0 defeated",
  "3 queued / 6"], "expanded finite cohort summary");
mustContain(body._html, ["aes-world-pulse", "active world change", "blocked quest",
  "aes-tab-badge"], "world summary stays discoverable outside the World tab");
expect(occurrences(body._html, "Driving Advance") === 1,
  "resolved enemy action appears exactly once");
const playerActionAt = body._html.indexOf("WHAT YOU DID");
const happenedAt = body._html.indexOf("WHAT HAPPENED");
const comingAt = body._html.indexOf("WHAT IS COMING");
const optionsAt = body._html.indexOf("WHAT YOU CAN DO");
const fieldAt = body._html.indexOf("ON THE FIELD");
expect(playerActionAt >= 0 && playerActionAt < happenedAt && happenedAt < comingAt &&
       comingAt < optionsAt && optionsAt < fieldAt,
  "War Room follows Player action, opposition, coming, options, field hierarchy");
for (const clutter of ["TIMING_COMMITTED_SENTINEL", "CADENCE_SETUP_SENTINEL",
  "RISK_RECOVERY_SENTINEL", "this action happened", "the INCOMING card below is the next attack",
  "future move · no impact yet", "recruit an ally"]) {
  expect(!body._html.includes(clutter), `War Room omits low-value clutter: ${clutter}`);
}
const followingIntent = payload.war_room.intent;
payload.war_room.intent = null;
sandbox.aetherHudTab("char");
mustContain(body._html, ["WHAT HAPPENED", "Driving Advance", "ON THE FIELD"],
  "resolved enemy action without following intent");
expect(!body._html.includes("WHAT IS COMING") && !body._html.includes("WHAT YOU CAN DO"),
  "action-only War Room renders no empty future sections");
expect(!body._html.includes("Hooking Sweep"),
  "action-only War Room renders no absent intent card");
payload.war_room.intent = followingIntent;
sandbox.aetherHudTab("char");
const goodCounters = payload.war_room.intent.counterplay;
payload.war_room.intent.counterplay = { malformed: true };
sandbox.aetherHudTab("char");
expect(!body._html.includes("HUD render error"), "malformed intent counterplay fails closed");
mustContain(body._html, ["INTENT_ACTOR_SENTINEL", "DELIVERY_PIPE_SENTINEL"],
  "malformed counterplay preserves exact intent identity");
payload.war_room.intent.counterplay = goodCounters;

const activeWarRoom = payload.war_room;
payload.war_room = { ...activeWarRoom, active: false, round: null, intent: null,
  order: [], combatants: [], battle: null,
  opposition: { ...activeWarRoom.opposition, move_name: "Measured Cut", tier: "HITS",
    damage: 2, hp_cur: 0, hp_max: 3, current_hp_cur: 1, current_hp_max: 3 } };
registry.get("aes_hud_ref").onclick();
await tick(); await tick();
mustContain(body._html, ["combat ended · last exchange", "WHAT HAPPENED", "Measured Cut",
  "2 damage", "Impact HP", "0/3", "Current HP", "1/3"],
  "inactive lethal War Room preserves impact and current HP");
for (const absent of ["WHAT IS COMING", "WHAT YOU CAN DO", "ON THE FIELD", "Hooking Sweep",
  "Raider", "recruit an ally"]) {
  expect(!body._html.includes(absent), `inactive lethal War Room omits ${absent}`);
}
payload.war_room = activeWarRoom;
sandbox.aetherHudTab("char");

// 3) every tab renders its markers
const TAB_MARKERS = {
  char: ["Attributes", "might", "obsession", "Reach the coast"],
  skills: ["Blades", "Witch-Sight", "Ash Focus 2", "Sealed Path",
    "Measured cuts and defensive parries.", "Used for: slash, parry",
    "Reads &lt;sealed&gt; patterns without proving them true.",
    "Unavailable due to world change"],
  abilities: ["Adrenal Surge", "Wasteland-Tough", "Cyber-Ware", "advantage",
    "Magnifies distant details.", "Telescopic eye implant.",
    "World-Locked Technique", "Unavailable due to world change"],
  rolls: ["aes-rollbtn", "Custom roll", "Adrenal Surge", "World-Locked Technique",
    "unavailable due to world change", "Recent checks",
    "Newest Roll Sentinel", "Older Roll Sentinel"],
  gear: ["Rusty Machete", "Its balance steadies defensive cuts.", "Patched Jacket",
    "Patch Kit", "Repairs one torn field layer.", "open:"],
  inventory: ["Ration", "Iron Key"],
  status: ["Bleeding", "Condition", "Consent boundaries", "romance"],
  world: ["Mira", "Cross the Amber Road", "Amber Road Open", "Raiders struck", "Roadwrights",
    "World circumstance", "Ashfall has closed the eastern road",
    "Location circumstance", "The old mill floor is flooded",
    "World condition", "Marked by the ash storm",
    "Unavailable due to world change", "Available under current world conditions",
    "World modifier", "-12", "Reputation modifier", "+7", "-5",
    "Their river toll is suspended",
    "Claims &amp; Events", "What was said", "Who knows, believes, doubts, or treats it as rumor",
    "Accepted facts", "Admitted world events and history", "knows", "believes", "doubts", "rumor",
    "active", "expired", "reversed", "superseded", "front:roadwrights",
    "world, location", "cause not known", "relates to Roadwrights"],
};
for (const [tab, markers] of Object.entries(TAB_MARKERS)) {
  try {
    sandbox.aetherHudTab(tab);
    if (!body._html || body._html.length < 80) fail(`tab ${tab}: rendered ${body._html.length} chars`);
    mustContain(body._html, markers, "tab " + tab);
  } catch (e) { fail(`tab ${tab} THREW: ` + (e && e.stack || e)); }
}
sandbox.aetherHudTab("abilities");
expect(occurrences(body._html, "Burn stamina to push a strike.") === 1,
  "ability effect and equivalent description render only once");
expect(!body._html.includes("  burn   stamina"),
  "ability prose deduplication ignores case and whitespace differences");
sandbox.aetherHudTab("skills");
expect(!body._html.includes("<sealed>"),
  "skill descriptions are HTML-escaped before visible rendering");
sandbox.aetherHudTab("world");
expect(!body._html.includes("HIDDEN_CAUSE_SENTINEL") &&
       !body._html.includes("HIDDEN_REVERSAL_CAUSE_SENTINEL") &&
       !body._html.includes("HIDDEN_SUPERSESSION_CAUSE_SENTINEL"),
  "World knowledge never renders hidden event causes");
expect(!body._html.includes("RAW_CHAT_PROSE_SENTINEL") &&
       !body._html.includes("RAW_CLAIM_PROSE_SENTINEL"),
  "World knowledge ignores raw prose fields outside the typed projection");
expect(!body._html.includes("expired_by_duration") &&
       !body._html.includes(">reversal<") &&
       !body._html.includes(">supersession<"),
  "World knowledge translates lifecycle codes into plain Player-facing history status");
expect(occurrences(body._html, "Unavailable due to world change") >= 1,
  "World tab plainly marks an unavailable quest");
expect(occurrences(body._html, "Reputation modifier") === 2,
  "actor and faction reputation modifiers render as separate typed rows");
expect(!body._html.includes("amber_road_open=true"),
  "World tab translates raw key=value flags into readable Player labels");

sandbox.aetherHudTab("inventory");
expect(!body._html.includes("Patch Kit"),
  "Items tab does not duplicate stowed equipment owned by Gear");

// 4) Rolls actions expose their real combined cost. Custom labels/colors remain presentation
// data, never CSS classes; an unaffordable or recharging button cannot insert a command.
sandbox.aetherHudTab("rolls");
const rollsHtml = body._html;
expect(!rollsHtml.includes("WHAT YOU DID"),
  "Rolls tab does not duplicate Player impacts in the persistent War Room");
const newestRollAt = rollsHtml.indexOf("Newest Roll Sentinel");
const olderRollAt = rollsHtml.indexOf("Older Roll Sentinel");
expect(newestRollAt >= 0 && olderRollAt >= 0 && newestRollAt < olderRollAt,
  "Rolls tab shows complete history newest-first");
mustContain(rollsHtml, ["Check · Newest Roll Sentinel", "Turn 12",
  "Check · Older Roll Sentinel", "Turn 10"],
  "roll history labels each genuine result and turn");
expect(occurrences(rollsHtml, 'class="aes-roll-history"') === 2,
  "roll history keeps two genuine rows visible");
mustContain(rollsHtml, ["No target impact", "2 damage to Raider",
  'class="aes-roll-impact none"', 'class="aes-roll-impact damage"'],
  "roll history renders backend impact truth independently of success tier");
sandbox.aetherHudTab("skills");
expect(!body._html.includes("Recent checks") && !body._html.includes("Older Roll Sentinel"),
  "roll history lives in Rolls while Skills keeps only the newest header summary");
sandbox.aetherHudTab("rolls");
mustContain(rollsHtml,
  ["Ash Focus 3", "Ash Focus 5", "cannot pay", "--aes-resource-color:#b56cff",
    "Cooldown Dash", "recharging (2t)", 'aria-disabled="true"', "aes-action-cost"],
  "roll costs and affordability");
expect(rollsHtml.includes('class="aes-rollbtn act gated unaffordable"'),
  "combined skill plus active cost is visibly unaffordable");
const cooldownNameAt = rollsHtml.indexOf("Cooldown Dash");
const cooldownButton = cooldownNameAt < 0 ? "" : rollsHtml.slice(
  rollsHtml.lastIndexOf("<button", cooldownNameAt), rollsHtml.indexOf("</button>", cooldownNameAt));
expect(cooldownButton.includes('class="aes-rollbtn act gated"') &&
       !cooldownButton.includes("unaffordable") && cooldownButton.includes("recharging (2t)"),
  "cooldown gating remains distinct from affordability");
expect(!/class="[^"]*ash_focus/.test(rollsHtml),
  "custom resource id never becomes a Rolls-tab CSS class");
expect(!rollsHtml.includes("red;display:none"), "unsafe resource color stays out of roll costs");

const sealedSkillAt = rollsHtml.indexOf("Sealed Path");
const sealedSkillButton = sealedSkillAt < 0 ? "" : rollsHtml.slice(
  rollsHtml.lastIndexOf("<button", sealedSkillAt), rollsHtml.indexOf("</button>", sealedSkillAt));
expect(sealedSkillButton.includes('class="aes-rollbtn gated world-unavailable"') &&
       sealedSkillButton.includes('aria-disabled="true"') &&
       sealedSkillButton.includes("unavailable due to world change"),
  "world-ineligible skill is visibly disabled");
const lockedAbilityAt = rollsHtml.indexOf("World-Locked Technique");
const lockedAbilityButton = lockedAbilityAt < 0 ? "" : rollsHtml.slice(
  rollsHtml.lastIndexOf("<button", lockedAbilityAt), rollsHtml.indexOf("</button>", lockedAbilityAt));
expect(lockedAbilityButton.includes("gated world-unavailable") &&
       lockedAbilityButton.includes('aria-disabled="true"') &&
       lockedAbilityButton.includes("unavailable due to world change"),
  "world-ineligible ability is visibly disabled");

sendTextarea.value = "";
const blockedWorldButton = { title: "unavailable due to world change",
  classList: { contains: (name) => name === "gated" } };
expect(sandbox.aetherTryRoll(blockedWorldButton, "sealed_path", "") === false &&
       sendTextarea.value === "",
  "world-ineligible skill cannot enter the composer");
const blockedRollButton = { title: "cannot pay Ash Focus 5",
  classList: { contains: (name) => name === "gated" } };
expect(sandbox.aetherTryRoll(blockedRollButton, "blades", "surge") === false &&
       sendTextarea.value === "",
  "gated unaffordable action cannot enter the composer");
const readyRollButton = { title: "ready", classList: { contains: () => false } };
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "") === true &&
       sendTextarea.value === "((aether.check blades))",
  "affordable ready action still enters the composer");

sendTextarea.value = "";
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === "((aether.check blades use surge))",
  "active selection with no draft creates one combined paid action");

sendTextarea.value = "I slash at the raider. ((aether.check blades))";
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === "I slash at the raider. ((aether.check blades use surge))",
  "matching active ability upgrades the draft check in place");
expect(occurrences(sendTextarea.value, "((aether.check") === 1,
  "active ability upgrade cannot create a second paid roll");

expect(sandbox.aetherTryRoll(readyRollButton, "stealth", "") === true &&
       sendTextarea.value === "I slash at the raider. ((aether.check stealth))",
  "ordinary skill selection replaces the one current draft action");
expect(sandbox.aetherTryRoll(readyRollButton, "stealth", "") === true &&
       occurrences(sendTextarea.value, "((aether.check") === 1,
  "reselecting the current draft check is idempotent");

expect(typeof sandbox.aetherSetSeparateRoll === "function",
  "explicit separate-action control exists");
sandbox.aetherSetSeparateRoll(true);
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "") === true &&
       sendTextarea.value === "I slash at the raider. ((aether.check stealth)) ((aether.check blades))",
  "armed separate-action path appends one intentional independent check");
expect(sandbox.aetherSeparateRollArmed() === false,
  "separate-action control is one-shot after a successful insertion");
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === "I slash at the raider. ((aether.check stealth)) ((aether.check blades use surge))",
  "ability upgrades the matching draft even when a separate check precedes it");
expect(occurrences(sendTextarea.value, "((aether.check") === 2,
  "matching upgrade preserves only the explicitly requested two actions");

const selectedDraft = sendTextarea.value;
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === selectedDraft,
  "selecting the same active twice is idempotent");
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "precision") === true &&
       sendTextarea.value === "I slash at the raider. ((aether.check stealth)) ((aether.check blades use precision))",
  "a different active replaces the prior upgrade instead of stacking");

sendTextarea.value = "Lead ((aether.check blades)) bridge ((aether.check stealth)) tail ((aether.check blades))";
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === "Lead ((aether.check blades)) bridge ((aether.check stealth)) tail ((aether.check blades use surge))",
  "multiple-action draft upgrades the rightmost matching skill and preserves prose");

sendTextarea.value = "Hold ((aether.check stealth))";
sandbox.aetherSetSeparateRoll(true);
const heldDraft = sendTextarea.value;
expect(sandbox.aetherTryRoll(blockedRollButton, "blades", "surge") === false &&
       sandbox.aetherSeparateRollArmed() === true && sendTextarea.value === heldDraft,
  "gated no-op preserves the armed separate action and draft bytes");
expect(sandbox.aetherTryRoll(readyRollButton, "blades", "surge") === true &&
       sendTextarea.value === "Hold ((aether.check stealth)) ((aether.check blades use surge))" &&
       sandbox.aetherSeparateRollArmed() === false,
  "next successful separate action appends once and auto-disarms");

const customRoll = documentStub.getElementById("aes_roll_custom");
sendTextarea.value = "Custom prose ((aether.check stealth))";
customRoll.value = "blades use surge";
expect(sandbox.aetherInsertCustom() === true &&
       sendTextarea.value === "Custom prose ((aether.check blades use surge))" &&
       customRoll.value === "",
  "custom composer uses the same one-action replacement contract");
customRoll.value = "((aether.check blades)) ((aether.check stealth))";
const customBefore = sendTextarea.value;
expect(sandbox.aetherInsertCustom() === false && sendTextarea.value === customBefore &&
       customRoll.value === "((aether.check blades)) ((aether.check stealth))",
  "custom field cannot smuggle multiple paid checks around the separate-action control");

// 5) a renderer throw must be VISIBLE, never stale content (poison then render)
payload.rules = { dice: "2d6", keep: 2, thresholds: "boom" };   // .map on a string -> throw
sandbox.aetherHudTab("skills");
mustContain(body._html, ["HUD render error"], "visible render-error fallback");

// 6) compact War Room keeps the live decision only; resolved history stays in the full HUD
payload.rules = { dice: "2d6", keep: 2, thresholds: [] };
registry.get("aes_hud_min").onclick();       // back to compact
await tick(); await tick(); await tick();
mustContain(body._html, ["WHAT IS COMING", "WHAT YOU CAN DO", "Hooking Sweep",
  "INTENT_TARGET_SENTINEL", "high", "the club drops low", "step outside the hook", "I brace.",
  "COHORT", "Baser Hollow", "3 active", "3 queued / 6", "aes-expand"],
  "compact war-room strip");
for (const clutter of ["WHAT HAPPENED", "WHAT YOU DID", "PLAYER_NO_IMPACT_SENTINEL",
  "PLAYER_DAMAGE_SENTINEL", "Driving Advance"]) {
  expect(!body._html.includes(clutter), `compact War Room omits resolved history: ${clutter}`);
}
expect(occurrences(body._html, "Hooking Sweep") === 1,
  "mid-fight compact enemy intent appears exactly once");
expect(!body._html.includes("Raider 9/14") && !body._html.includes("ON THE FIELD"),
  "compact War Room omits roster cards");

payload.war_room = { ...activeWarRoom, active: false, round: null, intent: null,
  order: [], combatants: [], battle: null,
  opposition: { ...activeWarRoom.opposition, move_name: "Measured Cut", tier: "HITS",
    damage: 2, hp_cur: 0, hp_max: 3, current_hp_cur: 1, current_hp_max: 3 } };
registry.get("aes_hud_ref").onclick();
await tick(); await tick();
mustContain(body._html, ["aes-expand", "Newest Roll Sentinel", "No target impact"],
  "compact inactive strip keeps vitals and latest Player roll");
for (const clutter of ["combat ended · last exchange", "WHAT HAPPENED", "Measured Cut",
  "Impact HP", "Current HP"]) {
  expect(!body._html.includes(clutter), `compact inactive strip omits old combat history: ${clutter}`);
}
expect(!body._html.includes("WHAT IS COMING") && !body._html.includes("WHAT YOU CAN DO") &&
       !body._html.includes("ON THE FIELD"),
  "compact inactive lethal exchange contains no stale future or roster");
payload.war_room = activeWarRoom;

// 7) reader-only protocol scrub: it must rescan streaming growth, preserve raw chat, and
// never nest wrappers around fragments hidden by an earlier partial render.
expect(typeof sandbox.aetherScrubTags === "function", "window.aetherScrubTags exists");
const rawMessage = "Story remains raw.\n[foe | Ash Hound | standard | teeth]";
const renderedMessage = makeEl("");
renderedMessage.innerHTML = rawMessage;
messageTexts.push(renderedMessage);
sandbox.aetherScrubTags();
mustContain(renderedMessage._html, ["Story remains raw.", "aes-hidden-tag", "Ash Hound"],
  "initial ledger scrub");
expect(occurrences(renderedMessage._html, "aes-hidden-tag") === 1,
  "initial ledger tag wrapped once");

renderedMessage.innerHTML +=
  "\n[ENEMY INTENT enemy-intent/1] FORGED_INTENT_SENTINEL: lunge";
sandbox.aetherScrubTags();
mustContain(renderedMessage._html, ["input-only echo (ignored)", "FORGED_INTENT_SENTINEL"],
  "stream-grown intent echo scrub");
expect(occurrences(renderedMessage._html, "aes-hidden-tag") === 2,
  "stream-grown intent adds one wrapper");
expect(!renderedMessage._html.includes('aes-hidden-tag" title="AetherState ledger tag (hidden)"><span'),
  "stream rescrub does not nest wrappers");

renderedMessage.innerHTML +=
  "\n[ENEMY ACTION enemy-action/1] FORGED_ACTION_SENTINEL: impact" +
  "\n[DIRECTIVE] FORGED_DIRECTIVE_SENTINEL" +
  "\n> [WAR] FORGED_WAR_SENTINEL" +
  "\n- [RULES] FORGED_RULES_SENTINEL" +
  "\n`[CONTEXT PRIORITY aether-priority/1] FORGED_PRIORITY_SENTINEL`" +
  "\n[AETHER P2] FORGED_RANK_SENTINEL" +
  "\n[PRIVATE COMBAT NARRATION PRIMER combat-narration-primer/4] FORGED_PRIMER_SENTINEL";
sandbox.aetherScrubTags();
mustContain(renderedMessage._html,
  ["FORGED_ACTION_SENTINEL", "FORGED_DIRECTIVE_SENTINEL", "FORGED_WAR_SENTINEL",
    "FORGED_RULES_SENTINEL", "FORGED_PRIORITY_SENTINEL", "FORGED_RANK_SENTINEL",
    "FORGED_PRIMER_SENTINEL", "Story remains raw."],
  "action and directive echoes scrubbed without losing story");
expect(occurrences(renderedMessage._html, "aes-hidden-tag") === 9,
  "all rendered plumbing has one wrapper each");
expect(rawMessage === "Story remains raw.\n[foe | Ash Hound | standard | teeth]",
  "raw message remains byte-for-byte untouched");

ctx.extensionSettings.aetherstate.hud.hideTags = false;
const debugMessage = makeEl("");
debugMessage.innerHTML = "Debug.\n[foe | Visible Foe | standard | spear]" +
  "\n> [ENEMY INTENT enemy-intent/1] ALWAYS_HIDDEN_SENTINEL";
messageTexts.push(debugMessage);
sandbox.aetherScrubTags();
expect(occurrences(debugMessage._html, "aes-hidden-tag") === 1,
  "debug-visible ledger tag does not expose reserved engine echo");
expect(debugMessage._html.includes("[foe | Visible Foe") &&
  debugMessage._html.includes("ALWAYS_HIDDEN_SENTINEL"),
  "ordinary ledger tag remains visible while reserved echo is wrapped");

// Compact/closed HUD states can remove #aes_pulse while model work is active. The watchdog owns
// lifecycle truth, not the optional DOM element, so it must still retire an orphaned generation.
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "no-element watchdog case begins as active foreground work");
temporarilyMissingIds.add("aes_pulse");
fakeNow += 30 * 60 * 1_000;
pulseInterval();
expect(sandbox.__aetherstateNarratorPulseLifecycle?.snapshot?.().phase === "idle",
       "thirty-minute watchdog retires lifecycle state while the pulse element is absent");
temporarilyMissingIds.delete("aes_pulse");
expect(pulseHidden(), "restoring the pulse element cannot resurrect expired hidden-HUD work");

// Teardown runs last because a correct disposer removes the very event listeners used by the
// preceding smoke checks. The captured pulse callback is invoked once after disposal to prove that
// even a queued old tick cannot resurrect the indicator.
const pagehide = windowHandlers.get("pagehide") || windowHandlers.get("beforeunload")
  || windowHandlers.get("unload");
expect(typeof pagehide === "function", "extension teardown hook registered for narrator lifecycle");
generationStarted("normal", {}, false);
afterData(false);
expect(pulseVisible(), "teardown case begins with active narrator work");
if (typeof pagehide === "function") pagehide({ type: "pagehide" });
expect(pulseHidden(), "extension teardown retires active narrator work");
expect(intervalCallbacks.filter((entry) => entry.delay === 1000 && entry.active).length === 0,
       "extension teardown clears the narrator interval without leaking it");
expect([...activeEventHandlers.values()].every((handlers) => handlers.size === 0),
       "extension teardown detaches every registered SillyTavern event handler");
expect(!windowHandlers.has("pagehide") && !windowHandlers.has("beforeunload")
       && !windowHandlers.has("unload"),
       "extension teardown detaches its page lifecycle handlers");

// A timer callback may already be queued when an old module instance is replaced. Likewise, an
// event emitter can hold a captured callback while teardown removes it. Disposed callbacks must
// return without touching the shared DOM now owned by the replacement instance.
pulse.style.display = "";
pulse.textContent = "REPLACEMENT_INSTANCE_PULSE_SENTINEL";
pulseInterval();
chatChangedForPulse();
generationStopped();
messageSwiped(1);
expect(pulse.style.display === "" &&
       pulse.textContent === "REPLACEMENT_INSTANCE_PULSE_SENTINEL",
       "queued callbacks from a disposed module cannot hide or rewrite the replacement pulse");

done();
