import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, "..", "src", "aetherstate", "static", "creator.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
assert.equal(scripts.length, 1, "Creator should keep one auditable inline controller");
new vm.Script(scripts[0], { filename: "creator.html" });
assert.match(
  html,
  /id="c_custom_summary_count"[^>]*>0 skills · 0 abilities<\/span>/,
  "the closed custom-mechanics disclosure must advertise its live row counts",
);

function extractFunction(name) {
  const start = html.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const brace = html.indexOf("{", start);
  let depth = 0;
  for (let i = brace; i < html.length; i += 1) {
    if (html[i] === "{") depth += 1;
    if (html[i] === "}") {
      depth -= 1;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

// A committed Character prefill can restore mechanics inside a collapsed disclosure. The
// disclosure summary must advertise those rows and keep its count current without changing ids.
const customPrefillSandbox = {
  skillRows: [], abilityRows: [], addedSkills: [], addedAbilities: [],
  summary: { textContent: "" },
};
const customHosts = {
  c_cskills: {
    get innerHTML() { return ""; },
    set innerHTML(value) { if (value === "") customPrefillSandbox.skillRows.length = 0; },
    querySelectorAll: () => customPrefillSandbox.skillRows,
  },
  c_cabil: {
    get innerHTML() { return ""; },
    set innerHTML(value) { if (value === "") customPrefillSandbox.abilityRows.length = 0; },
    querySelectorAll: () => customPrefillSandbox.abilityRows,
  },
  c_custom_summary_count: customPrefillSandbox.summary,
};
customPrefillSandbox.$ = (id) => customHosts[id] || null;
customPrefillSandbox.addCustomSkill = (definition) => {
  customPrefillSandbox.addedSkills.push(structuredClone(definition));
  customPrefillSandbox.skillRows.push(definition);
  customPrefillSandbox.refreshCustomDefinitionSummary();
};
customPrefillSandbox.addCustomAbil = (definition, skillDefinitions) => {
  customPrefillSandbox.addedAbilities.push(structuredClone(definition));
  customPrefillSandbox.abilityRows.push(definition);
  customPrefillSandbox.refreshCustomDefinitionSummary();
};
vm.createContext(customPrefillSandbox);
vm.runInContext([
  "customSkillId", "customDefinitionCountLabel",
  "refreshCustomDefinitionSummary", "applyPlayerCustomDefinitions",
].map(extractFunction).join("\n"), customPrefillSandbox,
{ filename: "creator-custom-prefill-contract.js" });

customPrefillSandbox.refreshCustomDefinitionSummary();
assert.equal(customPrefillSandbox.summary.textContent, "0 skills · 0 abilities");
customPrefillSandbox.skillRows.push({});
customPrefillSandbox.refreshCustomDefinitionSummary();
assert.equal(customPrefillSandbox.summary.textContent, "1 skill · 0 abilities");
customPrefillSandbox.skillRows.push({});
customPrefillSandbox.abilityRows.push({});
customPrefillSandbox.refreshCustomDefinitionSummary();
assert.equal(customPrefillSandbox.summary.textContent, "2 skills · 1 ability");

const restoredSkillDefinitions = {
  oathglass_triage: {
    name: "Oathglass Triage", keyed_stat: "INT", max_rank: 6,
    governs: ["diagnose", "stabilize"], group: "Bridgecraft",
    desc: "Reads exact fracture geometry.",
  },
};
const restoredAbilityDefinitions = {
  crack_map_instinct: {
    name: "Crack-Map Instinct", kind: "passive", mechanic: "edge", magnitude: 2,
    applies_to: "oathglass_triage", group: "Bridgecraft",
    effect: "Gain edge while mapping a fresh crack.",
  },
};
customPrefillSandbox.applyPlayerCustomDefinitions({
  skills: { oathglass_triage: 2 },
  defs: { skills: restoredSkillDefinitions, abilities: restoredAbilityDefinitions },
});
assert.deepEqual(customPrefillSandbox.addedSkills.at(-1), {
  id: "oathglass_triage", ...restoredSkillDefinitions.oathglass_triage, _rank: 2,
});
assert.deepEqual(customPrefillSandbox.addedAbilities.at(-1), {
  ...restoredAbilityDefinitions.crack_map_instinct,
});
assert.equal(customPrefillSandbox.summary.textContent, "1 skill · 1 ability");

function field(value) {
  return {
    value,
    dataset: {},
    attrs: {},
    removeAttribute(name) { delete this.attrs[name]; },
    setAttribute(name, next) { this.attrs[name] = next; },
    setCustomValidity(message) { this.validationMessage = message; },
    focus() { this.focused = true; },
  };
}

function resourceRow(id, name, cur = 4, max = 8, color = "#b56cff") {
  const values = {
    name: field(name), cur: field(String(cur)), max: field(String(max)), color: field(color),
  };
  return {
    dataset: { resourceId: id },
    querySelector(selector) {
      const match = selector.match(/data-field="([^"]+)"/);
      return match ? values[match[1]] : null;
    },
  };
}

function customSkillRow(def, rank) {
  const inputs = [
    field(def.name || ""), field(def.keyed_stat || "DEX"), field(String(def.base_mod || 0)),
    field(String(rank)), field(""), field(def.group || ""),
    field(Array.isArray(def.governs) ? def.governs.join(", ") : String(def.governs || "")),
    field(def.desc || ""),
  ];
  return {
    dataset: { def: JSON.stringify(def) },
    querySelectorAll() { return inputs; },
  };
}

function customAbilityRow(def) {
  const inputs = [
    field(def.name || ""), field(def.kind || "active"), field(def.mechanic || ""),
    field(def.applies_to || ""), field(String(def.magnitude || 0)), field(""),
    field(String(def.cooldown_turns || "")), field(def.effect || ""), field(def.group || ""),
    field(def.desc || ""),
  ];
  return {
    dataset: { def: JSON.stringify(def) },
    querySelectorAll() { return inputs; },
  };
}

function dataRow(values) {
  const fields = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, field(String(value))]));
  return {
    querySelector(selector) {
      const match = selector.match(/data-field="([^"]+)"/);
      return match ? fields[match[1]] : null;
    },
  };
}

