"""Player-facing HUD view — the ONE resolved, presentation-ready projection of the ledger.

AetherState tracks more than a Player may inspect. ``hud_view`` therefore returns only the
Player-visible subset with registry math and typed WorldOverlay effects already resolved.
SillyTavern reads that projection; Console Player/Raw uses its stricter whitelisted
``player_safe_raw`` member. Owner/debug state remains available through separate APIs.

Read-only, fail-open, off the relay: never on the hot path, a `none` session's wire is untouched,
and every section is defensively wrapped — a bad slice yields an empty section, never an
exception. By Bean (AetherState, MIT)."""
from __future__ import annotations

import re
from copy import deepcopy

from .compose import (GEAR_SLOT_ORDER, GEAR_SLOTS, _VALENCE_GLYPH, _initiative_order,
                      affinity_tier, derived_exposure)
from .state import battle_cohort_status, combatant_label

_TIER_LABEL = {
    "crit_fail": "Critical Failure", "fail": "Failure",
    "partial": "Partial (yes, but…)", "success": "Success",
    "crit_success": "Critical Success",
}
_REL_DIMS = ("trust", "affection", "respect", "desire", "tension", "fear", "familiarity")
_PLAYER_PROSE_MAX = 4000
_PLAYER_GOVERNS_MAX = 12
_PLAYER_GOVERNS_ITEM_MAX = 80


def _nm(state: dict, eid: str) -> str:
    e = (state.get("entities") or {}).get(eid) or {}
    return e.get("name") or eid


def _load_reg(cfg):
    try:
        from . import registry as _registry
        return _registry.load(cfg), _registry
    except Exception:
        return None, None


def _mood_label(affect: dict) -> str:
    v = int((affect or {}).get("valence", 0) or 0)
    e = int((affect or {}).get("energy", 0) or 0)
    mood = ("bright" if v >= 45 else "warm" if v >= 15 else "level" if v > -15
            else "low" if v > -45 else "dark")
    energy = ("charged" if e >= 45 else "lively" if e >= 15 else "steady" if e > -15
              else "tired" if e > -45 else "drained")
    return f"{mood} · {energy}"


def _overlay_value(domain: dict, subject_keys: tuple[str, ...], field: str):
    """Read one typed overlay scalar in an explicit, deterministic subject order."""
    for subject_key in subject_keys:
        fields = domain.get(subject_key)
        row = fields.get(field) if isinstance(fields, dict) else None
        if isinstance(row, dict):
            return row.get("value")
    return None


def _scene(state: dict) -> dict:
    sc = state.get("scene") or {}
    clock = state.get("clock") or {}
    present = [_nm(state, eid) for eid, e in (state.get("entities") or {}).items()
               if (e or {}).get("present")]
    try:
        from .world_events import effective_domain, project_state_overlay

        overlay = project_state_overlay(state)
        world_domain = effective_domain(overlay, "world")
        location_domain = effective_domain(overlay, "location")
    except Exception:
        world_domain, location_domain = {}, {}
    world_circumstance = None
    for fields in world_domain.values():
        row = fields.get("circumstance") if isinstance(fields, dict) else None
        if isinstance(row, dict):
            world_circumstance = row.get("value")
    location_id = str(sc.get("location_id") or "")
    location_circumstance = _overlay_value(
        location_domain, (f"location:{location_id}",), "circumstance",
    ) if location_id else None
    return {
        "location": location_id,
        "phase": sc.get("phase") or "",
        "mode": sc.get("mode") or "",
        "time_of_day": clock.get("time_of_day") or "",
        "day": clock.get("day", 1),
        "calendar_note": clock.get("calendar_note") or "",
        "present": sorted(present),
        "world_circumstance": world_circumstance,
        "location_circumstance": location_circumstance,
    }


def _stat_mod(reg, val: int) -> int:
    try:
        return int(reg.stat_mod(val))
    except Exception:
        try:
            return (int(val) - 10) // 2
        except Exception:
            return 0


_RESOURCE_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _resource_pool(p: dict, resource_id: str) -> dict:
    pool = p.get("hp") if str(resource_id) == "hp" else (p.get("resources") or {}).get(resource_id)
    return pool if isinstance(pool, dict) else {}


def _resource_label(p: dict, resource_id: str) -> str:
    rid = str(resource_id)
    name = _resource_pool(p, rid).get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    if rid == "hp":
        return "HP"
    return rid.replace("_", " ").title()


def _resource_color(pool: dict) -> str:
    color = pool.get("color") if isinstance(pool, dict) else None
    return color.lower() if isinstance(color, str) and _RESOURCE_COLOR.fullmatch(color) else ""


def _resource_cost_text(p: dict, cost: dict) -> str:
    bits = []
    for resource_id, amount in (cost or {}).items():
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            bits.append(f"{_resource_label(p, str(resource_id))} {amount}")
    return " + ".join(bits)


