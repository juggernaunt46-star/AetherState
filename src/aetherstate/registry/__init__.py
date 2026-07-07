"""Curated stat / skill / ability registry + deterministic resolution (doc 06 §3, 05 §5.2).

The frozen-before-rolled core of RPG mode: stats, skills and abilities have EXPLICIT definitions
(keyed stat, modifier, rank ceiling, bounded ability effects). Authoring is NOT banned — freestyle
ideas are allowed and land as a FROZEN registry snapshot first (the Q27 assist-authored -> snapshot
loop, doc 09 §1; RPG-1 ships hand-authored TOML + reject-on-unknown, the loop lands later). At
*resolution* nothing is freestyle: the model may *reference* a registry id and *propose* a check;
code maps the proposal to a REGISTERED skill or rejects it (unknown ids never mint a mechanic at
roll time — Bean's constraint, as amended by the snapshot directive).

Loading is disk I/O, so it is done ONCE and cached (keyed by override dir): after the first
call every lookup is a dict read — hot-path-legal (invariant 2). Shipped defaults live beside
this file; a user may override/extend via `<data_dir>/registry/*.toml` (merged like beat
libraries — user keys win per-table).

Replay purity (invariant 2): the registry is read at *resolve/apply* time only. The effective
modifier and the rolled result are baked into the journaled `check` op (state._enrich / the
Tier-0 emit), never re-read at replay — editing the registry later never rewrites history.
"""
from __future__ import annotations

import functools
import logging
import math
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..state import CHECK_TIERS as CHECK_TIERS  # re-export: state owns the op vocab (single source)
from ..state import GEAR_SLOTS, mastery_bracket

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # py310
    import tomli as tomllib  # type: ignore[no-redef]

log = logging.getLogger("aetherstate.registry")

_PKG_DIR = Path(__file__).resolve().parent
_DICE_RE = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)


def parse_dice(spec: str) -> Optional[tuple[int, int, int]]:
    """'2d6' / '3d6+1' -> (n, sides, flat_mod). None if unparseable or out of sane bounds."""
    m = _DICE_RE.match(str(spec or ""))
    if not m:
        return None
    n = int(m.group(1) or 1)
    sides = int(m.group(2))
    flat = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if not (1 <= n <= 20 and 2 <= sides <= 1000):
        return None
    return n, sides, flat


def roll_dice(spec: str, rng: random.Random) -> Optional[tuple[list[int], int, int]]:
    """Roll `spec` -> (naturals, sides, flat_mod). Real RNG; naturals are kept for audit."""
    parsed = parse_dice(spec)
    if parsed is None:
        return None
    n, sides, flat = parsed
    return [rng.randint(1, sides) for _ in range(n)], sides, flat


def roll_keep(n_keep: int, extra: int, sides: int, rng: random.Random) -> tuple[list[int], list[int]]:
    """Roll (n_keep + max(0, extra)) dice of `sides`; return (kept_best_n, full_pool). The
    generalized ADVANTAGE primitive (RPG abilities, 2026-07-07): 2d6 +1 extra = roll 3d6 keep
    the best 2; 1d20 +1 extra = roll 2d20 keep best 1 (classic advantage). Pure given `rng` —
    the pool + the kept subset are baked into the check op, so replay never re-rolls."""
    n_keep = max(1, int(n_keep))
    total = n_keep + max(0, int(extra))
    pool = [rng.randint(1, int(sides)) for _ in range(total)]
    kept = sorted(pool, reverse=True)[:n_keep]
    return kept, pool