const labels = new Map([
  ["hp", "hp"], ["stamina", "stamina"], ["mana", "mana"],
  ["focus", "focus"], ["ash focus", "focus"], ["tempo", "tempo"],
]);
const sandbox = {
  REG: { creator_limits: { resource_cost_min: 1, resource_cost_max: 10000 } },
  BUILTIN_RESOURCE_IDS: new Set(["hp", "stamina", "mana"]),
  resourceRows: [],
  customSkillRows: [],
  customAbilityRows: [],
  frontRows: [],
  routeRows: [],
  lootRows: { minion: [], standard: [], elite: [], boss: [] },
  LOOT_TIERS: ["minion", "standard", "elite", "boss"],
  resourceIdFor(label) { return labels.get(String(label).trim().toLowerCase()) || ""; },
  resourceLabel(id) { return id === "focus" ? "Ash Focus" : id[0].toUpperCase() + id.slice(1); },
};
sandbox.$ = (id) => ({
  querySelectorAll: () => {
    if (id === "c_cskills") return sandbox.customSkillRows;
    if (id === "c_cabil") return sandbox.customAbilityRows;
    if (id === "w_fronts") return sandbox.frontRows;
    if (id === "w_routes") return sandbox.routeRows;
    if (id.startsWith("w_loot_")) return sandbox.lootRows[id.slice("w_loot_".length)] || [];
    return sandbox.resourceRows;
  },
});
vm.createContext(sandbox);
vm.runInContext([
  "customSkillId", "creatorResourceError", "clearCreatorFieldError", "markCreatorFieldError",
  "resourceCostLimits", "readCustomResources", "parseResourceCost", "readCostInput", "costStr",
  "readCustomSkills", "readCustomAbils", "readFronts", "readLoot", "readRoutes",
].map(extractFunction).join("\n"), sandbox, { filename: "creator-resource-contract.js" });

sandbox.customSkillRows = [customSkillRow({
  id: "rope_dart", name: "Rope-Dart", keyed_stat: "DEX", max_rank: 3, _rank: 3,
  governs: ["bind", "redirect"], desc: "Controls space with a weighted line.",
}, 4)];
let [skill] = JSON.parse(JSON.stringify(sandbox.readCustomSkills()));
assert.equal(skill.max_rank, 4, "a visible rank edit must raise a stale hidden AI ceiling");
assert.equal(skill._rank, undefined, "the transient UI rank must not leak into the frozen definition");
assert.deepEqual(skill.governs, ["bind", "redirect"], "visible governs verbs must round-trip");
assert.equal(skill.desc, "Controls space with a weighted line.", "visible skill prose must round-trip");

sandbox.customSkillRows = [customSkillRow({
  id: "rope_dart", name: "Rope-Dart", keyed_stat: "DEX", max_rank: 3,
}, 2)];
[skill] = JSON.parse(JSON.stringify(sandbox.readCustomSkills()));
assert.equal(skill.max_rank, 3, "a lower visible rank must not reduce an intentional frozen ceiling");

assert.deepEqual(
  JSON.parse(JSON.stringify(sandbox.parseResourceCost("10000 Ash Focus + 2 Mana"))),
  { focus: 10000, mana: 2 },
  "the UI must preserve the documented upper bound and every resource component",
);
assert.deepEqual(
  JSON.parse(JSON.stringify(sandbox.parseResourceCost("Tempo 3, 1 Stamina"))),
  { tempo: 3, stamina: 1 },
  "both supported ordering forms and separators should remain usable",
);
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.parseResourceCost(""))), {});
assert.throws(() => sandbox.parseResourceCost("2 Removed Pool"), /Unknown resource/);
assert.throws(() => sandbox.parseResourceCost("10001 Ash Focus"), /between 1 and 10000/);
assert.throws(() => sandbox.parseResourceCost("1 Focus + 2 Ash Focus"), /appears more than once/);
assert.equal(sandbox.costStr({ focus: 2, mana: 1 }), "2 Ash Focus + 1 Mana");

sandbox.resourceRows = [resourceRow("focus", "Ash Focus"), resourceRow("tempo", "Tempo", 2, 3)];
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.readCustomResources())), {
  focus: { name: "Ash Focus", cur: 4, max: 8, color: "#b56cff" },
  tempo: { name: "Tempo", cur: 2, max: 3, color: "#b56cff" },
});

sandbox.resourceRows = [resourceRow("", "Ash Focus"), resourceRow("", "ash-focus")];
assert.throws(() => sandbox.readCustomResources(), /two rows become 'ash_focus'/);

sandbox.resourceRows = [resourceRow("first_pool", "Shared Name"), resourceRow("second_pool", "shared-name")];
assert.throws(() => sandbox.readCustomResources(), /Resource slug collision/);

sandbox.resourceRows = [resourceRow("blood", "Mana")];
assert.throws(() => sandbox.readCustomResources(), /conflicts with 'mana'/);

sandbox.resourceRows = [resourceRow("", "HP")];
assert.throws(() => sandbox.readCustomResources(), /HP already has its own field/);

vm.runInContext(extractFunction("resourceIdFor"), sandbox, { filename: "creator-resource-id.js" });
sandbox.resourceRows = [resourceRow("stamina", "Stamina", 0, 0)];
assert.equal(sandbox.readCustomResources().stamina.max, 0);
assert.equal(sandbox.resourceIdFor("Stamina"), "", "an explicitly disabled built-in is unavailable");

sandbox.customAbilityRows = [customAbilityRow({
  id: "witness_seal", name: "Witness Seal", kind: "active", mechanic: "reroll",
  applies_to: "testimony", magnitude: 2, cooldown_turns: 3, group: "Civic Disciplines",
  effect: "Rechecks one disputed record without changing its original wording.",
  desc: "A brass seal used by neutral civic recorders.",
})];
const [ability] = JSON.parse(JSON.stringify(sandbox.readCustomAbils()));
assert.equal(ability.effect, "Rechecks one disputed record without changing its original wording.");
assert.equal(ability.desc, "A brass seal used by neutral civic recorders.");
assert.equal(ability.group, "Civic Disciplines");

sandbox.frontRows = [dataRow({
  name: "The Harbor Chain Rises", faction: "Lantern Guild", segments: 6, pace: 2,
  consequence: "The refuge is sealed until the chain is lowered.", event_duration_turns: 3,
  spawn_eligibility: false,
})];
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.readFronts())), [{
  name: "The Harbor Chain Rises", faction: "Lantern Guild", segments: 6, pace: 2,
  consequence: "The refuge is sealed until the chain is lowered.", event_duration_turns: 3,
  spawn_eligibility: false,
}], "front editors must preserve duration and an explicit false spawn rule");

sandbox.lootRows.standard = [dataRow({
  name: "Sealed Lamp Oil", chance: 0.35, qty_min: 1, qty_max: 2,
})];
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.readLoot())), {
  standard: [{ name: "Sealed Lamp Oil", qty_min: 1, qty_max: 2, chance: 0.35 }],
});

sandbox.routeRows = [dataRow({ a: "Lantern Refuge", b: "East Gate", segments: 3 })];
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.readRoutes())), [
  { a: "Lantern Refuge", b: "East Gate", segments: 3 },
]);

