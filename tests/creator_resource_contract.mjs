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

function field(value) {
  return {
    value,
    dataset: {},
    attrs: {},
    removeAttribute(name) { delete this.attrs[name]; },
    setAttribute(name, next) { this.attrs[name] = next; },
    setCustomValidity(message) { this.validationMessage = message; },
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
]) assert.ok(html.includes(required), `Creator UI is missing ${required}`);

console.log("creator resource contract smoke: PASS");
