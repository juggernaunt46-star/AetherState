"""Director: rules-based beat engine + condition DSL + pacing (03 SS8/8.1, 02 SS9) — cold path.

Evaluates shipped/authored beat libraries against the post-apply snapshot, applies the
winning beat's effects through the SAME op path as extraction (source="rule", 02 SS9.2),
and stages next turn's director note: "[Direction] {beat_note} {corrective}" (04 SS3.3).
The linter's top corrective APPENDS here; L9 never steers this slot (guard channel, Q12).

Determinism (Q4): pure DSL over a read-only snapshot; sorted binding enumeration; winner
by (priority, consent_headroom, beat_id). Unknown DSL path -> condition is FALSE + an
authoring warning in the log (03 SS8.1 — a typo'd beat can never fire).

Fail-open everywhere (invariants 1-3): a broken beat library, a bad template, or an
effects rejection never blocks the note, the turn, or the stream. Frozen sessions run
aftercare_checkin EXCLUSIVELY (02 SS6); flashback/dream scenes get no steering (08 B4).
The user's character is never bound to initiator/actor slots (Q12 spirit).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .linter import _SEV_RANK
from .state import CONSENT_RANK, apply_delta

log = logging.getLogger("aetherstate.director")

BEATS_DIR = Path(__file__).parent / "beats"
PHASE_TARGETS = {"setup": 20, "rising": 55, "climax": 85, "resolution": 30}  # 02 SS9.2
PACING_BAND = 20
PACING_COOLDOWN = 6
_TOKEN = re.compile(r"\{([a-z_]+)\}")
_BEAT_FIELDS = {"beat_id", "name", "preconditions", "note_template"}
_warned_libs: set = set()
_lib_cache: dict = {}


# ---------------------------------------------------------------- library loading
def load_libraries(names: list[str]) -> list[dict]:
    """Package-data beat libraries; unknown name -> warn once, skip (fail-open)."""
    beats: list[dict] = []
    for name in names:
        if name in _lib_cache:
            beats.extend(_lib_cache[name])
            continue
        path = BEATS_DIR / f"{name}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            good = []
            for b in raw:
                if not isinstance(b, dict) or not _BEAT_FIELDS <= set(b):
                    raise ValueError(f"malformed beat in {name}")
                b.setdefault("library", name)
                b.setdefault("binds", "none")
                b.setdefault("effects", [])
                b.setdefault("priority", 50)
                b.setdefault("cooldown_turns", 6)
                b.setdefault("once_per_scene", False)
                b.setdefault("phase_hint", None)
                good.append(b)
            _lib_cache[name] = good
            beats.extend(good)
        except Exception as exc:
            if name not in _warned_libs:
                _warned_libs.add(name)
                log.warning("beat library %r unavailable: %s", name, type(exc).__name__)
    return beats


# ---------------------------------------------------------------- condition DSL (03 SS8.1)
def resolve_path(state: dict, path: str):
    """-> (found, value). Consent paths default to unknown/None (02 SS6 absence);
    rel dims default 0; anything else missing -> (False, None) -> condition False."""
    try:
        if path.startswith("scene."):
            key = path[6:]
            sc = state.get("scene", {})
            if key == "location":
                key = "location_id"
            if key in ("tension", "intimacy", "stagnation", "scene_index"):
                return True, sc.get(key, 0)
            return (key in sc), sc.get(key)
        if path.startswith("session."):
            key = path[8:]
            if key == "frozen":
                return True, bool(state.get("frozen"))
            if key == "turn":
                return True, state.get("meta", {}).get("turn", -1)
            return False, None
        if path.startswith("clock."):
            c = state.get("clock", {})
            return (path[6:] in c), c.get(path[6:])
        if path.startswith("rel."):
            m = re.match(r"rel\.([^.]+)->([^.]+)\.(\w+)$", path)
            if not m:
                return False, None
            rel = state.get("relationships", {}).get(f"{m.group(1)}->{m.group(2)}", {})
            return True, rel.get("dims", {}).get(m.group(3), 0)
        if path.startswith("consent."):
            m = re.match(r"consent\.([^.]+)->([^.]+)\.(\w+)\.(level|max_intensity)$", path)
            if not m:
                return False, None
            a, b, cat, field = m.groups()
            entry = (state.get("consent", {}).get(f"{a}|{b}|{cat}")
                     or state.get("consent", {}).get(f"{b}|{a}|{cat}")
                     or {"level": "unknown", "max_intensity": None})
            return True, entry.get(field)
        if path.startswith("char."):
            parts = path.split(".")
            eid, rest = parts[1], parts[2:]
            ch = state.get("chars", {}).get(eid)
            if rest and rest[0] == "present":
                return True, eid in _present(state)
            if rest and rest[0] == "attr":
                attrs = state.get("attributes", {}).get(eid, {})
                key = ".".join(rest[1:])
                return (key in attrs), attrs.get(key)
            if ch is None:
                return False, None
            if not rest:
                return True, ch
            head = rest[0]
            if head == "arousal":
                a = ch.get("arousal", {})
                if len(rest) == 1:
                    return True, a.get("arousal", 0)
                return (rest[1] in a), a.get(rest[1])
            if head == "affect":
                a = ch.get("affect", {})
                if len(rest) < 2:
                    return False, None
                return (rest[1] in a), a.get(rest[1])
            if head in ("goals", "secrets", "status_effects"):
                return True, ch.get(head, [])
            if head == "craving":
                c = ch.get("cravings", {}).get(rest[1])
                if c is None or len(rest) < 3:
                    return False, None
                return (rest[2] in c), c.get(rest[2])
            if head == "obsession":
                if len(rest) != 3:               # obsession.<kind>:<slug>.<field>
                    return False, None
                o = ch.get("obsessions", {}).get(rest[1])
                if o is None:
                    return False, None
                return (rest[2] in o), o.get(rest[2])
            return False, None
        if path.startswith("quest."):            # RPG-5: quest.active_count | quest.<qid>.<f>
            qs = state.get("quests", {}) or {}   # (.stale_turns = turns since last update)
            key = path[6:]
            if key == "active_count":
                return True, sum(1 for q in qs.values()
                                 if isinstance(q, dict) and q.get("status") == "active")
            qid, _, fieldname = key.partition(".")
            q = qs.get(qid)
            if not isinstance(q, dict) or not fieldname:
                return False, None
            if fieldname == "stale_turns":
                return True, (state.get("meta", {}).get("turn", -1)
                              - int(q.get("updated_turn", -1)))
            return (fieldname in q), q.get(fieldname)
        if path.startswith("player."):           # RPG-5: player.hp_frac | level | xp |
            pl = next((p for p in (state.get("player") or {}).values()   # defeated_ago
                       if isinstance(p, dict)), None)
            if pl is None:
                return False, None
            key = path[7:]
            if key == "hp_frac":
                hp = pl.get("hp") or {}
                mx = int(hp.get("max", 0) or 0)
                return (mx > 0), ((int(hp.get("cur", 0)) / mx) if mx else None)
            if key == "defeated_ago":
                d = pl.get("defeated") or {}
                if "turn" not in d:
                    return False, None
                return True, state.get("meta", {}).get("turn", -1) - int(d["turn"])
            return (key in pl), pl.get(key)
        if path.startswith("world."):            # RPG-5: standing world flags
            w = state.get("world", {}) or {}
            return (path[6:] in w), w.get(path[6:])
        return False, None
    except Exception:
        return False, None


def _leaf(cond: dict, state: dict, trace: list) -> bool:
    found, val = resolve_path(state, cond.get("path", ""))
    op, want = cond.get("op"), cond.get("value")
    if op == "exists":
        return found if want in (None, True) else not found
    if not found:
        trace.append(f"path miss: {cond.get('path')}")
        return False
    try:
        if op == "==":
            return val == want
        if op == "!=":
            return val != want
        if op == "in":
            return val in want
        if op == "contains":
            return want in val
        if op in (">", ">=", "<", "<="):
            v, w = float(val), float(want)
            return {"<": v < w, "<=": v <= w, ">": v > w, ">=": v >= w}[op]
    except (TypeError, ValueError, KeyError):
        return False
    trace.append(f"unknown op: {op}")
    return False


def eval_dsl(cond, state: dict, trace: list | None = None) -> bool:
    """Pure over the snapshot; combine all/any/not; unknown anything -> False."""
    trace = trace if trace is not None else []
    try:
        if not isinstance(cond, dict) or not cond:
            return False
        if "all" in cond:
            return all(eval_dsl(c, state, trace) for c in cond["all"])
        if "any" in cond:
            return any(eval_dsl(c, state, trace) for c in cond["any"])
        if "not" in cond:
            return not eval_dsl(cond["not"], state, trace)
        return _leaf(cond, state, trace)
    except Exception:
        return False


# ---------------------------------------------------------------- bindings
def _present(state: dict) -> list[str]:
    """Present chars: entity flag (linter._present parity) ∪ scene participants."""
    ents = {eid for eid, e in (state.get("entities") or {}).items()
            if isinstance(e, dict) and e.get("present")}
    ents |= set(state.get("scene", {}).get("participants") or [])
    return sorted(ents)


def _user_ids(state: dict, user_name: str, aliases: tuple = ()) -> set:
    names = {n.strip().lower() for n in (user_name, *aliases) if n and n.strip()}
    return {eid for eid, e in state.get("entities", {}).items()
            if str(e.get("name", "")).strip().lower() in names} if names else set()


def bindings(beat: dict, state: dict, user_ids: set) -> list[dict]:
    """Deterministic candidate bindings; user's char never fills actor slots (Q12)."""
    present = _present(state)
    actors = [p for p in present if p not in user_ids]
    kind = beat.get("binds", "none")
    if kind == "none":
        return [{}]
    if kind == "char":
        return [{"char": c} for c in actors]
    if kind == "pair":
        return [{"a": a, "b": b} for a in actors for b in present if a != b]
    if kind == "craving":
        out = []
        for c in actors:
            for sub in sorted(state.get("chars", {}).get(c, {}).get("cravings", {})):
                out.append({"char": c, "substance": sub})
        return out
    if kind == "obsession":
        out = []
        for c in actors:
            for key, o in sorted(state.get("chars", {}).get(c, {}).get("obsessions", {}).items()):
                out.append({"char": c, "obs_key": key, "obs_target": str(o.get("target", ""))})
        return out
    if kind == "quest":                          # RPG-5: one binding per ACTIVE quest
        return [{"quest": qid, "quest_name": str((q or {}).get("name", qid))}
                for qid, q in sorted((state.get("quests") or {}).items())
                if isinstance(q, dict) and q.get("status") == "active"]
    if kind == "front":                          # Phase 2: a JUST-FILLED front demands fallout
        turn = state.get("meta", {}).get("turn", -1)   # (the bind IS the filter — recency-gated)
        return [{"front": fid, "front_name": str(f.get("name", fid)),
                 "front_consequence": str(f.get("consequence") or "its consequence lands")[:200]}
                for fid, f in sorted((state.get("fronts") or {}).items())
                if isinstance(f, dict) and f.get("done")
                and 1 <= turn - int(f.get("filled_turn", -10**9)) <= 4]
    return []


