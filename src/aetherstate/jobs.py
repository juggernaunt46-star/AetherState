"""Post-stream job runner: settlement -> debounce/batch -> Tier-1 extraction (03 SS3.2/SS5).

The cold path starts HERE (03 SS1): nothing in this module ever runs before the user's
stream completes. Scheduling is restart-durable by construction — pending work is re-derived
from turns.extraction='pending' (no jobs table); a crashed batch simply re-runs on the next
settle. Per-session work stays serialized (03: SimpleMutex model) via the per-branch
_queued guard; per-ENDPOINT concurrency is a semaphore sized by max_concurrent (Q8).

Q8 routing: extraction mode "assist" sends batches to cfg.assist.endpoints[0] (nano/small
tiers keep the OP CARD via assist_tier — 04 SS5); no endpoints configured -> fall back to
the main upstream, warned once. The queue is a PriorityQueue seeded for the P4+ job families
(extraction > linter > director > reflection > lore — 06 C), extraction-only today.

08 B2: quarantined unknown-name ops feed the discovery counter; on creation the current
batch's ops for that name re-apply immediately (retro-unquarantine).

09 C2: fail_autodisable_after consecutive failed batches -> Tier-1 disabled for that session
until fail_reenable_after_turns more turns pass. A failure NEVER touches existing state.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import assist, chat_continuity, compose, director, discovery, linter, memory
from .claim_ingress import link_extracted_beliefs_to_claims
from .extraction import (Endpoint, Ladder, filter_unearned_social_ops,
                         filter_realization_owned_ops,
                         filter_unrealized_memory_ops)
from .experience import config_for_experience
from .narrator_realization import build_narrator_realization_from_state
from .state import (apply_delta, apply_partitioned_delta, assign_damage_effect_ids, battle_ops,
                     combat_ops, current_state, faction_cascade_ops, is_empty,
                     progression_ops, reduce_state, world_ops)

log = logging.getLogger("aetherstate.jobs")

PRIORITY = {"extraction": 0, "linter": 1, "director": 2, "reflection": 3, "lore": 4}
_UNKNOWN_NAME = re.compile(r"unknown entity '(.+?)'")


@dataclass
class Batch:
    session_id: str
    branch_id: str
    lo: int
    hi: int
    head: int
    request_cfg: object = None
    experience_mode: str = ""
    response_occurrence_id: str = ""


class JobRunner:
    def __init__(self, store, cfg, ladder: Ladder) -> None:
        self.store, self.cfg, self.ladder = store, cfg, ladder
        self.queue: asyncio.PriorityQueue[tuple[int, int, Batch]] = asyncio.PriorityQueue()
        self._seq = itertools.count()           # FIFO tie-break within a priority class
        self._worker: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()  # in-flight guarded batch tasks
        self._sems: dict[str, asyncio.Semaphore] = {}   # endpoint name -> concurrency gate
        self._debounce: dict[str, asyncio.Task] = {}
        self._queued: set[str] = set()          # branch ids with a batch in queue/flight
        self._fails: dict[str, int] = {}        # session -> consecutive failed batches
        self._transient_retries: dict[tuple[str, str, int, int], int] = {}
        self._disabled_until: dict[str, int] = {}   # session -> turn index (09 C2)
        self.models: dict[str, str] = {}        # session -> model from the last request
        self.user_names: dict[str, str] = {}    # session -> resolved player name (Q12 chain)
        self._inflight = 0
        self._warned_no_assist = False

    def _config_for_session(self, session_id: str):
        """Recover the persisted request-local profile for restart/drain scheduling."""
        try:
            binding = self.store.experience_binding(session_id)
            if not binding.source and not binding.locked:
                return self.cfg
            return config_for_experience(self.cfg, binding.mode)
        except (KeyError, ValueError):
            return None

    def _batch_context(self, session_id: str, request_cfg=None):
        """Resolve one durable experience boundary before any snapshot is rendered."""
        cfg = request_cfg or self._config_for_session(session_id) or self.cfg
        binding = self.store.experience_binding(session_id)
        bound_chat = (
            binding.mode == "chat"
            and bool(binding.core_fingerprint)
            and bool(binding.character_actor_id)
            and bool(binding.persona_actor_id)
        )
        if bound_chat:
            return "chat", cfg, binding
        spec = getattr(cfg, "specialization", None)
        mode = "rpg" if binding.mode == "rpg" or (
            spec is not None and getattr(spec, "name", "none") == "rpg"
        ) else ""
        return mode, cfg, binding

    def _batch_candidate_current(self, batch: Batch, *, status: str = "pending") -> bool:
        if batch.experience_mode != "chat":
            return self.store.extraction_range_is(
                batch.branch_id, batch.lo, batch.hi, status
            )
        if batch.lo != batch.hi or not batch.response_occurrence_id:
            return False
        row = self.store.db.execute(
            "SELECT accepted_response_occurrence_id, extraction FROM turns"
            " WHERE branch_id=? AND turn_index=?",
            (batch.branch_id, batch.lo),
        ).fetchone()
        return bool(
            row
            and str(row["accepted_response_occurrence_id"] or "")
            == batch.response_occurrence_id
            and str(row["extraction"] or "") == status
        )

    def _mark_batch_extraction(
        self, batch: Batch, status: str, *, expected: str = "pending"
    ) -> bool:
        if batch.experience_mode == "chat":
            return self.store.chat_extraction_compare_and_set(
                batch.branch_id,
                batch.lo,
                batch.response_occurrence_id,
                status,
                expected=expected,
            )
        self.store.mark_extraction(
            batch.branch_id,
            batch.lo,
            batch.hi,
            status,
            expected=expected,
        )
        return self.store.extraction_range_is(
            batch.branch_id, batch.lo, batch.hi, status
        )

    # ------------------------------------------------------------------ routing (Q8)
    def endpoint_for(self, session_id: str, request_cfg=None) -> tuple[Endpoint, str, int]:
        """(endpoint, semaphore-key, max_concurrent) for an extraction batch."""
        cfg = request_cfg or self._config_for_session(session_id) or self.cfg
        if cfg.extraction.mode == "assist":
            eps = cfg.assist.endpoints
            if eps:
                name = (getattr(getattr(cfg.assist, "group_endpoints", None),   # per-group
                                "extraction", "") or "").strip()                      # override (Q8)
                e = next((x for x in eps if x.name == name), None) if name else None
                if e is None:
                    e = eps[0]                   # unset OR unknown name -> first endpoint (fail-open)
                return (Endpoint(base_url=e.base_url,
                                 model=e.model or self.models.get(session_id, ""),
                                 api_key=e.api_key,
                                 credential_ref=e.credential_ref,
                                 assist_tier=e.tier in ("nano", "small")),   # 04 SS5
                        f"assist:{e.name}", max(1, e.max_concurrent))
            if not self._warned_no_assist:
                log.warning("extraction mode 'assist' but no [assist] endpoints configured "
                            "— falling back to the main upstream (fail-open)")
                self._warned_no_assist = True
        # 2026-07-07 live repro: after a proxy restart the in-memory model hint is gone and
        # resume_pending fired extraction with model="" -> upstream 404 x ladder. The
        # configured [upstream].model is the engine-default fallback (same rule as assist).
        return (Endpoint(base_url=cfg.upstream.base_url,
                          model=self.models.get(session_id, "")
                          or getattr(cfg.upstream, "model", "") or ""), "main", 1)

    # ------------------------------------------------------------------ scheduling
    def notify(
        self, session_id: str, branch_id: str, head_turn: int, *, request_cfg=None
    ) -> None:
        """Called post-stream. Arms debounce or flushes on a full batch (03 SS3.2)."""
        cfg = request_cfg or self._config_for_session(session_id)
        if cfg is None:
            return
        if cfg.extraction.mode in ("off", "rules"):
            return
        until = self._disabled_until.get(session_id)
        if until is not None:
            if head_turn < until:
                return
            self._disabled_until.pop(session_id, None)   # re-enable (09 C2)
            self._fails.pop(session_id, None)
        pending = self.store.pending_extractions(branch_id)
        if not pending or branch_id in self._queued:
            if not pending:                   # head may be unsettled: idle timer covers it
                if request_cfg is None:
                    self._arm_debounce(session_id, branch_id, head_turn)
                else:
                    self._arm_debounce(session_id, branch_id, head_turn, cfg)
            return
        # 2026-07-04: user-set update cadence. N settled turns -> extract now
        # (1 = every turn, immediate). Below the cadence the idle debounce catches up,
        # so a walk-away never leaves state behind.
        cadence = max(1, int(getattr(cfg.extraction, "cadence_turns", 1) or 1))
        if len(pending) >= cadence:
            if request_cfg is None:
                self._flush(session_id, branch_id, head_turn)
            else:
                self._flush(session_id, branch_id, head_turn, cfg)
        else:
            if request_cfg is None:
                self._arm_debounce(session_id, branch_id, head_turn)
            else:
                self._arm_debounce(session_id, branch_id, head_turn, cfg)

    def _arm_debounce(self, session_id: str, branch_id: str, head: int, request_cfg=None) -> None:
        old = self._debounce.pop(branch_id, None)
        if old:
            old.cancel()
        cfg = request_cfg or self._config_for_session(session_id)
        if cfg is None:
            return

        async def fire():
            try:
                await asyncio.sleep(cfg.extraction.debounce_s)
                try:
                    # Idle settle (2026-07-04): without this, the newest turn waited
                    # for the NEXT message before it could ever extract.
                    self.store.settle_head(branch_id)
                except Exception:
                    pass
                self._flush(session_id, branch_id, head, cfg)
            except asyncio.CancelledError:
                pass

        self._debounce[branch_id] = asyncio.get_running_loop().create_task(fire())

    def resume_pending(self) -> int:
        """Startup recovery (2026-07-04): pending work is DB-durable, but nothing rescanned
        it after a restart — settled-but-unextracted turns sat 'pending' until the next
        request on that session. Called once from app startup."""
        n = 0
        try:
            rows = self.store.db.execute(
                "SELECT s.session_id, b.branch_id, b.head_turn FROM branches b"
                " JOIN sessions s ON s.session_id=b.session_id WHERE b.status='live'").fetchall()
            for r in rows:
                cfg = self._config_for_session(r["session_id"])
                if cfg is None:
                    continue
                if cfg.extraction.mode in ("off", "rules"):
                    continue
                if self.store.pending_extractions(r["branch_id"]) \
                        and r["branch_id"] not in self._queued:
                    self._flush(r["session_id"], r["branch_id"], r["head_turn"])
                    n += 1
            if n:
                log.info("resume: re-queued pending extraction on %d branch(es)", n)
        except Exception as exc:
            log.warning("resume_pending failed open: %s", type(exc).__name__)
        return n

    def _flush(self, session_id: str, branch_id: str, head: int, request_cfg=None) -> None:
        t = self._debounce.pop(branch_id, None)
        if t:
            t.cancel()
        pending = self.store.pending_extractions(branch_id)
        if not pending or branch_id in self._queued:
            return
        experience_mode, cfg, _binding = self._batch_context(
            session_id, request_cfg
        )
        batch = pending[:(
            1
            if experience_mode == "chat"
            else cfg.extraction.batch_max_turns
        )]
        response_occurrence_id = (
            self.store.accepted_response_occurrence_id(branch_id, batch[0])
            if experience_mode == "chat"
            else ""
        )
        if experience_mode == "chat" and not response_occurrence_id:
            return
        self._queued.add(branch_id)
        self.queue.put_nowait((PRIORITY["extraction"], next(self._seq),
                                Batch(
                                    session_id,
                                    branch_id,
                                    batch[0],
                                    batch[-1],
                                    head,
                                    cfg,
                                    experience_mode,
                                    response_occurrence_id,
                                )))
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work())

    # ------------------------------------------------------------------ the dispatcher
    async def _work(self) -> None:
        while True:
            _, _, batch = await self.queue.get()
            ep, sem_key, maxc = self.endpoint_for(batch.session_id, batch.request_cfg)
            sem = self._sems.setdefault(sem_key, asyncio.Semaphore(maxc))
            await sem.acquire()
            task = asyncio.get_running_loop().create_task(self._run_guarded(batch, ep, sem))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_guarded(self, batch: Batch, ep: Endpoint, sem: asyncio.Semaphore) -> None:
        self._inflight += 1
        try:
            await self._run_batch(batch, ep)
        except Exception as exc:      # invariant 3: a job crash never propagates
            log.warning("batch failed open: %s", type(exc).__name__)
            self._mark_batch_extraction(batch, "failed", expected="pending")
        finally:
            sem.release()
            self._inflight -= 1
            self._queued.discard(batch.branch_id)
            self.queue.task_done()
            # more settled turns may have arrived while we worked
            if self.store.pending_extractions(batch.branch_id):
                self.notify(batch.session_id, batch.branch_id, batch.head)

    async def _run_batch(self, b: Batch, ep: Endpoint) -> None:
        experience_mode, cfg, binding = self._batch_context(
            b.session_id, b.request_cfg
        )
        b.request_cfg = cfg
        b.experience_mode = b.experience_mode or experience_mode
        if b.experience_mode == "chat":
            if b.lo != b.hi:
                return
            b.response_occurrence_id = (
                b.response_occurrence_id
                or self.store.accepted_response_occurrence_id(b.branch_id, b.lo)
            )
            if not self._batch_candidate_current(b):
                return
        state = self.store.state_at(b.branch_id, b.lo - 1, reduce_state)
        snapshot = (
            compose.render_chat_packet(state, binding, cfg)
            if b.experience_mode == "chat" and not is_empty(state)
            else compose.render_header(state, cfg)
            if not is_empty(state)
            else "(nothing tracked yet)"
        )
        # CHARACTERS = the registry as known NOW (04 SS1.2) — aliases aren't time-scrubbed;
        # the snapshot above stays at lo-1 so the delta describes changes, not history.
        characters = self._characters(
            current_state(self.store, b.branch_id), b.session_id, cfg
        )
        context, exchange = self._exchange_parts(b, cfg)
        if not exchange:
            self._mark_batch_extraction(b, "skipped", expected="pending")
            return
        extract_kwargs = {"context": context}
        chat_turn_rows = []
        if b.experience_mode == "chat":
            chat_turn_rows = self.store.get_turn_texts(b.branch_id, b.lo, b.hi)
            if len(chat_turn_rows) != 1:
                raise RuntimeError("Chat extraction lost its exact accepted turn text")
            if getattr(self.ladder, "chat_role_coordinates", False):
                extract_kwargs["chat_coordinates"] = {
                    "character_actor_id": binding.character_actor_id,
                    "persona_actor_id": binding.persona_actor_id,
                    "user_text": str(chat_turn_rows[0]["user_text"] or ""),
                    "assistant_text": str(chat_turn_rows[0]["assistant_text"] or ""),
                }
        if getattr(self.ladder, "request_local_config", False):
            extract_kwargs.update({
                "request_cfg": cfg,
                "experience_mode": b.experience_mode,
            })
        delta = await self.ladder.extract(
            ep,
            snapshot,
            characters,
            b.lo,
            b.hi,
            exchange,
            **extract_kwargs,
        )
        if b.experience_mode == "chat" and not self._batch_candidate_current(b):
            log.info(
                "discarded stale Chat extraction [%d,%d] after response replacement",
                b.lo,
                b.hi,
            )
            return
        if delta is None:
            self._mark_batch_extraction(b, "failed", expected="pending")
            failure_kind = getattr(self.ladder, "last_failure_kind", None) or "unknown"
            attempts = getattr(self.ladder, "last_failure_attempts", None) or []
            log.warning(
                "extraction [%d,%d] failed: kind=%s attempts=%s",
                b.lo, b.hi, failure_kind, attempts,
            )
            if failure_kind == "transient_upstream" and self._schedule_transient_retry(b):
                return
            n = self._fails.get(b.session_id, 0) + 1
            self._fails[b.session_id] = n
            if n >= cfg.extraction.fail_autodisable_after:      # 09 C2
                self._disabled_until[b.session_id] = \
                    b.head + cfg.extraction.fail_reenable_after_turns
                log.warning("extraction auto-disabled for session %s until turn %d",
                            b.session_id, self._disabled_until[b.session_id])
            return
        self._transient_retries.pop((b.session_id, b.branch_id, b.lo, b.hi), None)
        self._fails.pop(b.session_id, None)
        delta_ops = assign_damage_effect_ids(
            delta.ops, b.branch_id, b.hi, "extraction", basis=f"{b.lo}:{b.hi}", canonical=True)
        narration = "\n".join(
            str(row["assistant_text"])
            for row in self.store.get_turn_texts(b.branch_id, b.lo, b.hi)
            if row["assistant_text"]
        )
        # Extraction ops do not carry per-op source turns.  If any turn in this bounded batch
        # has a complete code-owned realization, prose cannot safely authorize an additional
        # world mutation for the batch; actor-relative beliefs remain non-world truth.
        rejected_realization_owned: list[dict] = []
        for turn in range(b.lo, b.hi + 1):
            realization = build_narrator_realization_from_state(
                self.store.state_at(b.branch_id, turn, reduce_state)
            )
            delta_ops, rejected = filter_realization_owned_ops(
                delta_ops, realization, turn=turn,
            )
            rejected_realization_owned.extend(rejected)
            if rejected:
                break
        if rejected_realization_owned:
            log.info(
                "rejected %d deferred extraction world-change op(s): current code-owned "
                "realization is closed",
                len(rejected_realization_owned),
            )
        delta_ops, rejected_social = filter_unearned_social_ops(delta_ops, narration)
        if rejected_social:
            log.info(
                "rejected %d unearned extraction social op(s): routine cooperation is not "
                "standing",
                len(rejected_social),
            )
        delta_ops, rejected_memories = filter_unrealized_memory_ops(delta_ops, narration)
        if rejected_memories:
            log.info(
                "rejected %d unrealized extraction memory op(s): incipient handoff is not "
                "completed transfer",
                len(rejected_memories),
            )
        # Claims from the exact Player/narrator exchange are already durable before Tier-1 runs.
        # Link actor-relative extraction beliefs to one unambiguous occurrence; never reparse the
        # exchange under a false ``extraction`` ingress or duplicate its Claim Records.
        delta_ops = link_extracted_beliefs_to_claims(
            delta_ops,
            self.store.claim_records(b.branch_id, through_turn=b.hi),
            turn_lo=b.lo,
            turn_hi=b.hi,
        )
        if b.experience_mode == "chat":
            user_text = str(chat_turn_rows[0]["user_text"] or "")
            assistant_text = str(chat_turn_rows[0]["assistant_text"] or "")
            continuity_state = current_state(self.store, b.branch_id)
            partitions = chat_continuity.seal_accepted_chat_proposals(
                user_text=user_text,
                assistant_text=assistant_text,
                proposals=getattr(delta, "social_occurrence_proposals", []),
                session_id=b.session_id,
                branch_id=b.branch_id,
                turn_index=b.lo,
                character_actor_id=binding.character_actor_id,
                persona_actor_id=binding.persona_actor_id,
                response_occurrence_id=b.response_occurrence_id,
                continuity_state=continuity_state,
                character_display_name=str(
                    ((continuity_state.get("entities") or {}).get(
                        binding.character_actor_id,
                    ) or {}).get("name") or ""
                ),
                claim_records=self.store.claim_records(
                    b.branch_id,
                    through_turn=b.hi,
                ),
            )
            partitions["deferred_extraction"].extend(
                chat_continuity.scope_chat_memory_ops(
                    delta_ops,
                    character_actor_id=binding.character_actor_id,
                    persona_actor_id=binding.persona_actor_id,
                )
            )
            with self.store.transaction():
                if not self._batch_candidate_current(b):
                    log.info(
                        "discarded stale Chat extraction [%d,%d] before atomic commit",
                        b.lo,
                        b.hi,
                    )
                    return
                res = apply_partitioned_delta(
                    self.store,
                    b.session_id,
                    b.branch_id,
                    b.hi,
                    partitions,
                    cfg,
                    expected_response_occurrence_id=b.response_occurrence_id,
                )
                if res.atomic_group_failures:
                    if not self.store.chat_extraction_compare_and_set(
                        b.branch_id,
                        b.lo,
                        b.response_occurrence_id,
                        "failed",
                        expected="pending",
                    ):
                        raise RuntimeError(
                            "Chat extraction candidate changed after grouped rejection"
                        )
                    log.warning(
                        "Chat extraction [%d,%d] rejected %d incomplete atomic"
                        " social group(s)",
                        b.lo,
                        b.hi,
                        len(res.atomic_group_failures),
                    )
                    return
                memory.index_applied(
                    self.store,
                    b.session_id,
                    b.branch_id,
                    res.applied,
                    res.state,
                    require_source_receipt=True,
                )
                artifact_lineage = {
                    "turn": b.hi,
                    "lifecycle_source": "deferred_extraction",
                    "response_occurrence_id": b.response_occurrence_id,
                    "source_message_fingerprint":
                        self.store.turn_text_source_fingerprint(
                            b.branch_id,
                            b.hi,
                            "deferred_extraction",
                            require_receipt=True,
                        ),
                }
                memory.reflect(
                    self.store,
                    cfg,
                    b.session_id,
                    b.branch_id,
                    res.state,
                    artifact_lineage=artifact_lineage,
                )
                memory.precompute_recall(
                    self.store,
                    cfg,
                    b.session_id,
                    b.branch_id,
                    res.state,
                    exchange,
                    b.hi,
                    viewer_actor_id=binding.character_actor_id,
                    player_actor_id=binding.persona_actor_id,
                    experience_mode="chat",
                    artifact_lineage=artifact_lineage,
                )
                if not self.store.chat_extraction_compare_and_set(
                    b.branch_id,
                    b.lo,
                    b.response_occurrence_id,
                    "done",
                    expected="pending",
                ):
                    raise RuntimeError(
                        "Chat extraction candidate changed before atomic completion"
                    )
            log.info(
                "extracted Chat response [%d,%d]: %d applied, %d quarantined",
                b.lo,
                b.hi,
                len(res.applied),
                len(res.quarantined),
            )
            return
        res = apply_delta(self.store, b.session_id, b.branch_id, b.hi, delta_ops,
                          "extraction", cfg, turn_lo=b.lo,
                          required_extraction=(b.lo, b.hi))
        if not self.store.extraction_pending_range(b.branch_id, b.lo, b.hi):
            log.info("discarded retired extraction [%d,%d] after narration retry", b.lo, b.hi)
            return
        requeued = self._discover_from_quarantine(b, res.quarantined)
        try:                                   # RPG-3b (doc 05 §5.4): deterministic faction
            spec = getattr(cfg, "specialization", None)   # cascade — journaled rule ops
            if spec is not None and spec.name == "rpg" and res.state.get("player"):
                casc = faction_cascade_ops(res.state, res.applied,
                                           getattr(spec, "faction_cascade", 0.1))
                if casc:
                    r2 = apply_delta(self.store, b.session_id, b.branch_id, b.hi, casc,
                                     "rule", cfg,
                                     required_extraction=(b.lo, b.hi))
                    res.state = r2.state       # downstream passes see the cascaded snapshot
                    log.info("faction cascade: %d op(s) applied", len(r2.applied))
        except Exception as exc:               # never fails the batch (invariant 3)
            log.warning("faction cascade skipped: %s", type(exc).__name__)
        try:                                   # Phase 1 (plan doc 13): the combat referee —
            spec = getattr(cfg, "specialization", None)   # batch-applied clashes/harm
            if spec is not None and spec.name == "rpg" and res.state.get("player") \
                    and getattr(spec, "war_room", True):       # can settle defeats too
                wr = combat_ops(
                    res.state, res.applied,
                    prepare_intent=bool(getattr(spec, "enemy_rolls", True)))
                if wr:
                    rw = apply_delta(self.store, b.session_id, b.branch_id, b.hi, wr,
                                     "rule", cfg,
                                     required_extraction=(b.lo, b.hi))
                    res.state = rw.state
                    res.applied = list(res.applied) + rw.applied
                    log.info("combat pass: %d op(s) applied", len(rw.applied))
                if getattr(spec, "large_battle", True):    # §F: large-scale battle referee —
                    bw = battle_ops(res.state, res.applied)   # waves / settle, after the defeats
                    if bw:
                        rb = apply_delta(self.store, b.session_id, b.branch_id, b.hi, bw,
                                         "rule", cfg,
                                         required_extraction=(b.lo, b.hi))
                        res.state = rb.state
                        res.applied = list(res.applied) + rb.applied
                        log.info("battle pass: %d op(s) applied", len(rb.applied))
        except Exception as exc:               # never fails the batch (invariant 3)
            log.warning("combat pass skipped: %s", type(exc).__name__)
        try:                                   # RPG-5 (doc 10): code-awarded progression —
            spec = getattr(cfg, "specialization", None)   # XP / level-ups / defeat from
            if spec is not None and spec.name == "rpg" and res.state.get("player"):
                pro = progression_ops(res.state, res.applied,
                                      hardcore=getattr(spec, "hardcore", False))
                if pro:
                    r3 = apply_delta(self.store, b.session_id, b.branch_id, b.hi, pro,
                                     "rule", cfg,
                                     required_extraction=(b.lo, b.hi))
                    res.state = r3.state
                    log.info("progression: %d op(s) applied", len(r3.applied))
        except Exception as exc:               # never fails the batch (invariant 3)
            log.warning("progression pass skipped: %s", type(exc).__name__)
        try:                                   # Phase 2 (plan doc 13): the living-world pass —
            spec = getattr(cfg, "specialization", None)   # travel time, the idle clock,
            if spec is not None and spec.name == "rpg" and res.state.get("player") \
                    and getattr(spec, "living_world", True):   # and faction-front ticks
                lw = world_ops(res.state, res.applied,
                               clock_turns=getattr(spec, "clock_turns", 6),
                               session_id=b.session_id, branch_id=b.branch_id,
                               turn_index=b.hi)
                if lw:
                    r4 = apply_delta(self.store, b.session_id, b.branch_id, b.hi, lw,
                                     "rule", cfg,
                                     required_extraction=(b.lo, b.hi))
                    res.state = r4.state
                    log.info("living world: %d op(s) applied", len(r4.applied))
        except Exception as exc:               # never fails the batch (invariant 3)
            log.warning("living-world pass skipped: %s", type(exc).__name__)
        self.store.mark_extraction(
            b.branch_id, b.lo, b.hi, "done", expected="pending")
        if not self.store.extraction_range_is(b.branch_id, b.lo, b.hi, "done"):
            log.info("discarded retired extraction [%d,%d] before post-processing", b.lo, b.hi)
            return
        log.info("extracted [%d,%d]: %d applied, %d quarantined, %d retro-applied",
                 b.lo, b.hi, len(res.applied), len(res.quarantined), requeued)
        try:                                   # P4: memory index + reflection + Q15 recall
            memory.index_applied(self.store, b.session_id, b.branch_id, res.applied,
                                 res.state)
            memory.reflect(self.store, cfg, b.session_id, b.branch_id, res.state)
            qvec = None
            get_client = self.ladder.get_client
            hint = self.models.get(b.session_id, "")
            ep_r = assist.endpoint_for_group(cfg, "memory_reflection", hint)
            if ep_r is not None:               # 06 C: digest -> LLM prose + semantic facts
                await assist.synthesize(self.store, cfg, get_client, ep_r,
                                        b.session_id, b.branch_id)
            ep_e = assist.endpoint_for_group(cfg, "embeddings", hint)
            if ep_e is not None:               # 03 SS7: vectors + query embed, cold path
                await assist.embed_missing(self.store, cfg, get_client, ep_e,
                                           b.branch_id)
                qvec = await assist.embed_query(get_client, cfg, ep_e, exchange)
            memory.precompute_recall(self.store, cfg, b.session_id, b.branch_id,
                                     res.state, exchange, b.hi, query_vec=qvec,
                                     viewer_actor_id=next(
                                         iter(res.state.get("player", {})), ""
                                     ),
                                     experience_mode="rpg")
        except Exception as exc:               # never fails the batch (invariant 3)
            log.warning("memory pass skipped: %s", type(exc).__name__)
        try:                                   # P4: linter L1-L9 on the post-apply snapshot
            vios = self._lint_batch(b, res)
        except Exception as exc:
            vios = []
            log.warning("lint pass skipped: %s", type(exc).__name__)

        try:                                   # L10 (03 SS9): cold-path ledger-contradiction pass
            ep_n = assist.endpoint_for_group(cfg, "linter_nli",   # assist/main gated, note-
                                             self.models.get(b.session_id, ""))   # only, fail-open
            if ep_n is not None and cfg.linter.enabled:           # runs BEFORE director.stage
                # NLI hardening (2026-07-08): a slow/absent linter_nli shim must NEVER stall the
                # cold-path worker. Bound the whole probe+judge; TimeoutError falls through to the
                # fail-open except below (invariant 1 — the pass is note-only, so it just skips).
                ep_n = await asyncio.wait_for(
                    assist.resolve_endpoint(self.ladder.get_client, cfg, ep_n), timeout=8)
                rows = self.store.get_turn_texts(b.branch_id, b.hi, b.hi)
                text = rows[-1]["assistant_text"] if rows else ""
                vios = list(vios) + await asyncio.wait_for(
                    linter.ledger_contradiction_pass(
                        self.store, cfg, self.ladder.get_client, ep_n,
                        b.session_id, b.branch_id, b.hi, res.state, text or ""), timeout=12)
        except Exception as exc:               # invariant 1: fail-open so its notes can be staged
            log.warning("L10 ledger-contradiction pass skipped: %s", type(exc).__name__)
        try:                                   # P4: director beats + note staging (03 SS8)
            guard = cfg.user_guard.name or self.user_names.get(b.session_id, "")
            director.stage(self.store, cfg, b.session_id, b.branch_id, b.hi,
                           res.state, vios, user_name=guard,
                           user_aliases=tuple(cfg.user_guard.aliases))
        except Exception as exc:
            log.warning("director pass skipped: %s", type(exc).__name__)

    def _schedule_transient_retry(self, b: Batch) -> bool:
        """Requeue one persistent-429/5xx batch later without immediate endpoint hammering."""
        cfg = b.request_cfg or self.cfg
        key = (b.session_id, b.branch_id, b.lo, b.hi)
        used = self._transient_retries.get(key, 0)
        limit = max(0, int(getattr(cfg.extraction, "transient_batch_retries", 1)))
        if used >= limit:
            self._transient_retries.pop(key, None)
            return False
        self._transient_retries[key] = used + 1
        delay = max(0.0, float(getattr(cfg.extraction, "transient_retry_s", 2.0)))

        async def fire() -> None:
            await asyncio.sleep(delay)
            requeued = self._mark_batch_extraction(
                b, "pending", expected="failed"
            )
            if requeued and self._batch_candidate_current(b):
                log.info(
                    "requeued transient extraction [%d,%d] after %.1fs",
                    b.lo, b.hi, delay,
                )
                self.notify(b.session_id, b.branch_id, b.head)

        task = asyncio.get_running_loop().create_task(fire())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    def _lint_batch(self, b: Batch, res) -> list:
        """03 SS9: deterministic checks per turn against the batch's post-apply snapshot.
        klass comes from the turns table so impersonate turns never trip L9.
        Returns fresh violations across [lo,hi] (director appends the best corrective)."""
        cfg = b.request_cfg or self.cfg
        out: list = []
        if not cfg.linter.enabled:
            return out
        applied_kinds = frozenset(op.get("op", "") for op in res.applied)
        guard = cfg.user_guard.name or self.user_names.get(b.session_id, "")
        aliases = tuple(cfg.user_guard.aliases)
        klasses = {r["turn_index"]: r["klass"] for r in self.store.db.execute(
            "SELECT turn_index, klass FROM turns WHERE branch_id=? AND"
            " turn_index BETWEEN ? AND ?", (b.branch_id, b.lo, b.hi)).fetchall()}
        for row in self.store.get_turn_texts(b.branch_id, b.lo, b.hi):
            text = row["assistant_text"] or ""
            if not text.strip():
                continue
            out.extend(linter.lint_turn(
                self.store, cfg, b.session_id, b.branch_id,
                row["turn_index"], res.state, text, applied_kinds=applied_kinds,
                klass=klasses.get(row["turn_index"], "new_turn"),
                user_name=guard, user_aliases=aliases,
                user_text=row["user_text"] or ""))
        return out

    # ------------------------------------------------------------------ discovery (08 B2)
    def _discover_from_quarantine(self, b: Batch, quarantined: list[dict]) -> int:
        """Unknown-name quarantines feed the evidence counter; creation retro-applies the
        CURRENT batch's ops for that name. Fail-open: discovery errors never fail the batch."""
        try:
            cfg = b.request_cfg or self.cfg
            if not self.store.extraction_pending_range(b.branch_id, b.lo, b.hi):
                return 0
            by_name: dict[str, list[dict]] = {}
            for q in quarantined:
                m = _UNKNOWN_NAME.search(q.get("reason") or "")
                if m:
                    by_name.setdefault(m.group(1), []).append(q["op"])
            requeued = 0
            for name, ops in by_name.items():
                n = self.store.discovery_bump(b.branch_id, name, b.hi)
                status = discovery.consider(self.store, cfg, b.session_id, b.branch_id,
                                            b.hi, name, n)
                if status == "created":
                    r = apply_delta(self.store, b.session_id, b.branch_id, b.hi, ops,
                                    "extraction", cfg, turn_lo=b.lo,
                                    required_extraction=(b.lo, b.hi))
                    requeued += len(r.applied)
            return requeued
        except Exception as exc:
            log.warning("discovery failed open: %s", type(exc).__name__)
            return 0

    # ------------------------------------------------------------------ context
    def _characters(self, state: dict, session_id: str = "", request_cfg=None) -> str:
        names = []
        cfg = request_cfg or self.cfg
        guard = cfg.user_guard.name or self.user_names.get(session_id, "")
        for e in state.get("entities", {}).values():
            mark = " [USER]" if guard and e.get("name") == guard else ""
            al = f" ({', '.join(e['aliases'])})" if e.get("aliases") else ""
            names.append(f"{e.get('name', '?')}{al}{mark}")
        if guard and not any(guard in n for n in names):
            names.append(f"{guard} [USER]")
        return "; ".join(names) if names else "(none registered yet)"

    def _exchange_parts(self, b: Batch, request_cfg=None) -> tuple[str, str]:
        """(context, exchange). The batch transcript always ships WHOLE (newest data is
        never truncated); leftover intake_chars budget prepends earlier turns as
        reference-only context, newest-first accumulation so recency wins (design note
        2026-07-04: char card/genesis sets the start, chat history develops it —
        extraction reads the recent past to resolve pronouns/callbacks)."""
        rows = self.store.get_turn_texts(b.branch_id, b.lo, b.hi)
        lines = []
        for r in rows:
            if r["user_text"]:
                lines.append(r["user_text"])
            if r["assistant_text"]:
                lines.append(r["assistant_text"])
        exchange = "\n".join(lines)
        cfg = request_cfg or self.cfg
        budget = int(getattr(cfg.extraction, "intake_chars", 0) or 0)
        remaining = budget - len(exchange)
        if not exchange or remaining < 400:      # no meaningful room for extra context
            return "", exchange
        prior = self.store.get_turn_texts(b.branch_id, max(0, b.lo - 30), b.lo - 1)
        ctx_segs: list[str] = []
        for r in reversed(prior):                # walk backward from the batch
            seg = "\n".join(t for t in (r["user_text"], r["assistant_text"]) if t)
            if not seg:
                continue
            if len(seg) + 1 > remaining:
                break
            ctx_segs.append(seg)
            remaining -= len(seg) + 1
        return "\n".join(reversed(ctx_segs)), exchange

    # ------------------------------------------------------------------ lifecycle
    async def drain(self, timeout: float = 5.0) -> None:
        """Test/shutdown helper: flush debounces immediately, then wait for idle."""
        for branch_id, t in list(self._debounce.items()):
            t.cancel()
        self._debounce.clear()
        # re-derive anything that was debouncing
        rows = self.store.db.execute(
            "SELECT s.session_id, b.branch_id, b.head_turn FROM branches b"
            " JOIN sessions s ON s.session_id=b.session_id WHERE b.status='live'").fetchall()
        for r in rows:
            if self.store.pending_extractions(r["branch_id"]) and r["branch_id"] not in self._queued:
                cfg = self._config_for_session(r["session_id"])
                if cfg is None:
                    continue
                if cfg.extraction.mode in ("off", "rules"):
                    continue
                until = self._disabled_until.get(r["session_id"])   # drain respects 09 C2 too
                if until is not None and r["head_turn"] < until:
                    continue
                self._flush(r["session_id"], r["branch_id"], r["head_turn"])
        deadline = asyncio.get_running_loop().time() + timeout
        while (not self.queue.empty() or self._inflight or self._tasks) and \
                asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)

    async def stop(self) -> None:
        for t in self._debounce.values():
            t.cancel()
        for t in list(self._tasks):
            t.cancel()
        if self._worker:
            self._worker.cancel()