// Enemy Workshop location identity must match the server's Creator row-head contract.
sandbox.locationRows = [];
sandbox.readLines = (id) => (id === "w_locations" ? sandbox.locationRows : []);
vm.runInContext([
  "namedRowHead", "namedRowKey", "worldLocationNames", "resolveWorldLocationName",
].map(extractFunction).join("\n"), sandbox, { filename: "creator-enemy-home-contract.js" });
sandbox.locationRows = [
  "Spindle Market: pressurized bazaar",
  "East-Gate — storm locks",
  `${"A".repeat(90)} - overlong location`,
];
assert.deepEqual(JSON.parse(JSON.stringify(sandbox.worldLocationNames())), [
  "Spindle Market", "East-Gate", "A".repeat(80),
]);
assert.equal(sandbox.resolveWorldLocationName("spindle-market"), "Spindle Market");
assert.equal(sandbox.resolveWorldLocationName("east gate"), "East-Gate");
assert.equal(sandbox.resolveWorldLocationName("missing quay"), "");

// Exercise the actual add/validation controller with a small DOM-shaped harness.
const originalDollar = sandbox.$;
const enemyHomeField = field("");
enemyHomeField.id = "e_home";
const enemyAddButton = field("");
const enemyResult = field("");
const enemyStatus = field("");
sandbox.$ = (id) => ({
  e_home: enemyHomeField,
  e_add_world: enemyAddButton,
  e_result: enemyResult,
  e_home_status: enemyStatus,
}[id] || originalDollar(id));
sandbox.enemyDraft = {
  name: "Glasswake Bell-Thief", tier: "standard", role: "infiltrator",
  type: "human saboteur", armament: "hooked blade", powers: "",
  description: "Cuts archive-barge ropes.", home: "",
};
sandbox.enemyDoc = () => ({ ...sandbox.enemyDraft });
sandbox.ENEMY_PREVIEW = { kit: { tier: "standard", basis: ["martial"], fingerprint: "enemy-1" } };
sandbox.addedNpcs = [];
sandbox.addNpc = (npc) => { sandbox.addedNpcs.push(npc); return true; };
sandbox.toasts = [];
sandbox.toast = (message, kind) => sandbox.toasts.push({ message, kind });
sandbox.shownTab = "";
sandbox.showTab = (tab) => { sandbox.shownTab = tab; };
sandbox.scheduleDraft = () => {};
vm.runInContext([
  "setEnemyHomeStatus", "addEnemyToWorld", "invalidateEnemyPreview",
].map(extractFunction).join("\n"), sandbox, { filename: "creator-enemy-add-contract.js" });

sandbox.addEnemyToWorld();
assert.equal(sandbox.addedNpcs.length, 0, "a missing home must not add an NPC");
assert.equal(enemyHomeField.attrs["aria-invalid"], "true");
assert.equal(enemyHomeField.focused, true);
assert.match(sandbox.toasts.at(-1).message, /existing World location/);

sandbox.enemyDraft.home = "spindle-market";
enemyHomeField.value = "spindle-market";
sandbox.addEnemyToWorld();
assert.equal(sandbox.addedNpcs.at(-1).home, "Spindle Market", "the canonical authored location must be stored");
assert.equal(sandbox.shownTab, "world");
assert.equal(enemyHomeField.attrs["aria-invalid"], undefined);

sandbox.shownTab = "enemy";
sandbox.addNpc = () => false;
sandbox.addEnemyToWorld();
assert.equal(sandbox.shownTab, "enemy", "a full NPC list must keep the player in the Workshop");
assert.match(sandbox.toasts.at(-1).message, /already has 20 NPCs/);
assert.ok(sandbox.ENEMY_PREVIEW, "a capacity failure must retain the valid mechanical preview");

sandbox.locationRows = [];
sandbox.enemyDraft.home = "Stale Ghost Dock";
sandbox.addedNpcs = [];
sandbox.addNpc = (npc) => { sandbox.addedNpcs.push(npc); return true; };
sandbox.addEnemyToWorld();
assert.equal(sandbox.addedNpcs.at(-1).home, "", "an unauthored stale home must not become an orphan anchor");
sandbox.locationRows = ["Spindle Market: pressurized bazaar"];
sandbox.enemyDraft.home = "spindle-market";

enemyHomeField.attrs["aria-invalid"] = "true";
sandbox.ENEMY_PREVIEW = { kit: {} };
sandbox.invalidateEnemyPreview({ target: enemyHomeField });
assert.ok(sandbox.ENEMY_PREVIEW, "changing only home must retain the mechanical preview");
assert.equal(enemyHomeField.attrs["aria-invalid"], undefined);
assert.equal(enemyStatus.textContent, "Using Spindle Market.");
enemyHomeField.value = "missing quay";
sandbox.invalidateEnemyPreview({ target: enemyHomeField });
assert.equal(enemyStatus.textContent, "Choose an existing World location before adding this enemy.");

sandbox.ENEMY_PREVIEW = { kit: {} };
enemyAddButton.disabled = false;
sandbox.invalidateEnemyPreview({ target: { id: "e_armament" } });
assert.equal(sandbox.ENEMY_PREVIEW, null, "changing a mechanical fact must invalidate the preview");
assert.equal(enemyAddButton.disabled, true);

// Home is World placement, not a combat fact, so it must not reach the preview endpoint.
const enemyNameField = field("Glasswake Bell-Thief");
const enemyPreviewButton = field("");
sandbox.$ = (id) => ({
  e_name: enemyNameField,
  e_preview: enemyPreviewButton,
  e_add_world: enemyAddButton,
  e_result: enemyResult,
  e_home: enemyHomeField,
  e_home_status: enemyStatus,
}[id] || originalDollar(id));
sandbox.enemyDoc = () => ({ ...sandbox.enemyDraft, home: "Spindle Market" });
sandbox.previewRequest = null;
sandbox.api = async (url, options) => {
  sandbox.previewRequest = { url, body: JSON.parse(options.body) };
  return { kit: { moves: [] } };
};
sandbox.renderEnemyPreview = () => {};
vm.runInContext(`async ${extractFunction("previewEnemy")}`, sandbox, { filename: "creator-enemy-preview-contract.js" });
await sandbox.previewEnemy();
assert.equal(sandbox.previewRequest.url, "/aether/enemies/preview");
assert.equal("home" in sandbox.previewRequest.body, false, "World placement must be stripped from the combat preview request");