def _sub_ids(text: str, binding: dict) -> str:
    return _TOKEN.sub(lambda m: str(binding.get(m.group(1), m.group(0))), text)


def _sub_cond(cond, binding: dict):
    if isinstance(cond, dict):
        return {k: _sub_cond(v, binding) for k, v in cond.items()}
    if isinstance(cond, list):
        return [_sub_cond(c, binding) for c in cond]
    if isinstance(cond, str):
        return _sub_ids(cond, binding)
    return cond


def _name(state: dict, eid: str) -> str:
    return str(state.get("entities", {}).get(eid, {}).get("name") or eid)


def render_note(template: str, binding: dict, state: dict) -> str:
    """{tokens} -> display names; any unresolved token -> drop the note (fail-safe:
    a note with a hole in it damages the RP more than no note)."""
    m = {k: _name(state, v) if k in ("a", "b", "char") else str(v)
         for k, v in binding.items()}
    if "a" in binding:
        m.setdefault("initiator", _name(state, binding["a"]))
        m.setdefault("char", _name(state, binding["a"]))
    if "b" in binding:
        m.setdefault("partner", _name(state, binding["b"]))
    out = _TOKEN.sub(lambda x: m.get(x.group(1), x.group(0)), template)
    if _TOKEN.search(out):
        log.warning("beat note dropped: unresolved placeholder in %r", template[:60])
        return ""
    return " ".join(out.split())


