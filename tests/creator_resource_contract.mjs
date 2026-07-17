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
  ];
  return {
    dataset: { def: JSON.stringify(def) },
    querySelectorAll() { return inputs; },
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
  resourceIdFor(label) { return labels.get(String(label).trim().toLowerCase()) || ""; },
  resourceLabel(id) { return id === "focus" ? "Ash Focus" : id[0].toUpperCase() + id.slice(1); },
};
sandbox.$ = (id) => ({
  querySelectorAll: () => id === "c_cskills" ? sandbox.customSkillRows : sandbox.resourceRows,
});
vm.createContext(sandbox);
vm.runInContext([
  "customSkillId", "creatorResourceError", "clearCreatorFieldError", "markCreatorFieldError",
  "resourceCostLimits", "readCustomResources", "parseResourceCost", "readCostInput", "costStr",
  "readCustomSkills",
].map(extractFunction).join("\n"), sandbox, { filename: "creator-resource-contract.js" });

sandbox.customSkillRows = [customSkillRow({
  id: "rope_dart", name: "Rope-Dart", keyed_stat: "DEX", max_rank: 3, _rank: 3,
}, 4)];
let [skill] = JSON.parse(JSON.stringify(sandbox.readCustomSkills()));
assert.equal(skill.max_rank, 4, "a visible rank edit must raise a stale hidden AI ceiling");
assert.equal(skill._rank, undefined, "the transient UI rank must not leak into the frozen definition");

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

console.log("creator resource contract smoke: PASS");