// Delayed Creator authoring must rebase onto the current draft instead of replacing it.
const authorMergeSandbox = {};
vm.createContext(authorMergeSandbox);
vm.runInContext([
  "customSkillId", "isCreatorRecord", "creatorValuesEqual", "cloneCreatorValue",
  "creatorDefinitionMap", "canonicalPlayerDocument", "mergeCreatorValue", "mergeAuthorResult",
].map(extractFunction).join("\n"), authorMergeSandbox, { filename: "creator-author-merge-contract.js" });
const authorBase = {
  name: "Sourcewake", setting: "", tone: "hopeful",
  npcs: [{ name: "Mara", role: "guide" }],
  resources: { hp: { cur: 20, max: 20 }, focus: { cur: 4, max: 8 } },
};
const authorGenerated = {
  name: "Sourcewake", setting: "AI-authored harbor", tone: "hopeful mystery",
  npcs: [{ name: "Mara", role: "guide" }, { name: "Orin", role: "ringer" }],
  resources: {
    hp: { cur: 20, max: 20 },
    focus: { cur: 4, max: 8, name: "Focus" },
    resonance: { cur: 3, max: 3 },
  },
};
const authorCurrent = {
  name: "Sourcewake", setting: "Player's newer harbor", tone: "hopeful",
  npcs: [{ name: "Mara", role: "harbor captain" }],
  resources: { hp: { cur: 17, max: 20 }, focus: { cur: 4, max: 8 } },
};
const authorMerged = JSON.parse(JSON.stringify(
  authorMergeSandbox.mergeAuthorResult(authorBase, authorGenerated, authorCurrent),
));
assert.equal(authorMerged.doc.setting, "Player's newer harbor", "a newer scalar edit must win");
assert.deepEqual(authorMerged.doc.npcs, authorCurrent.npcs, "a changed structured list is atomic");
assert.equal(authorMerged.doc.tone, "hopeful mystery", "an untouched field should receive AI fill");
assert.equal(authorMerged.doc.resources.hp.cur, 17, "a newer nested value must win");
assert.equal(authorMerged.doc.resources.focus.name, "Focus", "untouched nested blanks may fill");
assert.deepEqual(authorMerged.doc.resources.resonance, { cur: 3, max: 3 });
assert.deepEqual(authorMerged.preserved.sort(), ["npcs", "resources.hp.cur", "setting"]);
assert.equal(
  authorMergeSandbox.creatorValuesEqual(
    { b: [2, { z: true }], a: 1 }, { a: 1, b: [2, { z: true }] },
  ),
  true,
  "context comparison must ignore object key insertion order",
);
const canonicalPlayer = JSON.parse(JSON.stringify(authorMergeSandbox.canonicalPlayerDocument({
  name: "Rook",
  custom: {
    skills: [{ name: "Bridge Cooling", keyed_stat: "INT", desc: "Read hot oathglass." }],
    abilities: [{ name: "Oathglass Guard", kind: "passive", effect: "Warns before a crack." }],
  },
})));
assert.equal("custom" in canonicalPlayer, false, "Player drafts must have one merge shape");
assert.equal(canonicalPlayer.defs.skills.bridge_cooling.desc, "Read hot oathglass.");
assert.equal(canonicalPlayer.defs.abilities.oathglass_guard.kind, "passive");
const keyedCanonicalPlayer = JSON.parse(JSON.stringify(authorMergeSandbox.canonicalPlayerDocument({
  defs: { skills: { vac_ops: { name: "Vacuum Operations", keyed_stat: "CON" } }, abilities: {} },
})));
assert.equal(keyedCanonicalPlayer.defs.skills.vac_ops.name, "Vacuum Operations",
  "an existing canonical definition key outranks its display-name slug");
assert.equal("vacuum_operations" in keyedCanonicalPlayer.defs.skills, false);

// Character mode maps to the visible Character panel for assistive progress state.
const busyElements = Object.fromEntries(["w_ai", "c_ai", "w_template", "c_template", "world", "char"]
  .map((id) => [id, { disabled: false, attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } }]));
const busySandbox = { $: (id) => busyElements[id] || null };
vm.createContext(busySandbox);
vm.runInContext(extractFunction("setAuthoringBusy"), busySandbox, { filename: "creator-author-busy-contract.js" });
busySandbox.setAuthoringBusy("player", true);
assert.equal(busyElements.char.attrs["aria-busy"], "true");
assert.equal(busyElements.world.attrs["aria-busy"], "false");
assert.equal(busyElements.w_ai.disabled, true);

// Exercise the real async coordinator with a deferred response. This is the regression for the
// shipped lost-update bug: the Player keeps editing while the model is still working.
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const authorController = {
  SID: "author-race", CREATOR_SESSION_EPOCH: 0, WORLD_FORM_EPOCH: 2, PLAYER_FORM_EPOCH: 3,
  WORLD_ID: "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", AUTHORING_ACTIVE: "", AUTHORING_TIMER: null,
  worldCurrent: {
    world_id: "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", name: "Sourcewake", genre: "custom",
    setting: "Request snapshot", tone: "", notes: "keep mystery",
    npcs: [{ name: "Mara", role: "guide" }],
  },
  playerCurrent: { name: "Rook", concept: "ringer", notes: "no prophecy", gear: [] },
  modelField: { value: "main-model" }, calls: [], statuses: [], busy: [], toasts: [],
  worldApplied: [], playerApplied: [], validationCalls: 0,
};
authorController.$ = (id) => (id === "modelSel" ? authorController.modelField : null);
authorController.worldDoc = () => JSON.parse(JSON.stringify(authorController.worldCurrent));
authorController.playerDoc = () => JSON.parse(JSON.stringify(authorController.playerCurrent));
authorController.currentGenre = () => authorController.worldCurrent.genre;
authorController.setAuthoringBusy = (mode, busy) => authorController.busy.push({ mode, busy });
authorController.startAuthoringTimer = () => {};
authorController.stopAuthoringTimer = () => {};
authorController.setAuthorStatus = (mode, message, kind = "", retry = false) => {
  authorController.statuses.push({ mode, message, kind, retry });
};
authorController.toast = (message, kind) => authorController.toasts.push({ message, kind });
authorController.showCreatorValidation = () => { authorController.validationCalls += 1; };
authorController.applyWorld = (doc) => {
  authorController.worldApplied.push(JSON.parse(JSON.stringify(doc)));
  authorController.worldCurrent = JSON.parse(JSON.stringify(doc));
  authorController.WORLD_FORM_EPOCH += 1;
  authorController.WORLD_ID = doc.world_id;
};
authorController.applyPlayer = (doc) => {
  authorController.playerApplied.push(JSON.parse(JSON.stringify(doc)));
  authorController.playerCurrent = JSON.parse(JSON.stringify(doc));
  authorController.PLAYER_FORM_EPOCH += 1;
};
authorController.pending = deferred();
authorController.api = (url, options) => {
  authorController.calls.push({ url, body: JSON.parse(options.body) });
  return authorController.pending.promise;
};
vm.createContext(authorController);
vm.runInContext([
  "creatorSessionSnapshot", "creatorSessionIsCurrent",
  "customSkillId", "isCreatorRecord", "creatorValuesEqual", "cloneCreatorValue",
  "creatorDefinitionMap", "canonicalPlayerDocument", "mergeCreatorValue", "mergeAuthorResult",
].map(extractFunction).concat([
  "runCreatorAuthoring", "authorWorld", "authorPlayer", "fillOffline",
].map((name) => `async ${extractFunction(name)}`)).join("\n"), authorController,
{ filename: "creator-author-controller-contract.js" });