def consent_headroom(beat: dict, binding: dict, state: dict) -> int:
    """Tie-break: prefer beats furthest from consent limits (03 SS8). Sum over consent
    .level leaves of (actual rank - weakest accepted rank); no consent leaves -> 0."""
    total = 0

    def walk(cond):
        nonlocal total
        if isinstance(cond, dict):
            p = cond.get("path", "")
            if p.startswith("consent.") and p.endswith(".level") and cond.get("op") == "in":
                _, val = resolve_path(state, p)
                try:
                    total += CONSENT_RANK.get(val, 2) - min(
                        CONSENT_RANK.get(v, 2) for v in cond.get("value", []) or ["unknown"])
                except (TypeError, ValueError):
                    pass
            for v in cond.values():
                walk(v)
        elif isinstance(cond, list):
            for c in cond:
                walk(c)
    walk(_sub_cond(beat.get("preconditions", {}), binding))
    return total


# ---------------------------------------------------------------- evaluation (03 SS8)
def _cooldown_maps(store, branch_id: str, turn: int):
    rows = store.director_recent(branch_id, turn - 60)
    last: dict = {}
    scenes: dict = {}
    for r in rows:
        last[r["beat_id"]] = max(last.get(r["beat_id"], -1), r["turn_index"])
        scenes.setdefault(r["beat_id"], set()).add(r["scene_index"])
    return last, scenes