# ---- Ability mechanics (2026-07-07 redesign, Bean): a skill sets the MODIFIER; an ability
# SHAPES THE DICE. `mechanic` is curated + frozen; these normalizers keep resolution replay-safe
# and let a legacy `passive_mod`-only ability keep working (it normalizes to "mod").
ABILITY_MECHANICS = {"mod", "edge", "ward", "extra_die", "reroll", "surge", "basis"}
ABILITY_MECHANIC_LABEL = {
    "mod": "flat bonus", "edge": "advantage — roll an extra die, keep the best",
    "ward": "guard — raises your failure floor (no critical fumble)",
    "extra_die": "second chance — on a failed roll, roll an extra die and keep the best",
    "reroll": "reroll — on a failed roll, roll again and take the better",
    "surge": "surge — a big bonus that also lifts the outcome ceiling for one check",
    "basis": "grants the in-world basis for a gated skill",
}
ACTIVE_MECHANICS = {"extra_die", "reroll", "surge"}     # require `use`; cost + cooldown
ON_FAIL_MECHANICS = {"extra_die", "reroll"}             # fire (and are paid) only on a miss


def ability_mechanic(entry: dict) -> str:
    """Normalized mechanic for an ability def (registry or frozen). A def with only a legacy
    `passive_mod`/`resolution_mod` reads as "mod"; a bare passive marker reads as "basis"."""
    if not isinstance(entry, dict):
        return "mod"
    m = str(entry.get("mechanic", "")).strip().lower()
    if m in ABILITY_MECHANICS:
        return m
    if entry.get("passive_mod") or entry.get("resolution_mod"):
        return "mod"
    return "basis" if str(entry.get("kind", "")).lower() == "passive" else "mod"


def ability_applies(entry: dict, skill_id: str) -> bool:
    """Does this ability shape a check on `skill_id`? `applies_to` = "all" | id | [ids]."""
    a = (entry or {}).get("applies_to", "all")
    if isinstance(a, str):
        a = a.strip().lower()
        return a in ("", "all", "any") or a == str(skill_id).lower()
    if isinstance(a, (list, tuple, set)):
        return str(skill_id).lower() in {str(x).lower() for x in a}
    return True


def ability_magnitude(entry: dict, default: int = 1) -> int:
    """Extra dice (edge/extra_die), floor steps (ward), or flat bonus (surge/mod)."""
    for k in ("magnitude", "resolution_mod", "dice"):
        v = (entry or {}).get(k)
        if isinstance(v, int):
            return v
    pm = (entry or {}).get("passive_mod")
    if isinstance(pm, dict) and isinstance(pm.get("amount"), int):
        return int(pm["amount"])
    return default


# PbtA + optional crits — the single tier-resolution function (tier0 + tests share it).
# CHECK_TIERS is imported from state (op-vocab home); resolve_tier only emits its members.


def resolve_tier(naturals: list[int], mod: int, sides: int,
                 dc: Optional[int] = None, tiers: str = "pbta3") -> tuple[str, int]:
    """Pure, total: (tier, total). PbtA thresholds (10+/7-9) unless an explicit DC is given,
    then success at >=DC, partial within 3 below. All-min naturals fumble; all-max crit
    (regardless of `tiers` string — a 3-tier table simply treats crits as strong success/fail
    when it renders). Deterministic from its inputs -> replay reproduces the tier."""
    total = sum(naturals) + int(mod)
    n = len(naturals)
    nat = sum(naturals)
    if n and nat == n * 1:
        return "crit_fail", total
    if n and nat == n * sides:
        return "crit_success", total
    hi, mid = (int(dc), int(dc) - 3) if dc is not None else (10, 7)
    if total >= hi:
        return "success", total
    if total >= mid:
        return "partial", total
    return "fail", total