def _presentation_text(value, limit: int = _PLAYER_PROSE_MAX) -> str:
    """Bound one Player-owned display string without coercing structured/private values."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _presentation_governs(value) -> list[str]:
    """Keep only the bounded scalar verbs from one frozen skill definition."""
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for raw in value:
        text = _presentation_text(raw, _PLAYER_GOVERNS_ITEM_MAX)
        if text and text not in out:
            out.append(text)
        if len(out) >= _PLAYER_GOVERNS_MAX:
            break
    return out


def _skill_rows(state: dict, reg, _registry, eid: str, p: dict) -> list[dict]:
    skills = p.get("skills") or {}
    mastery = p.get("mastery") or {}
    rows: list[dict] = []
    defs_sk = p.get("defs") if isinstance(p.get("defs"), dict) else {}
    defs_sk = defs_sk.get("skills") if isinstance(defs_sk.get("skills"), dict) else {}
    sk_ids = list(skills) + [k for k in defs_sk if k not in skills]
    for sid in sk_ids:
        label, mod = str(sid), None
        ent = defs_sk.get(sid) if isinstance(defs_sk.get(sid), dict) else {}
        if reg is not None:
            try:
                label = reg.skill_label(sid, p)
                mod = reg.effective_mod(p, sid) + _registry.gear_skill_mod(state, eid, sid)
            except Exception:
                mod = None
        if mod is None:                       # fail-open: stored raw rank
            try:
                mod = int(skills.get(sid, 0))
            except Exception:
                mod = 0
            label = str(sid).replace("_", " ").title()
        try:
            rank = int(skills.get(sid, 0) or 0)
        except Exception:
            rank = 0
        keyed, cost, gated, basis_met, basis_name, group = "", "", False, False, "", ""
        if reg is not None:
            try:
                resolved = reg.skill_entry(sid, p)
                if isinstance(resolved, dict):
                    ent = resolved
                need = str(ent.get("requires_ability") or "")
                if need:                          # a gated skill: does THIS player hold the basis?
                    gated = True
                    basis_met = reg.has_ability(p, need)
                    basis_name = str((reg.merged_abilities(p).get(need) or {}).get("name", need))
                if _registry is not None:
                    cost = _resource_cost_text(p, _registry.skill_cost(ent))
            except Exception:
                pass
        keyed = _presentation_text(ent.get("keyed_stat"), 80)
        group = _presentation_text(ent.get("group"), 120)
        desc = _presentation_text(ent.get("desc"))
        governs = _presentation_governs(ent.get("governs"))
        bracket = ""
        try:
            from .state import mastery_bracket
            bracket = mastery_bracket(mastery.get(str(sid), 0))[0]
        except Exception:
            bracket = ""
        rows.append({"id": str(sid), "label": label, "mod": int(mod), "rank": rank,
                     "keyed_stat": keyed, "bracket": bracket, "cost": cost, "gated": gated,
                     "basis_met": basis_met, "basis_name": basis_name, "group": group,
                     "desc": desc, "governs": governs,
                     "mastery": int(mastery.get(str(sid), 0) or 0)})
    return rows


def _ability_rows(reg, _registry, p: dict, turn: int) -> list[dict]:
    """Abilities resolved for the player, with each one's MECHANIC spelled out (2026-07-07):
    a skill sets your modifier; an ability shapes the dice. Passives auto-apply; actives are
    invoked in a check with `use <id>` and cost a resource / cool down. Group tags let the HUD
    split talents · techniques · spells."""
    abil = p.get("abilities") or []
    merged = {}
    if reg is not None:
        try:
            merged = reg.merged_abilities(p)
        except Exception:
            merged = {}
    cd = p.get("ability_cd") or {}
    rows = []
    for a in abil:
        d = merged.get(str(a)) or {}
        mech, label, mag, cost = "mod", "", 0, {}
        if _registry is not None:
            try:
                mech = _registry.ability_mechanic(d)
                label = _registry.ABILITY_MECHANIC_LABEL.get(mech, "")
                mag = int(_registry.ability_magnitude(d, 0))
                cost = _registry.skill_cost(d)
            except Exception:
                pass
        active = mech in ("extra_die", "reroll", "surge")
        if _registry is not None:
            try:                                    # authored kind is the truth (2026-07-09:
                active = _registry.ability_is_active(d)   # custom actives were reading as
            except Exception:                       # inert passives off the mechanic alone)
                pass
        kind = str(d.get("kind", "")) or ("active" if active else "passive")
        at = d.get("applies_to", "all")
        applies_id = ""                                  # first SPECIFIC governing skill slug (for
        if isinstance(at, (list, tuple)):                # the Rolls-tab one-tap invoke button)
            applies = ", ".join((reg.skill_label(str(x), p) if reg else str(x)) for x in at)
            for x in at:
                if str(x).strip().lower() not in ("", "all", "any"):
                    applies_id = str(x)
                    break
        elif isinstance(at, str) and at.strip().lower() not in ("", "all", "any"):
            applies = reg.skill_label(at, p) if reg else at
            applies_id = at
        else:
            applies = "all checks"
        cool = int(d.get("cooldown_turns", 0) or 0)
        ready = int(cd.get(str(a), 0) or 0)
        rows.append({
            "id": str(a), "name": str(d.get("name", a)),
            "kind": kind, "active": active, "mechanic": mech, "mechanic_label": label,
            "group": str(d.get("group", "")) or ("spell" if mech == "basis" else "talent"),
            "applies_to": applies, "applies_id": applies_id, "magnitude": mag,
            "cost": _resource_cost_text(p, cost),
            "cooldown": cool, "on_cd": max(0, ready - (int(turn) + 1)) if cool else 0,
            "desc": _presentation_text(d.get("desc")),
            "effect": _presentation_text(d.get("effect")),
        })
    # group order: spells, then techniques (actives), then talents (passives)
    rank = {"spell": 0, "technique": 1, "talent": 2}
    rows.sort(key=lambda r: (rank.get(r["group"], 3), not r["active"], r["name"]))
    return rows


def _capability_overlay_rows(state: dict, rows: list[dict]) -> list[dict]:
    """Attach current typed eligibility without changing the underlying capability ledger."""
    try:
        from .world_events import capability_eligible
    except Exception:
        capability_eligible = None
    out = []
    for raw in rows:
        row = dict(raw)
        cid = str(row.get("id") or "")
        try:
            row["eligible"] = bool(capability_eligible(state, {
                "id": cid,
                "capability_id": cid,
                "name": str(row.get("label") or row.get("name") or cid),
            })) if capability_eligible is not None else True
        except Exception:
            row["eligible"] = True
        out.append(row)
    return out


def _mods_str(mods: dict) -> str:
    bits = []
    for k in sorted(mods or {}):
        mv = mods[k]
        bits.append(f"{k}{mv:+d}" if isinstance(mv, int) else f"{k} {mv}")
    return " ".join(bits)


def _effect_rows(state: dict, _registry, eid: str, turn: int) -> list[dict]:
    out = []
    for key, rec in ((state.get("effects") or {}).get(eid) or {}).items():
        if not isinstance(rec, dict):
            continue
        if _registry is not None:
            try:
                if not _registry.effect_active(rec, turn):
                    continue
            except Exception:
                pass
        remaining = None
        if rec.get("duration") is not None:
            try:
                remaining = max(0, int(rec.get("gained_turn", 0)) + int(rec["duration"]) - turn)
            except Exception:
                remaining = None
        name = str(rec.get("name", rec.get("id", "?")))
        kind = str(rec.get("kind", "condition"))
        # a condition named/flavored as an illness reads as a "disease" for the human
        low = name.lower()
        if kind == "condition" and any(w in low for w in
                                       ("disease", "diseased", "sick", "ill", "plague",
                                        "infection", "fever", "pox", "rot")):
            kind_label = "Disease"
        else:
            kind_label = kind.capitalize()
        out.append({"key": str(key), "name": name,
                    "kind": kind, "kind_label": kind_label,
                    "valence": str(rec.get("valence", "neutral")),
                    "glyph": _VALENCE_GLYPH.get(rec.get("valence"), "~"),
                    "stacks": int(rec.get("stacks", 1) or 1),
                    "remaining": remaining,
                    "mods": _mods_str(rec.get("mods") or {}),
                    "note": str(rec.get("note", ""))})
    return out


def _gear_rows(state: dict, eid: str) -> list[dict]:
    items = state.get("items") or {}
    slots = (state.get("gear") or {}).get(eid) or {}
    order = [s for s in GEAR_SLOT_ORDER if s in slots] + \
            [s for s in sorted(slots) if s not in GEAR_SLOTS]
    rows = []
    for slot in order:
        iid = slots.get(slot)
        it = items.get(iid) if iid else None
        if not it:
            continue
        rows.append({"slot": str(slot), "iid": str(iid), "name": str(it.get("name", iid)),
                     "mods": _mods_str(it.get("mods_snapshot") or {}),
                     "aura": _presentation_text(it.get("aura")),
                     "capacity": it.get("capacity")})
    return rows


# Paper-doll layout (2026-07-07): every equip position, so worn vs. empty reads at a glance and
# the player always knows WHERE a piece of gear goes. `kind` drives the visual grouping.
_SLOT_LABEL = {
    "head": "Head", "face": "Face", "neck": "Neck", "shoulders": "Shoulders", "body": "Body",
    "cape": "Cape", "arms": "Arms", "hands": "Hands", "mainhand": "Main Hand",
    "offhand": "Off Hand", "waist": "Waist", "legs": "Legs", "feet": "Feet", "back": "Back",
    "accessory1": "Trinket I", "accessory2": "Trinket II"}
_SLOT_KIND = {"mainhand": "weapon", "offhand": "weapon",
              "accessory1": "trinket", "accessory2": "trinket", "neck": "trinket"}


def _gear_slots(state: dict, eid: str) -> list[dict]:
    """The full equip layout for the paper-doll: EVERY slot (worn or empty), in render order,
    each tagged weapon | trinket | armor so worn gear is unmistakable from carried inventory."""
    items = state.get("items") or {}
    slots = (state.get("gear") or {}).get(eid) or {}
    order = list(GEAR_SLOT_ORDER) + [s for s in sorted(slots) if s not in GEAR_SLOTS]
    seen, out = set(), []
    for slot in order:
        if slot in seen:
            continue
        seen.add(slot)
        iid = slots.get(slot)
        it = items.get(iid) if iid else None
        row = {"slot": str(slot),
               "label": _SLOT_LABEL.get(slot, str(slot).replace("_", " ").title()),
               "kind": _SLOT_KIND.get(slot, "armor"), "item": None}
        if it:
            row["item"] = {"iid": str(iid), "name": str(it.get("name", iid)),
                           "mods": _mods_str(it.get("mods_snapshot") or {}),
                           "aura": _presentation_text(it.get("aura")),
                           "type": str(it.get("type", "")), "capacity": it.get("capacity")}
        out.append(row)
    return out


def _rules_view(reg, _registry, cfg) -> dict:
    """The dice rules, made VISIBLE to the player (Bean: 'dice rules MUST be visible too').
    A short, model-agnostic explainer of the resolution system + how abilities shape it."""
    dice, tiers, keep = "2d6", "pbta3", 2
    mechs = []
    if reg is not None and _registry is not None:
        try:
            dice = _registry.dice_spec(reg, cfg)
            tiers = _registry.tiers_model(reg, cfg)
            pd = _registry.parse_dice(dice)
            keep = pd[0] if pd else 2
            mechs = [{"mechanic": m, "label": _registry.ABILITY_MECHANIC_LABEL[m]}
                     for m in ("edge", "ward", "extra_die", "reroll", "surge")]
        except Exception:
            pass
    return {
        "dice": dice, "keep": keep, "tiers": tiers,
        "thresholds": [
            {"range": "10 +", "tier": "Success", "desc": "you do it, cleanly"},
            {"range": "7 – 9", "tier": "Partial", "desc": "yes, but at a cost or complication"},
            {"range": "6 or less", "tier": "Miss", "desc": "it goes wrong; the world pushes back"},
        ],
        "crits": "All dice minimum → Critical Failure · all dice maximum → Critical Success",
        "check_syntax": "((aether.check <skill> [+N] [vs DC] "
                        "[scope minor|major|epic|mythic] [use <ability>]))",
        "note": "Code rolls and decides; the story narrates the result it was handed. "
                "A skill sets your modifier; an ability bends the dice.",
        "mechanics": mechs,
    }


def _carried_rows(state: dict, eid: str, want_gear: bool) -> list[dict]:
    """Carried instances grouped by container, filtered by CLASS: want_gear=True yields the
    GEAR-class items (weapons/tools/bags not equipped — 'stowed gear'), want_gear=False the
    INVENTORY-class items (consumables/materials/devices). Bean 2026-07-07: gear ≠ inventory."""
    from .state import item_is_gear
    items = state.get("items") or {}
    conts = (state.get("inventory") or {}).get(eid) or {}
    out = []
    for cid in sorted(conts, key=lambda c: (c == "loose", str(c))):
        entries = []
        for iid in conts[cid]:
            it = items.get(iid)
            if not it or int(it.get("qty", 1)) < 1 or item_is_gear(it) != want_gear:
                continue
            entries.append({"iid": str(iid), "name": str(it.get("name", iid)),
                            "qty": int(it.get("qty", 1)),
                            "type": str(it.get("type") or ""),
                            "consumable": bool(it.get("on_consume") or it.get("consumable")),
                            "slot": str(it.get("slot") or ""),
                            "aura": _presentation_text(it.get("aura"))})
        if not entries:
            continue
        cname = "carried" if cid == "loose" else str((items.get(cid) or {}).get("name", cid))
        out.append({"container": cname, "items": entries})
    return out


def _inventory_rows(state: dict, eid: str) -> list[dict]:
    return _carried_rows(state, eid, want_gear=False)


def _stowed_gear_rows(state: dict, eid: str) -> list[dict]:
    return _carried_rows(state, eid, want_gear=True)


def _drives(state: dict, eid: str) -> dict:
    ch = (state.get("chars") or {}).get(eid) or {}
    obs = []
    for o in (ch.get("obsessions") or {}).values():
        if isinstance(o, dict):
            obs.append({"target": str(o.get("target", "")),
                        "target_kind": str(o.get("target_kind", "concept")),
                        "intensity": int(o.get("intensity", 0)),
                        "flavor": str(o.get("flavor", ""))})
    crav = []
    for sub, c in (ch.get("cravings") or {}).items():
        if not isinstance(c, dict):
            continue
        seed = c.get("_seed", {}) if isinstance(c.get("_seed"), dict) else {}
        wd = (c.get("level", 0) >= seed.get("withdrawal_level", 70)
              and c.get("dependency", 0) >= seed.get("withdrawal_dependency", 50))
        crav.append({"substance": str(sub), "level": int(c.get("level", 0)),
                     "dependency": int(c.get("dependency", 0)), "withdrawal": bool(wd)})
    goals = [str(g.get("text") if isinstance(g, dict) else g)
             for g in (ch.get("goals") or []) if g]
    return {"obsessions": obs, "cravings": crav, "goals": [g for g in goals if g]}


def _worn_exposed(state: dict, eid: str) -> tuple[list, list]:
    worn = []
    for item, g in (state.get("clothing", {}).get(eid) or {}).items():
        if isinstance(g, dict) and g.get("state") in ("worn", "opened", "displaced"):
            worn.append(item if g.get("state") == "worn" else f"{item} ({g['state']})")
    try:
        exposed = list(derived_exposure(state, eid))
    except Exception:
        exposed = []
    return worn, exposed


def _rel_to(state: dict, a: str, b: str) -> list[dict]:
    rec = (state.get("relationships") or {}).get(f"{a}->{b}") or {}
    dims = (rec.get("dims") or {}) if isinstance(rec, dict) else {}
    top = sorted(((d, int(v)) for d, v in dims.items() if abs(int(v)) >= 15),
                 key=lambda kv: -abs(kv[1]))[:5]
    return [{"dim": d, "val": v} for d, v in top]


def _player_rows(state: dict, cfg, reg, _registry, turn: int) -> list[dict]:
    players = state.get("player") or {}
    attrs = state.get("attributes") or {}
    out = []
    for eid, p in players.items():
        if not isinstance(p, dict):
            continue
        a = attrs.get(eid) or {}
        stats = []
        for k, v in (p.get("stats") or {}).items():
            stats.append({"key": str(k), "val": int(v) if isinstance(v, int) else v,
                          "mod": _stat_mod(reg, v)})
        hp = p.get("hp") or {}
        resources = {}
        for rn, r in (p.get("resources") or {}).items():
            if isinstance(r, dict) and r.get("max"):
                row = {"cur": r.get("cur", r["max"]), "max": r["max"],
                       "name": _resource_label(p, str(rn))}
                color = _resource_color(r)
                if color:
                    row["color"] = color
                resources[str(rn)] = row
        ch = (state.get("chars") or {}).get(eid) or {}
        hp_view = None
        if hp.get("max"):
            hp_view = {"cur": hp.get("cur", hp.get("max")), "max": hp.get("max"),
                       "name": _resource_label(p, "hp")}
            color = _resource_color(hp)
            if color:
                hp_view["color"] = color
        out.append({
            "eid": eid,
            "name": _nm(state, eid),
            "appearance": str(a.get("appearance") or a.get("description") or ""),
            "concept": str(p.get("concept") or a.get("class") or ""),
            "species": str(p.get("species") or a.get("species") or ""),
            "sex": str(p.get("sex") or a.get("sex") or ""),
            "pronouns": str(p.get("pronouns") or ""),
            "level": int(p.get("level", 1) or 1),
            "xp": int(p.get("xp", 0) or 0),
            "stat_points": int(p.get("stat_points", 0) or 0),
            "mood": _mood_label(ch.get("affect") or {}) if ch else "",
            "hp": hp_view,
            "resources": resources,
            "stats": stats,
            "skills": _capability_overlay_rows(
                state, _skill_rows(state, reg, _registry, eid, p)
            ),
            "abilities": _capability_overlay_rows(
                state, _ability_rows(reg, _registry, p, turn)
            ),
            "effects": _effect_rows(state, _registry, eid, turn),
            "gear": _gear_rows(state, eid),
            "gear_slots": _gear_slots(state, eid),
            "stowed_gear": _stowed_gear_rows(state, eid),
            "inventory": _inventory_rows(state, eid),
            "drives": _drives(state, eid),
        })
    return out


def _cast_rows(state: dict, _registry, turn: int, player_eids: set) -> list[dict]:
    """Player-observable cast state; private NPC drives, goals, and arousal stay internal."""
    ents = state.get("entities") or {}
    chars = state.get("chars") or {}
    aff = state.get("affinity") or {}
    peid = next(iter(player_eids), None)
    try:
        from .world_events import effective_domain, project_state_overlay

        actor_overlay = effective_domain(project_state_overlay(state), "actor")
    except Exception:
        actor_overlay = {}
    out = []
    for eid, e in ents.items():
        if eid in player_eids or (e or {}).get("kind") in ("faction", "location"):
            continue
        ch = chars.get(eid) or {}
        effs = _effect_rows(state, _registry, eid, turn)
        worn, exposed = _worn_exposed(state, eid)
        rec = aff.get(f"{peid}->{eid}") if peid else None
        present = bool((e or {}).get("present"))
        entity_kind = str((e or {}).get("kind") or "actor")
        typed_kind = entity_kind if entity_kind in {"npc", "enemy"} else "actor"
        subject_keys = (f"{typed_kind}:{eid}",)
        if typed_kind != "actor":
            subject_keys += (f"actor:{eid}",)
        overlay_condition = _overlay_value(actor_overlay, subject_keys, "condition")
        has_data = bool(effs or worn or exposed or isinstance(rec, dict)
                        or overlay_condition is not None)
        if not present and not has_data:
            continue                          # untracked bystander — skip to keep it scannable
        out.append({
            "eid": eid, "name": _nm(state, eid),
            "kind": entity_kind,
            "present": present,
            "location": (e or {}).get("location_id") or "",
            "mood": _mood_label(ch.get("affect") or {}) if ch.get("affect") else "",
            "effects": effs,
            "worn": worn, "exposed": exposed,
            "rel_tier": affinity_tier(rec.get("value", 0)) if isinstance(rec, dict) else "",
            "rel_dims": _rel_to(state, peid, eid) if peid else [],
            "world_condition": overlay_condition,
        })
    # present first, then those with the most tracked detail
    out.sort(key=lambda c: (not c["present"], -len(c["effects"]) - len(c["rel_dims"])))
    return out


def _relationship_overlay_id(a: str, b: str) -> str:
    """Closed typed id; state uses ``a->b`` but ``>`` is invalid in v2 subject refs."""
    return f"{a}:{b}"


def _relationships(state: dict, player_eids: set[str] | None = None) -> list[dict]:
    try:
        from .world_events import effective_domain, project_state_overlay

        overlay = effective_domain(project_state_overlay(state), "relationship")
    except Exception:
        overlay = {}
    out = []
    for key, rec in (state.get("relationships") or {}).items():
        if not isinstance(rec, dict) or "->" not in key:
            continue
        a, b = key.split("->", 1)
        if player_eids and a not in player_eids and b not in player_eids:
            continue
        dims = _rel_to(state, a, b)
        overlay_id = _relationship_overlay_id(a, b)
        modifier = _overlay_value(overlay, (f"relationship:{overlay_id}",), "modifier")
        if dims or modifier is not None:
            out.append({"a": _nm(state, a), "b": _nm(state, b), "dims": dims,
                        "a_id": a, "b_id": b, "id": overlay_id,
                        "world_modifier": modifier})
    return out[:12]


def _front_rows(state: dict, turn: int) -> list[dict]:
    """Phase 2: REVEALED faction clocks only — rumor-gating applies to the HUD/briefing
    (ratified); the Console reads state_summary and always sees every front. The
    consequence text stays hidden until the clock actually fills (no spoilers)."""
    from .world_events import front_identity_visible_to_player

    rows = []
    for fid, f in sorted((state.get("fronts") or {}).items()):
        if not isinstance(f, dict) or not f.get("revealed"):
            continue
        if f.get("done") and not front_identity_visible_to_player(state, str(fid)):
            continue
        rows.append({
            "id": fid, "name": str(f.get("name", fid)),
            "faction": str(f.get("faction") or "").replace("_", " "),
            "filled": int(f.get("filled", 0)), "segments": int(f.get("segments", 6)),
            "done": bool(f.get("done")),
            "consequence": str(f.get("consequence", "")) if f.get("done") else "",
            "fresh": bool(f.get("done"))
            and turn - int(f.get("filled_turn", -10**9)) <= 1,
        })
    rows.sort(key=lambda r: (not r["fresh"], r["done"], r["name"]))
    return rows


def _quests(state: dict) -> list[dict]:
    try:
        from .world_events import effective_domain, project_state_overlay

        overlay = effective_domain(project_state_overlay(state), "quest")
    except Exception:
        overlay = {}
    out = []
    for qid, q in (state.get("quests") or {}).items():
        if not isinstance(q, dict):
            continue
        name = str(q.get("name", qid))
        availability = _overlay_value(overlay, (f"quest:{qid}",), "available")
        out.append({"id": str(qid), "name": name, "status": str(q.get("status", "active")),
                    "stakes": str(q.get("stakes", "")), "note": str(q.get("note", "")),
                    "available": availability is not False})
    return out


def _roll_target(state: dict, target: object) -> tuple[str | None, str | None]:
    """Return one exact combat row plus its visible label, including defeated rows.

    Stored roll targets are code-authored visible labels.  Retaining defeated rows here is
    deliberate: the HUD must not forget which cohort member received a lethal committed hit.
    """
    if not isinstance(target, str) or not (label := target.strip()):
        return None, None
    combat = state.get("combat")
    rows = (combat.get("combatants") or {}) if isinstance(combat, dict) else {}
    if not isinstance(rows, dict):
        return None, label
    if label in rows and isinstance(rows[label], dict):
        return label, combatant_label(rows[label], label)
    low = label.casefold()
    matches = [
        (str(cid), combatant_label(row, str(cid)))
        for cid, row in rows.items()
        if isinstance(row, dict) and combatant_label(row, str(cid)).casefold() == low
    ]
    return matches[0] if len(matches) == 1 else (None, label)


def _unresolved_roll_target_label(target: object) -> str | None:
    """Retain bounded display text without treating malformed target shapes as authority."""
    if isinstance(target, str):
        return target.strip() or None
    if isinstance(target, dict):
        for key in ("label", "name", "id", "target", "target_id"):
            value = target.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _roll_impact(state: dict, row: dict) -> dict:
    target = row.get("target")
    if "target" not in row or target is None \
            or isinstance(target, str) and not target.strip():
        return {"kind": "none", "target_id": None, "target_label": None,
                "damage": None, "text": "No target impact"}
    target_id, target_label = _roll_target(state, target)
    if target_id is None:
        target_label = _unresolved_roll_target_label(target)
        text = f"{target_label}: impact unavailable" if target_label else "Impact unavailable"
        return {"kind": "unknown", "target_id": None, "target_label": target_label,
                "damage": None, "text": text}
    damage = row.get("dmg")
    if isinstance(damage, bool) or not isinstance(damage, int) or damage < 0:
        return {"kind": "unknown", "target_id": target_id, "target_label": target_label,
                "damage": None, "text": f"{target_label}: impact unavailable"}
    if damage == 0:
        return {"kind": "miss", "target_id": target_id, "target_label": target_label,
                "damage": 0, "text": f"{target_label}: no damage"}
    return {"kind": "damage", "target_id": target_id, "target_label": target_label,
            "damage": damage, "text": f"{target_label}: {damage} damage"}


def _rolls(state: dict) -> list[dict]:
    out = []
    rows = state.get("rolls")
    if not isinstance(rows, list):
        return out
    for r in rows[-14:]:
        if not isinstance(r, dict):
            continue
        tier = r.get("tier")
        skill = str(r.get("skill", ""))
        spec = str(r.get("spec") or r.get("dice") or "")
        raw_label = skill or spec or "Roll"
        label = raw_label.replace("_", " ").title() if skill else raw_label
        sh = r.get("shape") if isinstance(r.get("shape"), dict) else None
        note = ""
        if sh:                                # 2026-07-07: how an ability bent this roll
            if sh.get("fired"):
                pool = ",".join(str(x) for x in (sh.get("pool") or []))
                note = f"{sh['fired']} — extra die ({pool})" if pool else str(sh["fired"])
            elif sh.get("surge"):
                note = "surge"
            elif sh.get("edge"):
                note = "advantage"
            elif sh.get("ward"):
                note = "guard"
        out.append({"turn": r.get("turn"), "spec": spec,
                    "result": r.get("result"), "skill": skill,
                    "kind": "check" if skill else "roll", "label": label,
                    "mod": r.get("mod"),
                    "tier": str(tier) if tier else "",
                    "tier_label": _TIER_LABEL.get(str(tier), "") if tier else "",
                    # Kept for old stored rolls; current narration cannot call checks.
                    "dm_called": bool(r.get("dm_called")),
                    "note": note, "impact": _roll_impact(state, r)})
    return out


def _memories(state: dict) -> list[dict]:
    from .memory import player_safe_memory_text

    out = []
    for m in (state.get("memories") or [])[-10:]:
        if isinstance(m, dict) and m.get("text"):
            text = player_safe_memory_text(m["text"], state)
            if text:
                out.append({"turn": m.get("turn"), "text": text})
    return list(reversed(out))


def _consent(state: dict, player_eids: set[str] | None = None) -> list[dict]:
    out = []
    for key, c in (state.get("consent") or {}).items():
        if not isinstance(c, dict) or key.count("|") < 2:
            continue
        a, b, cat = key.split("|", 2)
        if player_eids and a not in player_eids and b not in player_eids:
            continue
        out.append({"pair": f"{_nm(state, a)} ↔ {_nm(state, b)}", "category": cat,
                    "a_id": a, "b_id": b,
                    "level": str(c.get("level", "unknown")), "cap": c.get("max_intensity")})
    return out


def _knowledge(state: dict) -> dict:
    """One Player-safe projection for claims, epistemics, facts, and world changes."""
    from .knowledge import select_knowledge

    player_ids = list((state.get("player") or {}).keys())
    # The shared HUD has no authenticated viewer identity. One Player is unambiguous; with
    # zero or multiple Player records actor-scoped knowledge must fail closed.
    actor_id = player_ids[0] if len(player_ids) == 1 else None
    selected = select_knowledge(
        state,
        audience="player",
        actor_id=actor_id,
        query=str(((state.get("scene") or {}).get("location_id") or "")),
        limit=48,
        include_history=True,
    )
    claims = [{
        **row,
        "proposition": row.get("statement", ""),
        "class": row.get("claim_class"),
        "polarity": row.get("proposition_polarity"),
    } for row in selected["claims"]]
    epistemics = [dict(row) for row in selected["epistemics"]]
    facts = [dict(row) for row in selected["facts"]]
    events = [{
        **row,
        "what_happened": row.get("statement", ""),
        "cause": row.get("cause") if row.get("cause_visible") else "cause not known",
    } for row in selected["events"]]
    return {"claims": claims, "epistemics": epistemics, "facts": facts, "events": events}


def _social(state: dict) -> tuple[list, list, dict]:
    players = state.get("player") or {}
    peid = next(iter(players), None)
    aff = state.get("affinity") or {}
    ents = state.get("entities") or {}
    try:
        from .world_events import project_state_overlay
        effective = (project_state_overlay(state) or {}).get("effective") or {}
    except Exception:
        effective = {}
    relations = []
    for eid, e in ents.items():
        if (e or {}).get("kind") in ("faction", "location") or eid in players:
            continue
        rec = aff.get(f"{peid}->{eid}") if peid else None
        reputation = _overlay_value(
            effective.get("reputation") or {},
            (f"reputation:{eid}", f"actor:{eid}"),
            "modifier",
        )
        if not isinstance(rec, dict) and reputation is None:
            continue
        relations.append({"id": str(eid), "name": _nm(state, eid),
                          "tier": affinity_tier((rec or {}).get("value", 0)),
                          "present": bool((e or {}).get("present")),
                          "reputation_modifier": reputation})
    factions = []
    facs = state.get("factions") or {}
    fids = [fid for fid, e in ents.items() if (e or {}).get("kind") == "faction"]
    fids += [fid for fid in facs if fid not in fids]
    for fid in fids:
        rec = aff.get(f"{peid}->{fid}") if peid else None
        overlay_row = ((effective.get("faction") or {}).get(f"faction:{fid}") or {}).get("circumstance")
        world_circumstance = overlay_row.get("value") if isinstance(overlay_row, dict) else None
        reputation = _overlay_value(
            effective.get("reputation") or {},
            (f"reputation:{fid}", f"faction:{fid}"),
            "modifier",
        )
        if not isinstance(rec, dict) and world_circumstance is None and reputation is None:
            continue
        factions.append({"id": str(fid), "name": _nm(state, fid),
                         "tier": affinity_tier((rec or {}).get("value", 0)),
                         "world_circumstance": world_circumstance,
                         "reputation_modifier": reputation})
    world = {}
    for subject in (effective.get("world") or {}).values():
        row = (subject or {}).get("circumstance") if isinstance(subject, dict) else None
        if isinstance(row, dict):
            world["world_circumstance"] = row.get("value")
    return relations, factions, world


def _war_room(state: dict, cfg=None) -> dict:
    """Phase 1 (plan doc 13): the combat lane payload — EXACT HP (Bean: decided, pillar-17
    rawness), each combatant's side/tier/armament, the pre-rolled dice (enemy opposition +
    per-ally dice, VISIBLE — ratified), fresh loot drops, and the last settled fight.
    Rendered from committed rows + the same deterministic dice code the directive uses."""
    from .compose import _ally_die, _die_tier
    from .enemy_kits import intent_matches_frozen_kit
    spec = getattr(cfg, "specialization", None) if cfg is not None else None
    cb = state.get("combat") or {}
    turn = (state.get("meta") or {}).get("turn", -1)
    enabled = bool(spec is not None and spec.name == "rpg"
                   and getattr(spec, "war_room", True))
    active = bool(enabled and cb.get("active"))
    out: dict = {"active": active, "round": 0, "combatants": [], "last": None,
                 "intent": None, "opposition": None,
                 "player_impacts": [],
                 "clashes": [c for c in (state.get("clashes") or [])[-5:]]}
    hist = cb.get("history") or []
    if hist:
        out["last"] = hist[-1]
    peid = next(iter(state.get("player") or {}), "")
    player = (state.get("player") or {}).get(peid) or {}
    opp = player.get("_opposition_last") if isinstance(player, dict) else None
    committed_opp = isinstance(opp, dict) and opp.get("turn") == turn
    enemy_rolls = bool(getattr(spec, "enemy_rolls", True))
    if enabled:
        out["player_impacts"] = [
            row for row in _rolls(state)
            if row.get("kind") == "check" and row.get("turn") == turn
        ]
    if enabled and enemy_rolls and committed_opp:
        visible_opp = dict(opp)
        actor_row = ((cb.get("combatants") or {}).get(str(opp.get("actor") or "")))
        if isinstance(actor_row, dict):
            visible_opp["actor_name"] = combatant_label(
                actor_row, str(opp.get("actor_name") or opp.get("actor") or "Enemy"))
        current_hp = player.get("hp") if isinstance(player, dict) else None
        if isinstance(current_hp, dict):
            try:
                current_hp_cur = int(current_hp.get("cur", current_hp.get("max")))
                current_hp_max = int(current_hp.get("max"))
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if current_hp_max > 0:
                    visible_opp["current_hp_cur"] = current_hp_cur
                    visible_opp["current_hp_max"] = current_hp_max
        out["opposition"] = visible_opp
    if not active:
        return out
    out["round"] = max(1, turn - int(cb.get("started_turn") or turn) + 1)
    intent = cb.get("pending_intent")
    irow = ((cb.get("combatants") or {}).get(str((intent or {}).get("actor")))
            if isinstance(intent, dict) else None)
    if enemy_rolls and isinstance(irow, dict) and irow.get("side") == "enemy" \
            and not irow.get("defeated") \
            and intent_matches_frozen_kit(intent, irow) \
            and intent.get("target") == peid \
            and intent.get("target_name") == str(
                ((state.get("entities") or {}).get(peid) or {}).get("name") or peid) \
            and intent.get("prepared_turn") == turn:
        out["intent"] = {**intent, "actor_name": combatant_label(
            irow, str(intent.get("actor_name") or intent.get("actor") or "Enemy"))}
    for cid, r in (cb.get("combatants") or {}).items():
        if not isinstance(r, dict):
            continue
        hp = r.get("hp") or {}
        row = {"cid": str(cid), "name": combatant_label(r, str(cid)),
               "base_name": str(r.get("name", cid)),
               "side": str(r.get("side", "enemy")), "kind": str(r.get("kind", "extra")),
               "tier": str(r.get("tier", "standard")),
               "hp": {"cur": int(hp.get("cur", 0)), "max": int(hp.get("max", 1))},
               "armament": str(r.get("armament", "")),
               "init": int(r.get("init", 10)),          # baked turn-order score (2026-07-10)
               "defeated": bool(r.get("defeated")),
               "dropped": list(r.get("dropped") or [])}
        if isinstance(r.get("cohort"), dict):
            row["cohort"] = dict(r["cohort"])
        if not row["defeated"]:
            if row["side"] != "enemy":
                t, d = _ally_die(state, str(cid))
                row["die"] = {"total": t, "tier": _die_tier(t), "dmg": d}
        out["combatants"].append(row)
    out["combatants"].sort(key=lambda r: (r["side"] != "enemy", r["defeated"], r["name"]))
    out["order"] = [{"name": nm, "side": sd, "init": sc}          # explicit initiative order
                    for sc, nm, sd in _initiative_order(state, cfg)]   # (2026-07-10, Bean)
    b = state.get("battle") or {}
    if b.get("active"):                                          # §F: the macro-battle chip
        from .state import battle_tide
        out["battle"] = {"name": str(b.get("name", "")),
                          "tide": battle_tide(b.get("momentum", 0)),
                          "waves": int(b.get("waves", 0))}
        cohort = battle_cohort_status(state)
        if cohort is not None:
            out["battle"]["cohort"] = cohort
    return out


_VISIBILITY_SECTIONS = frozenset({
    "scene", "players", "skills", "abilities", "capabilities", "cast", "actors",
    "quests", "rolls", "relationships", "relations", "factions", "world",
    "knowledge", "claims", "epistemics", "facts", "events", "consent", "rules",
    "war_room", "fronts", "clock",
})
_VISIBILITY_PREFIXES = frozenset({
    "player", "actor", "npc", "enemy", "capability", "quest", "relationship",
    "reputation", "faction", "claim", "epistemic", "fact", "event", "front",
    "combatant",
})


def _surface_visibility(overlay: dict, surface: str) -> dict[str, bool]:
    """Return only closed Player-surface selectors; unknown target ids are inert."""
    domain = ((overlay.get("effective") or {}).get(surface) or {})
    out: dict[str, bool] = {}
    for fields in domain.values():
        row = fields.get("visible") if isinstance(fields, dict) else None
        subject = row.get("subject") if isinstance(row, dict) else None
        value = row.get("value") if isinstance(row, dict) else None
        if not isinstance(subject, dict) or subject.get("kind") != surface \
                or not isinstance(value, bool):
            continue
        target = str(subject.get("id") or "")
        prefix = target.split(":", 1)[0]
        if target in _VISIBILITY_SECTIONS or prefix in _VISIBILITY_PREFIXES:
            out[target] = value
    return out


def _hidden(decisions: dict[str, bool], *targets: str) -> bool:
    return any(decisions.get(target) is False for target in targets if target)


def _knowledge_row_target(kind: str, row: dict) -> str:
    rid = row.get("id") or row.get("fingerprint") or row.get("claim_id") \
        or row.get("belief_id") or row.get("fact_id") or row.get("event_id")
    return f"{kind}:{rid}" if rid else ""


def _apply_surface_visibility(
    view: dict, decisions: dict[str, bool], state: dict,
) -> dict:
    """Apply HUD/Console visibility to a detached Player projection, never ledger state."""
    out = deepcopy(view)
    entities = state.get("entities") or {}

    if _hidden(decisions, "scene"):
        out["scene"] = {}
    elif isinstance(out.get("scene"), dict):
        hidden_names = {
            str((entity or {}).get("name") or eid)
            for eid, entity in entities.items()
            if _hidden(
                decisions, f"actor:{eid}", f"npc:{eid}", f"enemy:{eid}",
                f"player:{eid}",
            )
        }
        out["scene"]["present"] = [
            name for name in out["scene"].get("present") or [] if name not in hidden_names
        ]

    players = [] if _hidden(decisions, "players") else list(out.get("players") or [])
    filtered_players = []
    for player in players:
        eid = str(player.get("eid") or "")
        if _hidden(decisions, f"player:{eid}", f"actor:{eid}"):
            continue
        if _hidden(decisions, "capabilities", "skills"):
            player["skills"] = []
        else:
            player["skills"] = [
                row for row in player.get("skills") or []
                if not _hidden(decisions, f"capability:{row.get('id') or ''}")
            ]
        if _hidden(decisions, "capabilities", "abilities"):
            player["abilities"] = []
        else:
            player["abilities"] = [
                row for row in player.get("abilities") or []
                if not _hidden(decisions, f"capability:{row.get('id') or ''}")
            ]
        filtered_players.append(player)
    out["players"] = filtered_players

    if _hidden(decisions, "cast", "actors"):
        out["cast"] = []
    else:
        out["cast"] = [
            row for row in out.get("cast") or []
            if not _hidden(
                decisions, f"actor:{row.get('eid') or ''}",
                f"{row.get('kind') or 'actor'}:{row.get('eid') or ''}",
            )
        ]
    out["quests"] = [] if _hidden(decisions, "quests") else [
        row for row in out.get("quests") or []
        if not _hidden(decisions, f"quest:{row.get('id') or ''}")
    ]
    if _hidden(decisions, "rolls"):
        out["rolls"] = []
    out["relationships"] = [] if _hidden(decisions, "relationships") else [
        row for row in out.get("relationships") or []
        if not _hidden(decisions, f"relationship:{row.get('id') or ''}")
    ]
    out["relations"] = [] if _hidden(decisions, "relations") else [
        row for row in out.get("relations") or []
        if not _hidden(
            decisions, f"actor:{row.get('id') or ''}", f"reputation:{row.get('id') or ''}",
        )
    ]
    out["factions"] = [] if _hidden(decisions, "factions") else [
        row for row in out.get("factions") or []
        if not _hidden(
            decisions, f"faction:{row.get('id') or ''}", f"reputation:{row.get('id') or ''}",
        )
    ]
    if _hidden(decisions, "world"):
        out["world_flags"] = {}

    knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
    if _hidden(decisions, "knowledge"):
        out["knowledge"] = {"claims": [], "epistemics": [], "facts": [], "events": []}
    else:
        for section, kind in (
            ("claims", "claim"), ("epistemics", "epistemic"),
            ("facts", "fact"), ("events", "event"),
        ):
            knowledge[section] = [] if _hidden(decisions, section) else [
                row for row in knowledge.get(section) or []
                if not _hidden(decisions, _knowledge_row_target(kind, row))
            ]
        out["knowledge"] = knowledge

    if _hidden(decisions, "consent"):
        out["consent"] = []
    if _hidden(decisions, "rules"):
        out["rules"] = {}
    if _hidden(decisions, "fronts"):
        out["fronts"] = []
    else:
        out["fronts"] = [
            row for row in out.get("fronts") or []
            if not _hidden(decisions, f"front:{row.get('id') or ''}")
        ]
    if _hidden(decisions, "clock"):
        out["clock"] = {}
    if _hidden(decisions, "war_room"):
        out["war_room"] = {"active": False, "combatants": [], "player_impacts": []}
    elif isinstance(out.get("war_room"), dict):
        room = out["war_room"]
        room["combatants"] = [
            row for row in room.get("combatants") or []
            if not _hidden(
                decisions, f"combatant:{row.get('cid') or ''}",
                f"actor:{row.get('cid') or ''}", f"enemy:{row.get('cid') or ''}",
            )
        ]
        for field in ("intent", "opposition"):
            row = room.get(field)
            if isinstance(row, dict) and _hidden(
                decisions, f"actor:{row.get('actor') or ''}", f"enemy:{row.get('actor') or ''}",
            ):
                room[field] = None
    return out


_PLAYER_RAW_KEYS = (
    "spec", "frozen", "intent_floor", "frozen_reason", "turn", "scene", "players",
    "cast", "quests", "rolls", "relationships", "relations", "factions", "knowledge",
    "consent", "rules", "war_room", "fronts", "clock",
)
_SAFE_CAST_KEYS = (
    "eid", "name", "kind", "present", "location", "mood", "effects", "worn", "exposed",
    "rel_tier", "rel_dims", "world_condition",
)


def _player_safe_raw(view: dict, decisions: dict[str, bool], state: dict) -> dict:
    """Strictly whitelisted Console Raw payload; no state, journal, memory, or private NPC prose."""
    safe = {key: deepcopy(view.get(key)) for key in _PLAYER_RAW_KEYS if key in view}
    safe["schema"] = "aetherstate-player-inspection/1"
    safe["cast"] = [
        {key: deepcopy(row.get(key)) for key in _SAFE_CAST_KEYS if key in row}
        for row in safe.get("cast") or []
    ]
    safe["factions"] = [{
        key: deepcopy(row.get(key))
        for key in ("id", "name", "tier", "world_circumstance", "reputation_modifier")
        if key in row
    } for row in safe.get("factions") or []]
    return _apply_surface_visibility(safe, decisions, state)


def hud_view(state: dict, cfg=None) -> dict:
    """Resolved Player projection with typed overlays and fail-closed private-state boundaries."""
    state = dict(state) if isinstance(state, dict) else {}
    if not isinstance(state.get("meta"), dict):
        state["meta"] = {}
    if not isinstance(state.get("player"), dict):
        state["player"] = {}
    spec = getattr(getattr(cfg, "specialization", None), "name", "none")
    turn = (state.get("meta") or {}).get("turn", -1)
    if isinstance(turn, bool) or not isinstance(turn, int):
        turn = -1
    reg, _registry = _load_reg(cfg)
    player_eids = set((state.get("player") or {}).keys())
    out: dict = {
        "spec": spec, "frozen": bool(state.get("frozen")),
        "intent_floor": bool(getattr(getattr(cfg, "specialization", None), "intent_floor", True)),
        "frozen_reason": state.get("frozen_reason"), "turn": turn,
        "scene": {}, "players": [], "cast": [], "quests": [], "rolls": [],
        "relationships": [], "relations": [], "factions": [], "world_flags": {},
        "memories": [], "knowledge": {"claims": [], "epistemics": [], "facts": [], "events": []},
        "consent": [], "rules": {},
        "war_room": {"active": False, "combatants": [], "player_impacts": [],
                     "intent": None, "opposition": None},
        "fronts": [], "clock": {},
    }
    for key, fn in (
        ("scene", lambda: _scene(state)),
        ("war_room", lambda: _war_room(state, cfg)),
        ("rules", lambda: _rules_view(reg, _registry, cfg)),
        ("players", lambda: _player_rows(state, cfg, reg, _registry, turn)),
        ("cast", lambda: _cast_rows(state, _registry, turn, player_eids)),
        ("quests", lambda: _quests(state)),
        ("rolls", lambda: _rolls(state)),
        ("relationships", lambda: _relationships(state, player_eids)),
        ("knowledge", lambda: _knowledge(state)),
        ("consent", lambda: _consent(state, player_eids)),
        ("fronts", lambda: _front_rows(state, turn)),
        ("clock", lambda: dict(state.get("clock") or {})),
    ):
        try:
            out[key] = fn()
        except Exception:
            pass
    try:
        out["relations"], out["factions"], out["world_flags"] = _social(state)
    except Exception:
        pass
    try:
        from .world_events import project_state_overlay

        overlay = project_state_overlay(state)
    except Exception:
        overlay = {}
    hud_visibility = _surface_visibility(overlay, "hud")
    console_visibility = _surface_visibility(overlay, "console")
    out = _apply_surface_visibility(out, hud_visibility, state)
    out["player_safe_raw"] = _player_safe_raw(out, console_visibility, state)
    out["surface_visibility"] = {
        "hud": hud_visibility,
        "console": console_visibility,
    }
    return out