def evaluate(store, cfg, session_id: str, branch_id: str, turn: int, state: dict,
             user_name: str = "", user_aliases: tuple = ()) -> str:
    """Pick + fire one beat (or a pacing nudge); returns the rendered beat note ("" = none)."""
    frozen = bool(state.get("frozen"))
    if state.get("scene", {}).get("mode") in ("flashback", "dream") and not frozen:
        return ""                                    # never steer a memory (08 B4)
    libs = (["aftercare_checkin"] if frozen
            else list(cfg.director.beat_libraries))  # frozen -> exclusive (02 SS6)
    arc = state.get("arc")                           # v1: no shipped arcs; hook per 03 SS8
    if arc and arc.get("beat_libraries_enabled"):
        libs = [x for x in libs if x in arc["beat_libraries_enabled"]]
    last, scenes = _cooldown_maps(store, branch_id, turn)
    scene_index = int(state.get("scene", {}).get("scene_index", 0))
    uids = _user_ids(state, user_name, user_aliases)
    cands = []
    for beat in load_libraries(libs):
        bid = beat["beat_id"]
        cd = int(beat.get("cooldown_turns", 6))
        if cd > 0 and bid in last and turn - last[bid] < cd:
            continue
        if beat.get("once_per_scene") and scene_index in scenes.get(bid, ()):
            continue
        for binding in bindings(beat, state, uids):
            if eval_dsl(_sub_cond(beat["preconditions"], binding), state):
                cands.append((beat, binding))
                break                                # first (sorted) binding wins
    if not cands:
        return _pacing_default(store, cfg, branch_id, turn, state, last, scene_index)
    beat, binding = max(cands, key=lambda cb: (cb[0].get("priority", 50),
                                               consent_headroom(cb[0], binding=cb[1],
                                                                state=state),
                                               cb[0]["beat_id"]))
    ops = [json.loads(_sub_ids(json.dumps(op), binding)) for op in beat.get("effects", [])]
    if beat.get("phase_hint"):
        ops.append({"op": "scene_set", "phase": beat["phase_hint"]})
    if ops:
        try:
            apply_delta(store, session_id, branch_id, turn, ops, "rule", cfg)
        except Exception as exc:                     # effects never block the note
            log.warning("beat effects failed open: %s", type(exc).__name__)
    store.director_add(branch_id, turn, beat["beat_id"], scene_index)
    return render_note(beat["note_template"], binding, state)