@dataclass
class Registry:
    version: str = "rpg-registry/1"
    mod_policy: str = "dnd5e"
    dice: str = "2d6"
    tiers: str = "pbta3"
    stats: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    abilities: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)      # RPG-2: item TEMPLATES (doc 06 §3.5)
    effects: dict = field(default_factory=dict)    # RPG-3: Status/Condition PRESETS (effects.toml)
    extra_slots: list = field(default_factory=list)

    @property
    def slots(self) -> set:
        """Gear-slot vocabulary: the built-in default set + per-table meta.toml `extra_slots`
        (doc 07 O5). Membership is baked into item_equip at _enrich, never read at replay."""
        return GEAR_SLOTS | {str(s) for s in self.extra_slots}

    # -- lookups -----------------------------------------------------------------
    def stat_mod(self, value) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 0
        if self.mod_policy == "direct":
            return v
        return math.floor((v - 10) / 2)           # dnd5e default

    # -- per-character snapshot overlay (Q27 assist-authored -> snapshot, doc 09 §1) -----
    # A player may own FROZEN per-character definitions under player["defs"]["skills"|
    # "abilities"|"stats"] — a mastery-evolved variant or a freestyle-authored unique. These
    # WIN over the global registry for that id, and an id present ONLY here still resolves —
    # so an evolving / bespoke mechanic has a home WITHOUT mutating the shared, process-wide,
    # lru-cached registry (one player's invention never leaks into another's). The overlay is
    # a per-call dict merge (µs, hot-path-legal) and only allocates when defs are present.
    # Resolution stays replay-pure: the effective mod is baked into the `check` op at emit,
    # never re-read at replay — editing a snapshot later never rewrites history.
    @staticmethod
    def _pdefs(player: Optional[dict], table: str) -> dict:
        d = (player or {}).get("defs") or {}
        t = d.get(table)
        return t if isinstance(t, dict) else {}

    def merged_skills(self, player: Optional[dict] = None) -> dict:
        pd = self._pdefs(player, "skills")
        return {**self.skills, **pd} if pd else self.skills

    def merged_abilities(self, player: Optional[dict] = None) -> dict:
        pd = self._pdefs(player, "abilities")
        return {**self.abilities, **pd} if pd else self.abilities

    def known_abilities(self, player: Optional[dict] = None) -> dict:
        """aid -> def for the abilities THIS player actually knows (merged snapshot ⊕ registry,
        matched by id or display name). The set the dice-shaping resolver iterates."""
        known = {str(a).lower() for a in (player or {}).get("abilities", [])}
        out: dict = {}
        for aid, a in self.merged_abilities(player).items():
            a = a or {}
            if str(aid).lower() in known or str(a.get("name", aid)).lower() in known:
                out[aid] = a
        return out

    def skill_entry(self, skill_id: str, player: Optional[dict] = None) -> dict:
        """The definition that governs `skill_id` for THIS player: snapshot first, else global."""
        pd = self._pdefs(player, "skills")
        if skill_id in pd:
            return pd[skill_id] or {}
        return self.skills.get(skill_id) or {}

    def resolve_skill(self, token: str, player: Optional[dict] = None) -> Optional[str]:
        """id | display name | governs-verb -> skill id (or None: unknown => rejected).
        A per-character snapshot skill (player["defs"]["skills"]) resolves too — global first,
        then the player's own frozen definitions (Q27 overlay)."""
        t = str(token or "").strip().lower()
        if not t:
            return None
        skills = self.merged_skills(player)
        if t in skills:
            return t
        for sid, s in skills.items():
            if str((s or {}).get("name", "")).lower() == t:
                return sid
        for sid, s in skills.items():
            if t in [str(g).lower() for g in (s or {}).get("governs", [])]:
                return sid
        return None

    def resolve_effect(self, token: str) -> Optional[str]:
        """id | display name -> effect PRESET id, or None (= open vocabulary, no preset).
        Unlike skills, an unknown effect is NOT rejected — RPG-3's floor/ceiling split
        (doc 05 §5.4): presets carry engine-side mechanics; open names still ground in the
        ledger, they just carry none."""
        t = str(token or "").strip().lower()
        if not t:
            return None
        if t in self.effects:
            return t
        t2 = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
        if t2 in self.effects:
            return t2
        for fid, e in self.effects.items():
            if str((e or {}).get("name", "")).lower() == t:
                return fid
        return None

    def has_ability(self, player: Optional[dict], ability_id: str) -> bool:
        """Does THIS character know `ability_id`? The eligibility gate's basis test
        (doc 10 / RPG-3): checks the card's abilities list and the per-character frozen
        defs, matching id or display name (snapshot-first, same as resolution)."""
        if not player:
            return False
        want = str(ability_id or "").strip().lower()
        if not want:
            return False
        known = {str(a).lower() for a in (player.get("abilities") or [])}
        merged = self.merged_abilities(player)
        if want in known or want in {str(k).lower() for k in self._pdefs(player, "abilities")}:
            return True
        entry = merged.get(want) or {}
        name = str(entry.get("name", "")).lower()
        return bool(name) and name in known

    def passive_mod(self, player: dict, skill_id: str) -> int:
        """Sum of passive-ability bonuses naming `skill_id` for abilities the player knows.
        Reads the merged ability set so a per-character snapshot ability counts too."""
        total = 0
        known = set(str(a).lower() for a in (player or {}).get("abilities", []))
        for aid, a in self.merged_abilities(player).items():
            a = a or {}
            if a.get("kind") != "passive":
                continue
            name = str(a.get("name", aid)).lower()
            if aid not in known and name not in known:
                continue
            pm = a.get("passive_mod") or {}
            if str(pm.get("skill", "")).lower() == skill_id:
                total += int(pm.get("amount", 0))
        return total

    def effective_mod(self, player: dict, skill_id: str) -> int:
        """stat_mod(keyed_stat) + base_mod + rank + passive-ability mods (doc 06 §2.2)
        + the mastery bracket bonus (RPG-5, doc 10 §4 — the curated evolution floor).
        The skill definition is resolved snapshot-first (per-character freeze wins)."""
        s = self.skill_entry(skill_id, player)
        keyed = str(s.get("keyed_stat", "")).upper()
        stat_val = (player or {}).get("stats", {}).get(keyed)
        mod = self.stat_mod(stat_val) if stat_val is not None else 0
        mod += int(s.get("base_mod", 0))
        mod += int((player or {}).get("skills", {}).get(skill_id, 0))
        mod += self.passive_mod(player, skill_id)
        mod += mastery_bracket(((player or {}).get("mastery") or {}).get(skill_id, 0))[1]
        return mod

    def skill_label(self, skill_id: str, player: Optional[dict] = None) -> str:
        return str(self.skill_entry(skill_id, player).get("name", skill_id))

    def subject_terms(self, skill_id: str, player: Optional[dict] = None) -> list[str]:
        """Terms that bind a narrated result to this check (linter outcome_match subject)."""
        s = self.skill_entry(skill_id, player)
        out = [skill_id, str(s.get("name", skill_id))]
        out += [str(g) for g in s.get("governs", [])]
        return [t for t in out if len(t) >= 3]


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                      # fail-open: a bad file never blocks startup
        log.warning("registry: skipping %s (%s)", path.name, type(exc).__name__)
        return {}


