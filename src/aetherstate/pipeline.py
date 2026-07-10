"""Per-request enrichment pipeline: observe -> Tier-0 -> apply -> compose -> forward,
plus the post-stream half: response tee -> turn-text capture -> discovery -> extraction.

Hot path (pre-forward, deterministic sub-ms — Q15): observe, Tier-0, authority apply,
header compose. Cold path (strictly post-stream, 03 SS1): assistant-text capture, entity
discovery (08 B2), and Tier-1 scheduling via the JobRunner. Every step is fail-open
(invariant 1/2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from . import compose, director, discovery, genesis, linter, promptcache, tier0
from .config import Config
from .session_engine import SessionEngine
from . import memory
from .state import apply_delta, combat_ops, current_state, progression_ops, world_ops
from .stamps import Stamp
from .store import Store

log = logging.getLogger("aetherstate.pipeline")


def _live_recalc(cfg) -> bool:
    """True when the newest reply is ingested immediately (default). Bean 2026-07-07."""
    return bool(getattr(getattr(cfg, "extraction", None), "live_recalc", True))


@dataclass
class PostContext:
    """What the response tee needs to finish the turn after the stream ends."""
    session_id: str
    branch_id: str
    turn_index: int
    klass: str
    speaker: Optional[str] = None
    card: str = ""                # Q23: genesis stage-B inputs (new_session only)
    opening: str = ""
    evolutions: Optional[list] = None   # RPG-5: (char, table, id, bracket) crossings this
    #                                     turn — the cold path schedules Q27 re-authoring
    enriched: bool = False              # Phase 0a: this request was actually changed (the
    #                                     prompt_cache_key-carrying set — stats denominator)


class Pipeline:
    def __init__(self, store: Store, engine: SessionEngine, cfg: Config,
                 jobs=None, rng: Optional[random.Random] = None) -> None:
        self.store, self.engine, self.cfg, self.jobs = store, engine, cfg, jobs
        self.rng = rng or random.Random()
        # ---- Phase 0a: prompt-cache plumbing (all in-memory, all fail-open) ----
        self.cache = promptcache.CacheStats()
        self._last_docs: OrderedDict[str, dict] = OrderedDict()   # sid -> last enriched doc
        self._prewarm_at: dict[str, float] = {}                   # sid -> monotonic cooldown
        # 2026-07-10 (Eranmor, pillar 17): tier0 notices used to die in the proxy log —
        # "recharging"/non-move/unknown-skill were invisible to the player. A bounded
        # in-memory ring per session feeds the HUD rolls lane (transient UX, not state).
        self._notices: OrderedDict[str, list] = OrderedDict()
        if getattr(cfg.upstream, "cache_key", True) \
                and cfg.injection.placement == "system_merge":
            log.info("prompt-cache: placement=system_merge splices volatile state into the "
                     "FIRST system message — every turn invalidates the provider's prefix "
                     "cache; placement=depth (default) keeps volatile bytes at the tail")

    # ------------------------------------------------------------------ hot path
    def process(self, stamp: Optional[Stamp], body: bytes) -> tuple[bytes, Optional[PostContext]]:
        """Returns (bytes to forward, tee context | None). Proxy wraps in its own guard."""
        res = self.engine.observe(stamp, body)
        if res is None:                       # quiet gen / non-chat payload: passthrough
            return body, None
        if self.store.session_mode(res.session_id) == "passthrough":
            return body, None                 # 05 SS7: per-session kill-switch — byte-exact
        doc = json.loads(body)
        changed = False
        card = opening = ""
        if res.klass.value == "new_session" and not res.duplicate:   # Q23 stage A: inline
            card, opening = genesis.card_and_prompt(doc)             # rules seed (sub-ms)
            genesis.seed_rules(self.store, self.cfg, res.session_id, res.branch_id, doc,
                               speaker=(res.stamp.speaker if res.stamp else "") or "")
            genesis.seed_player(self.store, self.cfg, res.session_id, res.branch_id, doc)
        state = current_state(self.store, res.branch_id)

        evolutions: list = []
        if not res.duplicate:                 # 08 S7: retries never double-apply
            t0 = tier0.run(doc, res.klass.value, res.duplicate, state, self.cfg, self.rng)
            if t0.doc is not None:            # OOC spans stripped (03 R1)
                doc = t0.doc
                changed = True
            reserved = self._reserve_lost_turn(res, doc)
            if reserved is not None:
                # 2026-07-10 (Eranmor): the SAME action re-sent after a lost generation —
                # its rolls/costs/cooldowns are already journaled at the previous turn. One
                # player action = one resolution: re-serve the settled checks on THIS
                # [DIRECTIVE] and apply nothing new (no re-roll, no double clock/cost).
                state["_fresh_checks"] = reserved
                if t0.off_protocol:
                    state["_protocol_nudge"] = t0.off_protocol
                log.info("re-serve: %d settled check(s) from the lost previous turn",
                         len(reserved))
                self._notice(res, [f"reply was lost — re-serving the settled "
                                   f"{str(c.get('skill', 'check'))} roll ({c.get('tier')})"
                                   for c in reserved])
                self._capture_user_text(doc, res)
            else:
                applied_now: list = []
                if t0.user_ops:               # user source FIRST: freeze gates the rule batch
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.user_ops, "user", self.cfg)
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                if t0.rule_ops:
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.rule_ops, "rule", self.cfg)
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                if t0.proposal_ops:           # R9/R10: model-authored ledger tags apply
                    r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                    t0.proposal_ops, "extraction", self.cfg)   # clamped
                    state = r.state
                    applied_now += r.applied
                    self._index_memories(res, r)
                state, evolutions = self._progress(res, state, applied_now)   # RPG-5 (doc 10)
                # [DIRECTIVE] shows EXACTLY the checks resolved THIS request (not
                # turn-matched): reliable delivery + no stale rolls confusing the model.
                state["_fresh_checks"] = [o for o in applied_now
                                          if isinstance(o, dict) and o.get("op") == "check"]
                if t0.off_protocol:
                    state["_protocol_nudge"] = t0.off_protocol
                if getattr(t0, "kill_note", ""):        # out-of-combat kill outcome (2026-07-10)
                    state["_kill_note"] = t0.kill_note
                for n in t0.notices:
                    log.info("tier0 notice: %s", n)
                self._notice(res, t0.notices)
                self._capture_user_text(doc, res)
                self._swipe_rollback_guard(res)

        if self.jobs is not None and isinstance(doc.get("model"), str):
            self.jobs.models[res.session_id] = doc["model"]

        recall = self.store.read_recall(res.session_id)     # Q15: one SELECT on the hot path
        note, l9 = "", None                                  # + two tiny indexed reads (03 SS9)
        try:
            note = self.store.read_note(res.session_id)
            if self.cfg.user_guard.enabled and self.cfg.user_guard.mode == "prevent_and_correct":
                l9 = self.store.lint_l9_evidence(
                    res.branch_id, res.turn_index - self.cfg.consent.guard_escalate_turns)
        except Exception:                                    # fail-open: base guard + no note
            note, l9 = "", None
        out_doc, kept = compose.compose(doc, state, self.cfg, res.stamp or stamp,
                                        res.klass.value, recall=recall, note=note,
                                        guard_evidence=l9)
        if out_doc is not None:
            doc = out_doc
            changed = True
        try:
            self.store.write_slice(res.session_id, res.turn_index, kept)
        except Exception:                     # slice row is observability, never load-bearing
            pass
        if changed and getattr(self.cfg.upstream, "cache_key", True):
            # Phase 0a: every turn of a conversation routes to the same warm provider
            # cache. ONLY on requests the engine already changed — an untouched request
            # stays byte-identical (transparency), so `none`-untouched wires carry nothing.
            promptcache.add_cache_key(doc, res.session_id)
            if getattr(self.cfg.upstream, "include_usage", False):
                promptcache.add_usage_probe(doc)
            self._last_docs[res.session_id] = doc          # prewarm source (bounded LRU)
            self._last_docs.move_to_end(res.session_id)
            while len(self._last_docs) > promptcache.LAST_DOCS_MAX:
                self._last_docs.popitem(last=False)
        ctx = PostContext(res.session_id, res.branch_id, res.turn_index, res.klass.value,
                          speaker=(res.stamp.speaker if res.stamp else None),
                          card=card, opening=opening,
                          evolutions=evolutions or None, enriched=changed)
        return (compose.to_bytes(doc) if changed else body), ctx

    def prewarm_doc(self, session_id: str) -> Optional[dict]:
        """Phase 0a stretch: the session's last enriched doc IF the per-session
        prewarm cooldown allows — claims the cooldown slot when it returns one.
        None = nothing remembered (fresh proxy / never enriched) or on cooldown."""
        doc = self._last_docs.get(session_id)
        if doc is None:
            return None
        now = time.monotonic()
        if now - self._prewarm_at.get(session_id, 0.0) < promptcache.PREWARM_COOLDOWN_S:
            return None
        self._prewarm_at[session_id] = now
        return doc

    def _progress(self, res, state: dict, applied: list) -> tuple[dict, list]:
        """RPG-5 hot-path progression pass (µs, pure arithmetic + one apply): XP awards,
        level-ups, and defeat resolution derived from THIS turn's applied ops; also
        collects mastery-bracket crossings for the cold-path Q27 evolution hook.
        Fail-open — any error leaves state exactly as it was (invariant 1)."""
        evolutions: list = []
        try:
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg" or not state.get("player"):
                return state, evolutions
            for op in applied:
                if op.get("op") == "master_tick" and op.get("_bracket_up"):
                    evolutions.append((op.get("char"), "skills", op.get("skill"),
                                       op.get("_bracket_up")))
            if getattr(spec, "war_room", True):   # Phase 1: the combat referee runs FIRST —
                wr = combat_ops(state, applied)   # defeats + curated XP feed the level pass
                if wr:
                    r0 = apply_delta(self.store, res.session_id, res.branch_id,
                                     res.turn_index, wr, "rule", self.cfg)
                    state = r0.state
                    applied = list(applied) + r0.applied
                    self._index_memories(res, r0)
            pro = progression_ops(state, applied,
                                  hardcore=getattr(spec, "hardcore", False))
            if pro:
                r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                pro, "rule", self.cfg)
                state = r.state
                applied = list(applied) + r.applied
                self._index_memories(res, r)
            if getattr(spec, "living_world", True):   # Phase 2: the world moves — travel
                lw = world_ops(state, applied,        # time, the idle clock, faction fronts
                               clock_turns=getattr(spec, "clock_turns", 6))
                if lw:
                    rl = apply_delta(self.store, res.session_id, res.branch_id,
                                     res.turn_index, lw, "rule", self.cfg)
                    state = rl.state
                    self._index_memories(res, rl)
            return state, evolutions
        except Exception as exc:
            log.warning("progression pass failed open: %s", type(exc).__name__)
        return state, evolutions

    def _index_memories(self, res, r) -> None:
        """Mirror user/rule memory_event ops into the retrieval index (fail-open)."""
        try:
            memory.index_applied(self.store, res.session_id, res.branch_id,
                                 r.applied, r.state)
        except Exception as exc:
            log.warning("memory index skipped: %s", type(exc).__name__)

    def _reserve_lost_turn(self, res, doc: dict) -> Optional[list[dict]]:
        """2026-07-10 (Eranmor): detect the retry of a LOST turn. A `new_turn` whose user
        text is byte-identical to the PREVIOUS turn's, when that turn's reply settled
        EMPTY (dead upstream stream / client abort), is the same player action re-sent —
        its checks already rolled, paid, and cooled at the previous turn. Returns those
        settled check ops (marked `lost_reply`) to re-serve on this turn's [DIRECTIVE],
        or None for the normal path. Fail-open: any doubt -> None (today's behavior).
        De-facto rpg-gated: only `check` ops re-serve, and checks exist only under rpg —
        a `none` session always returns None (byte-identical wire)."""
        try:
            if not getattr(self.cfg.session, "reserve_lost_turns", True):
                return None
            if res.klass.value != "new_turn":
                return None
            prev = self.store.db.execute(
                "SELECT MAX(turn_index) AS t FROM turns WHERE branch_id=? AND turn_index<?",
                (res.branch_id, res.turn_index)).fetchone()
            pt = prev["t"] if prev else None
            if pt is None or pt < 0:
                return None
            rows = self.store.get_turn_texts(res.branch_id, pt, pt)
            if not rows:
                return None
            prev_user = rows[0]["user_text"] or ""
            if rows[0]["assistant_text"] not in (None, ""):
                return None                    # the reply exists — a genuinely new turn
            name = (self.cfg.user_guard.name
                    or (res.stamp.user if res.stamp and res.stamp.user else "") or "User")
            msgs = doc.get("messages", [])
            text = next((tier0._msg_text(m.get("content")) for m in reversed(msgs)
                         if isinstance(m, dict) and m.get("role") == "user"), "")
            text = " ".join(text.split())
            if not text or f"{name}: {text}" != prev_user:
                return None
            checks = [dict(o, lost_reply=True)
                      for o in self.store.rule_ops_at(res.branch_id, pt)
                      if o.get("op") == "check" and o.get("tier")]
            return checks or None              # nothing to re-serve -> normal path
        except Exception as exc:
            log.warning("re-serve detection failed open: %s", type(exc).__name__)
            return None

    def _notice(self, res, msgs: list) -> None:
        """Pillar 17: mirror tier0 notices into the per-session HUD ring (fail-open)."""
        try:
            if not msgs:
                return
            ring = self._notices.setdefault(res.session_id, [])
            ring.extend({"turn": res.turn_index, "ts": time.time(),
                         "text": str(m)[:200]} for m in msgs[:6])
            del ring[:-12]
            self._notices.move_to_end(res.session_id)
            while len(self._notices) > 64:
                self._notices.popitem(last=False)
        except Exception:
            pass

    def recent_notices(self, session_id: str) -> list[dict]:
        """The HUD's notice feed (newest last). Transient — a restart clears it."""
        try:
            return list(self._notices.get(session_id, ()))
        except Exception:
            return []

    def _capture_user_text(self, doc: dict, res) -> None:
        """Retain the NEW user message (post-OOC-strip) for extraction context (01 SS7)."""
        name = (self.cfg.user_guard.name
                or (res.stamp.user if res.stamp and res.stamp.user else "") or "User")
        if self.jobs is not None:            # CHARACTERS [USER] mark (04 SS1.2)
            self.jobs.user_names[res.session_id] = name
        if res.klass.value not in ("new_turn", "new_session", "impersonate"):
            return
        msgs = doc.get("messages", [])
        text = next((tier0._msg_text(m.get("content")) for m in reversed(msgs)
                     if isinstance(m, dict) and m.get("role") == "user"), "")
        text = " ".join(text.split())        # OOC strip can leave double spaces
        if not text:
            return
        self.store.write_turn_text(res.branch_id, res.turn_index,
                                   user_text=f"{name}: {text}")

    def _swipe_rollback_guard(self, res) -> None:
        """03 SS3.3 / 08 E7: a swiped turn that already got extracted rolls back and re-queues.
        Under live_recalc the head is extracted on its OWN cold path, so EVERY swipe retracts
        the prior generation's extraction-source ops BEFORE the new reply re-derives them — the
        resolved check/roll survives (retract is source-scoped). Legacy lag-1: only when the
        turn actually reached extraction='done' (rare, hence the whole-turn rollback is fine)."""
        if res.klass.value != "swipe":
            return
        if _live_recalc(self.cfg):
            self.store.retract_extraction_at(res.branch_id, res.turn_index)
            return
        row = self.store.db.execute(
            "SELECT extraction FROM turns WHERE branch_id=? AND turn_index=?",
            (res.branch_id, res.turn_index)).fetchone()
        if row and row["extraction"] == "done":
            log.info("swipe rollback: retracting extracted state at turn %d", res.turn_index)
            self.store.rollback_to(res.branch_id, res.turn_index - 1)

    # ------------------------------------------------------------------ cold path
    def on_response(self, ctx: Optional[PostContext], raw: bytes, content_type: str) -> None:
        """Called by the proxy tee AFTER the stream ends. Never raises."""
        if ctx is None:
            return
        try:
            if ctx.enriched:                 # Phase 0a: cache observability (pillar 17)
                self.cache.observe(ctx.session_id,
                                   promptcache.parse_usage(raw, content_type))
            text = _response_text(raw, content_type)
            if text and text.strip():
                speaker = ctx.speaker or "Narrator"
                self.store.write_turn_text(ctx.branch_id, ctx.turn_index,
                                           assistant_text=f"{speaker}: {text.strip()}")
            self._ingest_reply_tags(ctx, text)   # live_recalc: newest reply's world-tags NOW
            self._discover(ctx)               # 08 B2 Tier-0 evidence pass (fail-open)
            self._recall_pass(ctx)            # P4/Q15: keep recall fresh in rules-only mode
            self._lint_pass(ctx, text)        # 03 SS9 (full in off/rules; L9-only otherwise)
            self._genesis_pass(ctx)           # Q23 stage B: assist-LLM seed (cold path)
            self._evolve_pass(ctx)            # RPG-5: Q27 mastery re-authoring (cold path)
            if self.jobs is not None:
                # settle the head NOW so Tier-1 extracts the newest reply on its OWN cold path
                # (Bean 07-07). Skip turn-0: let genesis stage-B seed the world first, exactly as
                # before, so opening-turn extraction still lands on turn 1's cold path.
                if _live_recalc(self.cfg) and ctx.klass != "new_session":
                    try:
                        self.store.settle_head(ctx.branch_id)
                    except Exception:
                        pass
                self.jobs.notify(ctx.session_id, ctx.branch_id, ctx.turn_index)
        except Exception as exc:
            log.warning("response tee failed open: %s", type(exc).__name__)

    def _ingest_reply_tags(self, ctx: PostContext, text: Optional[str]) -> None:
        """live_recalc (Bean 2026-07-07): parse the DM's FRESH reply's world/effect tags
        (R9/R10) the instant its stream ends and commit them to the ledger at THIS turn
        (source='extraction'), so state reflects the NEWEST output — not the reply before it.
        rpg-gated; a swipe already retracted the prior generation (hot-path guard). Fail-open —
        any error leaves the ledger exactly as it was (invariant 3)."""
        try:
            if not (text and text.strip()) or not _live_recalc(self.cfg):
                return
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg":
                return
            state = current_state(self.store, ctx.branch_id)
            ops = tier0.parse_reply_tags(text, state)
            applied: list = []
            if ops:
                r = apply_delta(self.store, ctx.session_id, ctx.branch_id, ctx.turn_index,
                                ops, "extraction", self.cfg)
                state = r.state
                applied = r.applied
                self._index_memories(ctx, r)
                for q in r.quarantined:
                    log.info("live tag quarantined: %s", q.get("reason", ""))
            if getattr(spec, "war_room", True):
                foes = tier0.parse_foe_tags(text, state)   # Phase 1: the DM's [foe] tag is
                if foes:                                   # validated, then RE-SOURCED as a
                    r = apply_delta(self.store, ctx.session_id, ctx.branch_id,   # rule op —
                                    ctx.turn_index, foes, "rule", self.cfg)      # the R8b
                    state = r.state                                              # pattern
                    applied += r.applied
                    self._index_memories(ctx, r)
                wr = combat_ops(state, applied)   # the referee: enlistment, HP-0 defeats +
                if wr:                            # loot, combat_end — on the fresh reply's
                    r = apply_delta(self.store, ctx.session_id, ctx.branch_id,   # own turn
                                    ctx.turn_index, wr, "rule", self.cfg)
                    state = r.state
                    applied += r.applied
                    self._index_memories(ctx, r)
            if applied and getattr(spec, "living_world", True):
                # Phase 2 (2026-07-09 Cinderveil live): the living-world referee must read
                # the FRESH reply's ops too — this path applied the player's move_entity,
                # but only jobs/_progress ran world_ops, so travel time and the camera
                # never followed a reply-committed move (the ladder later dedups it away).
                lw = world_ops(state, applied,
                               clock_turns=getattr(spec, "clock_turns", 6))
                if lw:
                    r = apply_delta(self.store, ctx.session_id, ctx.branch_id,
                                    ctx.turn_index, lw, "rule", self.cfg)
                    self._index_memories(ctx, r)
                    log.info("living world (live path): %d op(s) applied", len(r.applied))
        except Exception as exc:
            log.warning("live tag ingest failed open: %s", type(exc).__name__)

    def _recall_pass(self, ctx: PostContext) -> None:
        """Cold-path recall staging when NO extraction job will run for this session
        (extraction jobs do their own precompute with the settled exchange). Fail-open."""
        try:
            if self.cfg.extraction.mode not in ("off", "rules"):
                return                        # jobs path owns it
            state = current_state(self.store, ctx.branch_id)
            rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index,
                                             ctx.turn_index)
            q = " ".join(t for r in rows for t in (r["user_text"], r["assistant_text"]) if t)
            memory.reflect(self.store, self.cfg, ctx.session_id, ctx.branch_id, state)
            memory.precompute_recall(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                                     state, q, ctx.turn_index)
        except Exception as exc:
            log.warning("recall pass skipped: %s", type(exc).__name__)

    def _genesis_pass(self, ctx: PostContext) -> None:
        """Q23 stage B: schedule the full-matrix LLM seed after turn 1's stream ends.
        off/rules extraction -> stage A is the whole product (mark done). Fail-open."""
        try:
            if ctx.klass != "new_session" or not ctx.card:
                return
            if self.cfg.extraction.mode in ("off", "rules") or self.jobs is None:
                if self.store.genesis_state(ctx.session_id) == "rules":
                    self.store.genesis_mark(ctx.session_id, "done")
                return
            ep, _, _ = self.jobs.endpoint_for(ctx.session_id)
            t = asyncio.get_running_loop().create_task(
                genesis.seed_llm(self.store, self.cfg, self.jobs.ladder.get_client, ep,
                                 ctx.session_id, ctx.branch_id, ctx.card, ctx.opening,
                                 speaker=ctx.speaker or ""))
            self.jobs._tasks.add(t)
            t.add_done_callback(self.jobs._tasks.discard)
        except Exception as exc:
            log.warning("genesis schedule failed open: %s", type(exc).__name__)

    def _evolve_pass(self, ctx: PostContext) -> None:
        """RPG-5 (doc 10 §4 / Q27): a mastery bracket crossed this turn schedules a cold-path
        assist re-authoring of that skill's frozen def. Fail-open at every step — without an
        assist model the curated bracket bonus (registry.effective_mod) IS the evolution."""
        try:
            if not ctx.evolutions or self.jobs is None:
                return
            spec = getattr(self.cfg, "specialization", None)
            if spec is None or spec.name != "rpg":
                return
            from . import creator as _creator
            ep, _, _ = self.jobs.endpoint_for(ctx.session_id)
            for (char, table, sid, bracket) in ctx.evolutions[:2]:   # bounded per turn
                t = asyncio.get_running_loop().create_task(
                    _creator.evolve_def_snapshot(
                        self.store, self.cfg, self.jobs.ladder.get_client, ep,
                        ctx.session_id, ctx.branch_id, str(char), str(table), str(sid),
                        str(bracket), turn=ctx.turn_index))
                self.jobs._tasks.add(t)
                t.add_done_callback(self.jobs._tasks.discard)
        except Exception as exc:
            log.warning("evolve schedule failed open: %s", type(exc).__name__)

    def _lint_pass(self, ctx: PostContext, text: Optional[str]) -> None:
        """Cold-path lint. off/rules extraction: the Tier-0 apply IS the post-apply
        snapshot -> full L1-L9 here. main/assist: the batch job runs the full pass
        post-extraction-apply; only L9 (prose-only, needs no snapshot) runs NOW so the
        guard can escalate on the very next turn (Q12). Cooldown dedups the overlap."""
        try:
            if not self.cfg.linter.enabled or not (text and text.strip()):
                return
            name = (self.cfg.user_guard.name
                    or (self.jobs.user_names.get(ctx.session_id, "") if self.jobs else ""))
            aliases = tuple(self.cfg.user_guard.aliases)
            full = self.cfg.extraction.mode in ("off", "rules")
            state = current_state(self.store, ctx.branch_id)
            cfg = self.cfg
            if not full:                      # L9-only quick pass (see docstring)
                import copy
                cfg = copy.deepcopy(self.cfg)
                cfg.linter.rules_off = sorted(set(cfg.linter.rules_off)
                                              | {f"L{i}" for i in range(1, 9)})
            utext = ""
            try:                              # L9 door + L11 read the player's message (0b)
                rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index,
                                                 ctx.turn_index)
                utext = next((r["user_text"] or "" for r in rows), "")
            except Exception:
                utext = ""
            fresh = linter.lint_turn(self.store, cfg, ctx.session_id, ctx.branch_id,
                                     ctx.turn_index, state, text, klass=ctx.klass,
                                     user_name=name, user_aliases=aliases,
                                     user_text=utext)
            if full:                      # Tier-0 apply IS the post-apply snapshot (03 SS8)
                director.stage(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                               ctx.turn_index, state, fresh, user_name=name,
                               user_aliases=aliases)
            else:                         # batch job owns the note; consume the stale one
                self.store.write_note(ctx.session_id, ctx.turn_index + 1, "")
        except Exception as exc:              # invariant 3: linter never breaks the turn
            log.warning("lint pass skipped: %s", type(exc).__name__)

    def _discover(self, ctx: PostContext) -> None:
        """Entity discovery over this turn's captured prose (08 B2). Any error stays here."""
        try:
            if self.cfg.extraction.mode == "off":
                return
            rows = self.store.get_turn_texts(ctx.branch_id, ctx.turn_index, ctx.turn_index)
            text = "\n".join((r["user_text"] or "") + "\n" + (r["assistant_text"] or "")
                             for r in rows)
            if not text.strip():
                return
            state = current_state(self.store, ctx.branch_id)
            guard = self.cfg.user_guard.name or \
                (self.jobs.user_names.get(ctx.session_id, "") if self.jobs else "")
            known = discovery.known_names(state, (guard, ctx.speaker or ""))
            discovery.observe_text(self.store, self.cfg, ctx.session_id, ctx.branch_id,
                                   ctx.turn_index, text, known)
            if getattr(self.cfg, "specialization", None) is not None \
                    and self.cfg.specialization.name == "rpg":
                # RPG-4: places persist once too — rpg-gated so a `none` session's journal
                # stays byte-identical (invariant: no fingerprint under none).
                discovery.observe_locations(self.store, self.cfg, ctx.session_id,
                                            ctx.branch_id, ctx.turn_index, text, state)
        except Exception as exc:
            log.warning("discovery pass failed open: %s", type(exc).__name__)


def _response_text(raw: bytes, content_type: str) -> Optional[str]:
    """Assistant text from a completed response: SSE deltas or plain JSON body."""
    if b"data:" in raw[:256] or "text/event-stream" in (content_type or ""):
        parts = []
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                doc = json.loads(payload)
                ch = (doc.get("choices") or [{}])[0]
                parts.append((ch.get("delta") or {}).get("content")
                             or (ch.get("message") or {}).get("content") or "")
            except (json.JSONDecodeError, ValueError, AttributeError, IndexError):
                continue
        return "".join(parts)
    try:
        doc = json.loads(raw)
        ch = (doc.get("choices") or [{}])[0]
        return (ch.get("message") or {}).get("content") or (ch.get("text") or "")
    except (json.JSONDecodeError, ValueError, AttributeError, IndexError):
        return None