const delayedWorld = authorController.authorWorld();
assert.equal(authorController.calls.length, 1);
assert.deepEqual(authorController.busy.at(-1), { mode: "world", busy: true });
await authorController.authorPlayer();
assert.equal(authorController.calls.length, 1, "a second costly model request must not overlap");
assert.match(authorController.statuses.at(-1).message, /already running/);
authorController.worldCurrent.setting = "PLAYER EDIT WHILE WAITING";
authorController.worldCurrent.npcs = [{ name: "Mara", role: "harbor captain" }];
authorController.pending.resolve({
  source: "llm", model: "main-model",
  doc: {
    ...authorBase, world_id: "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", genre: "custom",
    setting: "Request snapshot", tone: "AI hopeful", notes: "model changed notes",
    npcs: [{ name: "Mara", role: "guide" }, { name: "Orin", role: "ringer" }],
  },
});
await delayedWorld;
assert.equal(authorController.worldApplied.length, 1);
assert.equal(authorController.worldApplied[0].setting, "PLAYER EDIT WHILE WAITING");
assert.deepEqual(authorController.worldApplied[0].npcs, [{ name: "Mara", role: "harbor captain" }]);
assert.equal(authorController.worldApplied[0].tone, "AI hopeful");
assert.equal(authorController.worldApplied[0].notes, "keep mystery");
assert.match(authorController.statuses.at(-1).message, /Kept 2 newer edits/);
assert.deepEqual(authorController.busy.at(-1), { mode: "world", busy: false });

// The author response is also scoped to the selected session because auto mode can use that
// session's configured main model. Retargeting the Creator while it runs makes the old result stale.
authorController.pending = deferred();
const worldAppliesBeforeSessionSwitch = authorController.worldApplied.length;
const staleSessionAuthor = authorController.authorWorld();
authorController.SID = "author-race-new-session";
authorController.pending.resolve({
  source: "llm", model: "old-session-model",
  doc: { ...authorController.worldCurrent, tone: "OLD SESSION RESULT" },
});
await staleSessionAuthor;
assert.equal(authorController.worldApplied.length, worldAppliesBeforeSessionSwitch,
  "an AI response started for a previous selected session must not apply to the retargeted form");
authorController.SID = "author-race";

// Custom mechanics use the same canonical defs shape on both sides of a delayed Character call.
authorController.playerCurrent = {
  name: "Rook", concept: "ringer", notes: "no prophecy", gear: [],
  custom: {
    skills: [{ name: "Signal Reading", keyed_stat: "INT", desc: "Read bridge tones." }],
    abilities: [],
  },
};
authorController.pending = deferred();
const customCharacter = authorController.authorPlayer();
assert.equal(authorController.calls.length, 3);
authorController.playerCurrent.custom.skills.push({
  name: "Bridge Cooling", keyed_stat: "INT", desc: "PLAYER ADDED while waiting",
});
authorController.playerCurrent.custom.abilities.push({
  name: "Oathglass Guard", kind: "passive", effect: "PLAYER ADDED while waiting",
});
authorController.pending.resolve({
  source: "llm", model: "main-model",
  doc: {
    name: "Rook", concept: "ringer", appearance: "AI-authored soot coat", notes: "model note",
    gear: [], defs: {
      skills: { signal_reading: { name: "Signal Reading", keyed_stat: "INT", desc: "AI-polished bridge tones." } },
      abilities: {},
    },
  },
});
await customCharacter;
assert.equal(authorController.playerApplied.length, 1);
assert.equal(authorController.playerApplied[0].defs.skills.bridge_cooling.desc, "PLAYER ADDED while waiting");
assert.equal(authorController.playerApplied[0].defs.abilities.oathglass_guard.effect, "PLAYER ADDED while waiting");
assert.equal("custom" in authorController.playerApplied[0], false);
assert.match(authorController.statuses.at(-1).message, /Kept 2 newer edits/);

// A Character result authored against a different World is not mergeable and must be skipped.
authorController.pending = deferred();
const staleCharacter = authorController.authorPlayer();
assert.equal(authorController.calls.length, 4);
authorController.worldCurrent.setting = "A different World context";
authorController.pending.resolve({
  source: "llm", model: "main-model",
  doc: { name: "Rook", concept: "ringer", appearance: "AI-authored against old World", gear: [] },
});
await staleCharacter;
assert.equal(authorController.playerApplied.length, 1, "stale-world Character output must not apply");
assert.equal(authorController.statuses.at(-1).retry, true);
assert.match(authorController.statuses.at(-1).message, /result skipped/);

// Invalid current inputs are caught before any request and route through visible validation.
authorController.playerDoc = () => { const error = new Error("bad resource"); error.creatorField = {};
  throw error; };
await authorController.authorPlayer();
assert.equal(authorController.calls.length, 4);
assert.equal(authorController.validationCalls, 1);
assert.equal(authorController.statuses.at(-1).kind, "bad");
assert.equal(authorController.statuses.at(-1).retry, true);

// Deterministic prefills join the same single-flight gate, so AI cannot start and spend while a
// template/default request is still in flight.
authorController.playerDoc = () => JSON.parse(JSON.stringify(authorController.playerCurrent));
authorController.pending = deferred();
const delayedDefaults = authorController.fillOffline("player");
assert.equal(authorController.calls.length, 5);
await authorController.authorWorld();
assert.equal(authorController.calls.length, 5, "AI must not overlap a deterministic prefill");
authorController.pending.resolve({ source: "deterministic", doc: authorController.playerCurrent });
await delayedDefaults;
assert.equal(authorController.AUTHORING_ACTIVE, "");