def _merge_table(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        base[k] = v                               # user table entry wins wholesale (like beats)
    return base


@functools.lru_cache(maxsize=8)
def _load(override_dir: str) -> Registry:
    meta = _read_toml(_PKG_DIR / "meta.toml").get("meta", {})
    if not meta:                                  # meta.toml has no [meta] wrapper -> flat
        meta = _read_toml(_PKG_DIR / "meta.toml")
    stats = _read_toml(_PKG_DIR / "stats.toml")
    skills = _read_toml(_PKG_DIR / "skills.toml")
    abilities = _read_toml(_PKG_DIR / "abilities.toml")
    items = _read_toml(_PKG_DIR / "items.toml")
    effects = _read_toml(_PKG_DIR / "effects.toml")
    if override_dir:
        od = Path(override_dir)
        if (od / "meta.toml").is_file():
            meta = {**meta, **(_read_toml(od / "meta.toml").get("meta")
                               or _read_toml(od / "meta.toml"))}
        for name, tbl in (("stats.toml", stats), ("skills.toml", skills),
                          ("abilities.toml", abilities), ("items.toml", items),
                          ("effects.toml", effects)):
            if (od / name).is_file():
                _merge_table(tbl, _read_toml(od / name))
    return Registry(
        version=str(meta.get("version", "rpg-registry/1")),
        mod_policy=str(meta.get("mod_policy", "dnd5e")),
        dice=str(meta.get("dice", "2d6")),
        tiers=str(meta.get("tiers", "pbta3")),
        stats=stats, skills=skills, abilities=abilities, items=items, effects=effects,
        extra_slots=list(meta.get("extra_slots", []) or []))


def load(cfg=None) -> Registry:
    """Cached registry. `cfg` supplies the optional <data_dir>/registry override dir."""
    override = ""
    try:
        if cfg is not None:
            override = str(Path(cfg.server.data_dir) / "registry")
    except Exception:
        override = ""
    return _load(override)


def skill_cost(entry: dict) -> dict:
    """Normalized resource cost {resource: amount>0} from a skill/ability def (RPG-5,
    doc 10 §5.4). Costs are declared in the registry / frozen defs — never by the model
    mid-roll. Empty dict = free (worlds without pools play exactly as before)."""
    c = (entry or {}).get("cost")
    out: dict[str, int] = {}
    if isinstance(c, dict):
        for k, v in c.items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                out[str(k).lower()] = iv
    return out


def gear_skill_mod(state: dict, eid: str, skill_id: str) -> int:
    """Sum of EQUIPPED-gear mods naming `skill_id` for `eid` (doc 06 §2.2: gear mods are part
    of the effective check mod). Reads only the baked per-instance `mods_snapshot` — pure state,
    replay-safe, µs (editing a template never changes an already-minted item's contribution)."""
    total = 0
    for it in (state.get("items") or {}).values():
        if not isinstance(it, dict):
            continue
        if it.get("owner") != eid or not str(it.get("loc", "")).startswith("gear:"):
            continue
        mv = (it.get("mods_snapshot") or {}).get(skill_id)
        if isinstance(mv, int):
            total += mv
    return total


def effect_active(rec: dict, turn: int) -> bool:
    """Is a ledger effect record live at `turn`? Expiry is DERIVED (gained_turn + duration),
    never mutated — a pure function of state, so replay needs no expiry ops (RPG-3)."""
    if not isinstance(rec, dict):
        return False
    dur = rec.get("duration")
    if dur is None:
        return True
    try:
        return int(turn) < int(rec.get("gained_turn", 0)) + int(dur)
    except (TypeError, ValueError):
        return True


def effect_skill_mod(state: dict, eid: str, skill_id: str, turn: int) -> int:
    """Sum of ACTIVE ledger-effect mods naming `skill_id` (or "all") for `eid` (RPG-3).
    Reads only the baked per-record `mods` — pure state, replay-safe, µs (editing
    effects.toml never changes an effect already committed to the ledger)."""
    total = 0
    for rec in ((state.get("effects") or {}).get(eid) or {}).values():
        if not isinstance(rec, dict) or not effect_active(rec, turn):
            continue
        mods = rec.get("mods") or {}
        for key in (skill_id, "all"):
            mv = mods.get(key)
            if isinstance(mv, int):
                total += mv
    return total


def dice_spec(reg: Registry, cfg=None) -> str:
    """Profile knob wins over the registry default (D1)."""
    try:
        if cfg is not None and getattr(cfg, "specialization", None) is not None \
                and cfg.specialization.name == "rpg" and cfg.specialization.dice:
            return cfg.specialization.dice
    except Exception:
        pass
    return reg.dice


def tiers_model(reg: Registry, cfg=None) -> str:
    try:
        if cfg is not None and getattr(cfg, "specialization", None) is not None \
                and cfg.specialization.name == "rpg" and cfg.specialization.tiers:
            return cfg.specialization.tiers
    except Exception:
        pass
    return reg.tiers