def _pacing_default(store, cfg, branch_id: str, turn: int, state: dict,
                    last: dict, scene_index: int) -> str:
    """03 SS8: no candidate beats -> tension-vs-curve / stagnation nudge (pseudo-beats)."""
    if bool(state.get("frozen")):
        return ""
    sc = state.get("scene", {})

    def fire(bid: str, note: str) -> str:
        if bid in last and turn - last[bid] < PACING_COOLDOWN:
            return ""
        store.director_add(branch_id, turn, bid, scene_index)
        return note
    if float(sc.get("stagnation", 0.0)) >= cfg.director.stagnation_threshold:
        return fire("pacing.complication",
                    "The scene is circling itself. Introduce a complication — an arrival, "
                    "a discovery, a demand — that forces someone's hand.")
    target = PHASE_TARGETS.get(sc.get("phase") or "setup", 40)
    tension = int(sc.get("tension", 0))
    if tension < target - PACING_BAND:
        return fire("pacing.raise",
                    "Tighten the screws a notch — raise the stakes or the heat toward "
                    "the scene's natural pitch.")
    if tension > target + PACING_BAND:
        return fire("pacing.ease",
                    "Let the scene breathe — slow the pace and allow a quieter beat "
                    "before the next push.")
    return ""


# ---------------------------------------------------------------- note staging (04 SS3.3)
def best_corrective(vios: list, cfg, state: dict) -> str:
    """Single highest-severity corrective (moved from linter; L9 -> guard channel)."""
    if not cfg.linter.corrective_notes or state.get("frozen"):
        return ""                                    # aftercare owns the slot when frozen
    cands = [x for x in vios if x.note and not x.advisory and x.rule != "L9"]
    if not cands:
        return ""
    best = max(cands, key=lambda x: (_SEV_RANK[x.severity], x.rule))
    return best.note


def stage(store, cfg, session_id: str, branch_id: str, turn: int, state: dict,
          vios: list, user_name: str = "", user_aliases: tuple = ()) -> str:
    """Assemble + persist next turn's note: '[Direction] {beat_note} {corrective}'.
    Fail-open: any error leaves the previous note cleared, never raises."""
    note = ""
    try:
        beat_note = ""
        if cfg.director.enabled:
            beat_note = evaluate(store, cfg, session_id, branch_id, turn, state,
                                 user_name=user_name, user_aliases=user_aliases)
        corrective = best_corrective(vios, cfg, state)
        parts = [p for p in (beat_note, corrective) if p]
        note = f"[Direction] {' '.join(parts)}" if parts else ""
    except Exception as exc:
        log.warning("director stage failed open: %s", type(exc).__name__)
        note = ""
    try:
        store.write_note(session_id, turn + 1, note)  # "" clears any stale note
    except Exception:
        pass
    return note