// A committed-session prefill belongs to the session selected when the request starts. If the
// Player selects a different game while that response is in flight, the old document must never
// land in the now-retargeted form.
const committedPrefill = {
  SID: "session-old", CREATOR_SESSION_EPOCH: 0, AUTHORING_ACTIVE: "",
  applied: [], tabs: [], statuses: [], busy: [],
  pending: deferred(),
};
committedPrefill.api = () => committedPrefill.pending.promise;
committedPrefill.applyWorld = (doc) => committedPrefill.applied.push(doc);
committedPrefill.applyPlayer = (doc) => committedPrefill.applied.push(doc);
committedPrefill.showTab = (tab) => committedPrefill.tabs.push(tab);
committedPrefill.setAuthorStatus = (...args) => committedPrefill.statuses.push(args);
committedPrefill.setAuthoringBusy = (...args) => committedPrefill.busy.push(args);
committedPrefill.toast = () => {};
vm.createContext(committedPrefill);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("loadCommitted")}`,
].join("\n"), committedPrefill,
  { filename: "creator-committed-prefill-context-contract.js" });
const stalePrefill = committedPrefill.loadCommitted("world");
committedPrefill.SID = "session-new";
committedPrefill.pending.resolve({ world: { name: "Old Session World" } });
await stalePrefill;
assert.deepEqual(committedPrefill.applied, [],
  "a prefill response from the previous selected session must not touch the current form");
assert.deepEqual(committedPrefill.tabs, [],
  "a stale prefill response must not navigate the current Creator tab");

// Review rendering is asynchronous too. An old response must not replace the visible review after
// the Player has selected a different session.
const reviewHost = { textContent: "", innerHTML: "" };
const staleReviewController = {
  SID: "review-old", CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null), pending: deferred(),
  $: (id) => (id === "rv_body" ? reviewHost : null),
};
staleReviewController.api = () => staleReviewController.pending.promise;
vm.createContext(staleReviewController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("renderReview")}`,
].join("\n"), staleReviewController,
  { filename: "creator-review-session-context-contract.js" });
const staleReview = staleReviewController.renderReview();
staleReviewController.SID = "review-new";
reviewHost.textContent = "NEW SESSION REVIEW";
reviewHost.innerHTML = "NEW SESSION REVIEW";
staleReviewController.pending.resolve({ world: null, player: null, effects_live: {} });
await staleReview;
assert.equal(reviewHost.innerHTML, "NEW SESSION REVIEW",
  "a stale review response must not replace the newly selected session's review");

const sequencedReviewHost = { textContent: "", innerHTML: "" };
const olderReview = deferred(), newerReview = deferred();
const reviewQueue = [olderReview, newerReview];
const sequencedReviewController = {
  SID: "review-same", CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null),
  $: (id) => (id === "rv_body" ? sequencedReviewHost : null),
  api: () => reviewQueue.shift().promise,
  esc: (value) => String(value), chipRow: () => "", reviewProse: () => "", reviewRows: () => "",
};
vm.createContext(sequencedReviewController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("renderReview")}`,
].join("\n"), sequencedReviewController,
  { filename: "creator-review-request-sequence-contract.js" });
const firstReview = sequencedReviewController.renderReview();
const secondReview = sequencedReviewController.renderReview();
newerReview.resolve({ world: { name: "Newest Review", npcs: [] }, player: null, effects_live: {} });
await secondReview;
olderReview.resolve({ world: { name: "Stale Review", npcs: [] }, player: null, effects_live: {} });
await firstReview;
assert.match(sequencedReviewHost.innerHTML, /Newest Review/,
  "an older same-session review response must not replace the newest refresh");
assert.doesNotMatch(sequencedReviewHost.innerHTML, /Stale Review/);

// Same-named committed sessions need a stable Player-facing distinction. A verified portable-card
// receipt contributes only its short revision; legacy and fail-closed receipt rows use the exact
// creation time instead. Neither path forces the Player to compare internal session ids.
const sessionLabelController = {};
vm.createContext(sessionLabelController);
vm.runInContext([
  extractFunction("relTime"), extractFunction("sessionCreatedLabel"),
  extractFunction("sessionLabelPart"), extractFunction("sessOptLabel"),
].join("\n"), sessionLabelController,
{ filename: "creator-session-label-contract.js" });
const commonSessionLabel = {
  world_name: "Emberglass Concord", player_name: "Sera Emberward",
  last_seen: Date.now() / 1000 - 3600, head_turn: -1,
};
const cardLabelA = sessionLabelController.sessOptLabel({
  ...commonSessionLabel, session_id: "internal-card-a", card_revision: "f5cf1c39fa39ad1a",
  session_cue: "zeta2345",
  created_at: 1721582551.123,
}, false);
const cardLabelB = sessionLabelController.sessOptLabel({
  ...commonSessionLabel, session_id: "internal-card-b", card_revision: "4bc9dd472706db8e",
  session_cue: "beta2345",
  created_at: 1721582551.456,
}, false);
assert.notEqual(cardLabelA, cardLabelB, "different validated card revisions must stay distinguishable");
assert.match(cardLabelA, /rev f5cf1c39fa39ad1a · chat zeta2345/);
assert.match(cardLabelB, /rev 4bc9dd472706db8e · chat beta2345/);
assert.doesNotMatch(cardLabelA, /internal-card-a/);

const repeatedCardLabel = sessionLabelController.sessOptLabel({
  ...commonSessionLabel, session_id: "internal-card-repeat",
  card_revision: "f5cf1c39fa39ad1a", session_cue: "gamma234",
  created_at: 1721582551.123,
}, false);
assert.notEqual(cardLabelA, repeatedCardLabel,
  "two chats from the exact same card and timestamp must retain distinct anonymous cues");
assert.match(repeatedCardLabel, /rev f5cf1c39fa39ad1a · chat gamma234/);

const legacyLabelA = sessionLabelController.sessOptLabel({
  ...commonSessionLabel, session_id: "internal-legacy-a", card_revision: "",
  session_cue: "delta234",
  created_at: 1721582551.123,
}, false);
const legacyLabelB = sessionLabelController.sessOptLabel({
  ...commonSessionLabel, session_id: "internal-legacy-b", card_revision: "",
  session_cue: "epsilon2",
  created_at: 1721582551.456,
}, false);
assert.notEqual(legacyLabelA, legacyLabelB, "legacy sessions must use precise stable creation times");
assert.match(legacyLabelA, /created .*31\.123/);
assert.match(legacyLabelB, /created .*31\.456/);
assert.doesNotMatch(legacyLabelA, /internal-legacy-a/);
const unnamedLegacyLabel = sessionLabelController.sessOptLabel({
  external_id: "st-private-looking-id", session_id: "internal-legacy-c",
  card_revision: "", session_cue: "theta234", created_at: 1721582551.789,
  last_seen: 1721582551.789,
  head_turn: 0,
}, false);
assert.match(unnamedLegacyLabel, /^Unnamed session · created /);
assert.doesNotMatch(unnamedLegacyLabel, /st-private-looking-id|internal-legacy-c/);
assert.match(unnamedLegacyLabel, / · chat theta234 · t0/);

const invalidTimeLabel = sessionLabelController.sessOptLabel({
  session_id: "internal-invalid-time", card_revision: null, session_cue: "kappa234",
  created_at: "not-a-time", last_seen: 0, head_turn: 3,
}, false);
assert.match(invalidTimeLabel, /^Unnamed session · created time unavailable · chat kappa234 · t3$/);
assert.doesNotMatch(invalidTimeLabel, /internal-invalid-time/);

const longNameLabel = sessionLabelController.sessOptLabel({
  world_name: "A World Name So Long It Would Previously Consume Every Useful Tail Cue",
  player_name: "A Character Name That Is Also Far Too Long For One Select Option",
  card_revision: "f5cf1c39fa39ad1a", session_cue: "iota2345",
  created_at: 1721582551.123, last_seen: 1721582551.123, head_turn: 12,
}, true);
assert.match(longNameLabel, /rev f5cf1c39fa39ad1a · chat iota2345 · ● newest · t12/,
  "component truncation must preserve revision, chat cue, newest marker, and turn");

// Rapid session selection can leave two specialization requests in flight. Only the latest
// selection may update Creator state when responses arrive out of order.
const selectA = deferred(), selectB = deferred();
const sessionSelect = { value: "session-a", innerHTML: "", onchange: null, insertAdjacentHTML() {} };
const rapidSelector = {
  SID: "session-a", SPEC: "none", CREATOR_SESSION_EPOCH: 0,
  CREATOR_SESSION_REQUESTS: Object.create(null),
  toasts: [], renders: [], history: { replaceState() {} },
  $: (id) => id === "sessSel" ? sessionSelect
    : (id === "review" ? { classList: { contains: () => false } } : null),
  esc: (value) => String(value), sessOptLabel: (row) => row.session_id,
  toast: (message) => rapidSelector.toasts.push(message),
  renderSpec: () => rapidSelector.renders.push(rapidSelector.SPEC), renderReview: () => {},
};
rapidSelector.api = (url) => {
  if (url === "/aether/sessions") return Promise.resolve({
    sessions: [{ session_id: "session-a" }, { session_id: "session-b" }],
  });
  if (url.includes("session-a")) return selectA.promise;
  if (url.includes("session-b")) return selectB.promise;
  throw new Error(`unexpected selector URL ${url}`);
};
vm.createContext(rapidSelector);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("loadSessions")}`,
].join("\n"), rapidSelector,
  { filename: "creator-session-selector-sequence-contract.js" });
