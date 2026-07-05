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
from dataclasses import dataclass
from typing import Optional

from . import compose, director, discovery, genesis, linter, tier0
from .config import Config
from .session_engine import SessionEngine
from . import memory
from .state import apply_delta, current_state
from .stamps import Stamp
from .store import Store

log = logging.getLogger("aetherstate.pipeline")


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


class Pipeline:
    def __init__(self, store: Store, engine: SessionEngine, cfg: Config,
                 jobs=None, rng: Optional[random.Random] = None) -> None:
        self.store, self.engine, self.cfg, self.jobs = store, engine, cfg, jobs
        self.rng = rng or random.Random()

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
        state = current_state(self.store, res.branch_id)

        if not res.duplicate:                 # 08 S7: retries never double-apply
            t0 = tier0.run(doc, res.klass.value, res.duplicate, state, self.cfg, self.rng)
            if t0.doc is not None:            # OOC spans stripped (03 R1)
                doc = t0.doc
                changed = True
            if t0.user_ops:                   # user source FIRST: freeze gates the rule batch
                r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                t0.user_ops, "user", self.cfg)
                state = r.state
                self._index_memories(res, r)
            if t0.rule_ops:
                r = apply_delta(self.store, res.session_id, res.branch_id, res.turn_index,
                                t0.rule_ops, "rule", self.cfg)
                state = r.state
                self._index_memories(res, r)
            for n in t0.notices:
                log.info("tier0 notice: %s", n)
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
        ctx = PostContext(res.session_id, res.branch_id, res.turn_index, res.klass.value,
                          speaker=(res.stamp.speaker if res.stamp else None),
                          card=card, opening=opening)
        return (compose.to_bytes(doc) if changed else body), ctx

    def _index_memories(self, res, r) -> None:
        """Mirror user/rule memory_event ops into the retrieval index (fail-open)."""
        try:
            memory.index_applied(self.store, res.session_id, res.branch_id,
                                 r.applied, r.state)
        except Exception as exc:
            log.warning("memory index skipped: %s", type(exc).__name__)

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
        """03 SS3.3 / 08 E7: a swiped turn that already got extracted (early flush) rolls back
        and re-queues. Lag-1 makes this rare; the guard makes it correct."""
        if res.klass.value != "swipe":
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
            text = _response_text(raw, content_type)
            if text and text.strip():
                speaker = ctx.speaker or "Narrator"
                self.store.write_turn_text(ctx.branch_id, ctx.turn_index,
                                           assistant_text=f"{speaker}: {text.strip()}")
            self._discover(ctx)               # 08 B2 Tier-0 evidence pass (fail-open)
            self._recall_pass(ctx)            # P4/Q15: keep recall fresh in rules-only mode
            self._lint_pass(ctx, text)        # 03 SS9 (full in off/rules; L9-only otherwise)
            self._genesis_pass(ctx)           # Q23 stage B: assist-LLM seed (cold path)
            if self.jobs is not None:
                self.jobs.notify(ctx.session_id, ctx.branch_id, ctx.turn_index)
        except Exception as exc:
            log.warning("response tee failed open: %s", type(exc).__name__)

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
            fresh = linter.lint_turn(self.store, cfg, ctx.session_id, ctx.branch_id,
                                     ctx.turn_index, state, text, klass=ctx.klass,
                                     user_name=name, user_aliases=aliases)
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