await rapidSelector.loadSessions();
sessionSelect.value = "session-a";
const selectingA = sessionSelect.onchange();
sessionSelect.value = "session-b";
const selectingB = sessionSelect.onchange();
selectB.resolve({ specialization: "rpg" });
await selectingB;
selectA.resolve({ specialization: "none" });
await selectingA;
assert.equal(rapidSelector.SID, "session-b");
assert.equal(rapidSelector.SPEC, "rpg",
  "a slower response for an earlier selection must not replace the latest session specialization");
assert.deepEqual(rapidSelector.renders, ["rpg"]);

// Session-backed card generation can install/download a large artifact after a slow response. Do
// not present an old session's card as the result for a newly selected game.
const cardResponse = deferred();
const staleCardController = {
  SID: "card-old", CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null),
  downloads: 0, messages: [], toasts: [],
  worldDoc: () => ({ world_id: "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }), playerDoc: () => ({}),
  api: () => cardResponse.promise,
  setCardMsg: (message) => staleCardController.messages.push(message),
  toast: (message) => staleCardController.toasts.push(message),
  document: {
    body: { appendChild() {} },
    createElement: () => ({
      href: "", download: "", click: () => { staleCardController.downloads += 1; }, remove() {},
    }),
  },
};
vm.createContext(staleCardController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("genNarratorCard")}`,
].join("\n"), staleCardController,
  { filename: "creator-card-session-context-contract.js" });
const staleCard = staleCardController.genNarratorCard(true);
staleCardController.SID = "card-new";
cardResponse.resolve({
  png_b64: "AA==", filename: "Old-Session-Narrator.png", name: "Old Session Narrator",
  world: "Old Session World", seeded_world: true, seeded_player: true, installed: false,
});
await staleCard;
assert.equal(staleCardController.downloads, 0,
  "a card response from the prior session must not auto-download under the new selection");
assert.equal(staleCardController.messages.some((message) => /is ready/i.test(message)), false,
  "a stale session-card response must not be presented as the current card result");

// Form-backed cards are session-independent, but they still share one download/result surface.
// If the Player starts a second build before the first completes, only the newest request may
// download or present a card when those responses arrive out of order.
const olderFormCard = deferred(), newerFormCard = deferred();
const formCardQueue = [olderFormCard, newerFormCard];
const formCardController = {
  SID: "", CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null),
  downloads: [], visibleMessage: "", toasts: [],
  worldDoc: () => ({
    world_id: "world_cccccccccccccccccccccccccccccccc", name: "Form World",
  }),
  playerDoc: () => ({ name: "Form Player" }),
  api: () => formCardQueue.shift().promise,
  setCardMsg: (message) => { formCardController.visibleMessage = message; },
  toast: (message) => formCardController.toasts.push(message),
  document: {
    body: { appendChild() {} },
    createElement: () => {
      const link = { href: "", download: "", remove() {} };
      link.click = () => formCardController.downloads.push(link.download);
      return link;
    },
  },
};
vm.createContext(formCardController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("genNarratorCard")}`,
].join("\n"), formCardController,
  { filename: "creator-card-request-sequence-contract.js" });
const firstFormCard = formCardController.genNarratorCard(false);
const secondFormCard = formCardController.genNarratorCard(false);
newerFormCard.resolve({
  png_b64: "Ag==", filename: "Newer-Form-Narrator.png", name: "Newer Form Narrator",
  world: "Newer Form World", seeded_world: true, seeded_player: true, installed: false,
});
await secondFormCard;
olderFormCard.resolve({
  png_b64: "AQ==", filename: "Older-Form-Narrator.png", name: "Older Form Narrator",
  world: "Older Form World", seeded_world: true, seeded_player: true, installed: false,
});
await firstFormCard;
assert.deepEqual(formCardController.downloads, ["Newer-Form-Narrator.png"],
  "an older form-card response must never download after the newest build finishes");
assert.match(formCardController.visibleMessage, /Newer Form Narrator/,
  "the newest form-card result must remain visible after an older response arrives");
assert.doesNotMatch(formCardController.visibleMessage, /Older Form Narrator/);

// A save response must describe/cache the exact submitted document, not whatever the Player typed
// while the write was in flight. A session switch must leave the new target's cache untouched.
const submittedWorld = { world_id: "world_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", name: "Submitted World" };
const editedWorld = { ...submittedWorld, name: "Edited While Saving" };
const worldSaveReply = deferred();
const worldSaveController = {
  SID: "save-world", WORLD_ID: submittedWorld.world_id, WORLD_CACHE: null,
  CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null),
  worldReads: 0, messages: [],
  worldDoc: () => JSON.parse(JSON.stringify(
    worldSaveController.worldReads++ === 0 ? submittedWorld : editedWorld,
  )),
  api: () => worldSaveReply.promise, sessLabel: () => "Submitted session",
  toast: (message) => worldSaveController.messages.push(message),
};
vm.createContext(worldSaveController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  extractFunction("cloneCreatorValue"), `async ${extractFunction("saveWorld")}`,
].join("\n"), worldSaveController,
  { filename: "creator-world-save-context-contract.js" });
const savingWorld = worldSaveController.saveWorld();
worldSaveReply.resolve({ applied: 1, world_id: submittedWorld.world_id });
await savingWorld;
assert.deepEqual(JSON.parse(JSON.stringify(worldSaveController.WORLD_CACHE)), submittedWorld,
  "World cache must represent the document actually sent, not later form edits");

const submittedPlayer = { name: "Submitted Player", resources: { hp: { cur: 20, max: 20 } } };
const playerSaveReply = deferred();
const playerSaveController = {
  SID: "save-player-old", PLAYER_CACHE: { name: "Prior Cache" }, messages: [],
  CREATOR_SESSION_EPOCH: 0, CREATOR_SESSION_REQUESTS: Object.create(null),
  playerDoc: () => JSON.parse(JSON.stringify(submittedPlayer)), api: () => playerSaveReply.promise,
  sessLabel: () => "Old player session", toast: (message) => playerSaveController.messages.push(message),
  showCreatorValidation: () => {},
};
vm.createContext(playerSaveController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  extractFunction("cloneCreatorValue"), `async ${extractFunction("savePlayer")}`,
].join("\n"), playerSaveController,
  { filename: "creator-player-save-context-contract.js" });
const savingPlayer = playerSaveController.savePlayer();
playerSaveController.SID = "save-player-new";
playerSaveReply.resolve({ applied: 1 });
await savingPlayer;
assert.deepEqual(JSON.parse(JSON.stringify(playerSaveController.PLAYER_CACHE)), { name: "Prior Cache" },
  "a Player save response for the prior session must not become the new target's cache");
assert.equal(playerSaveController.messages.some((message) => /saved to Old player session/i.test(message)), false,
  "a stale save response must not be presented as a save to the newly selected target");

// Boot performs one delayed session read after the selector has become interactive. That startup
// response must not overwrite a session selected by the Player in the meantime.
const bootSessionReply = deferred();
const bootElements = {
  w_genre: { innerHTML: "", addEventListener() {} }, w_time: { innerHTML: "" },
  c_name: { value: "" }, enemy: { addEventListener() {} },
  sessRefresh: {}, newDraft: {}, draftChip: {},
};
const bootController = {
  SID: "boot-old", SPEC: "none", CREATOR_SESSION_EPOCH: 0,
  CREATOR_SESSION_REQUESTS: Object.create(null),
  REG: null, creatorRequested: false, document: {}, $: (id) => bootElements[id] || null,
  addLine() {}, loadModels() {}, loadPresets() {}, async loadSessions() {},
  buildStats() {}, buildSkills() {}, buildAbilities() {}, restoreDraft: () => false,
  genreChanged() {}, renderSpec() {}, refreshLimitCounters() {}, refreshAllListCounts() {},
  wireDraftAutosave() {}, invalidateEnemyPreview() {}, clearCreatorDraft() {}, toast() {},
};
bootController.api = (url) => {
  if (url === "/aether/registry") return Promise.resolve({ genres: ["custom"], times: ["day"] });
  if (url.includes("/creator")) { bootController.creatorRequested = true; return bootSessionReply.promise; }
  throw new Error(`unexpected boot URL ${url}`);
};
vm.createContext(bootController);
vm.runInContext([
  extractFunction("creatorSessionSnapshot"), extractFunction("creatorSessionIsCurrent"),
  `async ${extractFunction("boot")}`,
].join("\n"), bootController,
  { filename: "creator-boot-session-context-contract.js" });
const booting = bootController.boot();
for (let i = 0; i < 10 && !bootController.creatorRequested; i += 1) await Promise.resolve();
assert.equal(bootController.creatorRequested, true);
bootController.SID = "boot-new";
bootController.SPEC = "new-session-spec";
bootSessionReply.resolve({ specialization: "old-session-spec", persona: "Old Persona" });
await booting;
assert.equal(bootController.SPEC, "new-session-spec",
  "a delayed startup read must not replace the newly selected session specialization");
assert.equal(bootElements.c_name.value, "",
  "a delayed startup read must not prefill persona from the previous session");

for (const required of [
  'id="w_setting" maxlength="8000" data-limit="8000"',
  'id="w_notes" maxlength="32768" data-limit="32768"',
  'id="w_scene" maxlength="8000" data-limit="8000"',
  'id="w_quest" maxlength="8000" data-limit="8000"',
  'id="c_appearance" maxlength="4000" data-limit="4000"',
  'id="c_notes" maxlength="32768" data-limit="32768"',
  'id="w_fronts"', 'id="w_loot_standard"', 'id="w_routes"',
  'data-field="governs"', 'data-field="desc"', 'data-field="effect"',
  "Custom world lore", "Faction fronts", "Starting gear",
  "loot:readLoot(), fronts:readFronts(), routes:readRoutes()",
  "fillFronts(w.fronts); fillLoot(w.loot); fillRoutes(w.routes);",
  "ordinaryRows:20,gearRows:32,frontRows:8,routeRows:24",
  "lootRows:12,direction:32768,longProse:8000,rowProse:4000,namedRow:2000",
  'id="w_ai_status" role="status" aria-live="polite"',
  'id="c_ai_status" role="status" aria-live="polite"',
  'id="w_ai_retry"', 'id="c_ai_retry"',
  "return runCreatorAuthoring(\"world\")", "return runCreatorAuthoring(\"player\")",
  "mergeAuthorResult(draft,proposal,current)",
  "canonicalPlayerDocument(playerDoc())",
  'const activeSection=mode==="player"?"char":"world"',
  "WORLD_FORM_EPOCH!==context.worldEpoch",
  "PLAYER_FORM_EPOCH!==context.playerEpoch",
]) assert.ok(html.includes(required), `Creator UI is missing ${required}`);

console.log("creator resource contract smoke: PASS");
