"""Session engine — P1a stamped path + P1b L3 heuristic path (planning/03 SS2, 08 A.1/B1).

Stamped (L1/L2): trusts the frontend's own labels (Internals SS6).
Heuristic (L3): canonicalize -> chained hash -> LCP (binary search) -> B1 alignment on
index-0 miss -> 4-way classify. Both paths reconcile the persisted canonical transcript
(branch_msgs), which is what makes 08 S4 relink and S5 adopt-fork detectable.

L3 sees only REQUESTS: an assistant reply enters the transcript in the NEXT request's
history. A wrong classification degrades to "no injected state", never corrupted state
(invariant 3) — the safe default is always new_session.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .canon import CanonMsg, canonicalize, chain
from .config import SessionConfig
from .lcp import BranchView, Match, PrefixIndex
from .stamps import Stamp
from .store import Store
from .turn_lifecycle import TurnLifecycleError

log = logging.getLogger("aetherstate.session")


class TurnClass(str, Enum):
    new_turn = "new_turn"
    swipe = "swipe"
    edit_fork = "edit_fork"
    continue_ = "continue"
    new_session = "new_session"
    quiet = "quiet"
    impersonate = "impersonate"


@dataclass
class Resolution:
    session_id: str
    branch_id: str
    turn_index: int
    klass: TurnClass
    stamp: Optional[Stamp] = None
    duplicate: bool = False
    path: str = "stamp"          # stamp | l3
    replay_reason: str = ""      # lost_reply terminal replay | resume_reserved crash recovery
    deferred_swipe: bool = False
    deferred_swipe_keep: Optional[int] = None
    deferred_swipe_count: Optional[int] = None


class SessionEngine:
    def __init__(self, store: Store, cfg: Optional[SessionConfig] = None) -> None:
        self.store = store
        self.cfg = cfg or SessionConfig()
        self.index = PrefixIndex()
        self._dedup: dict[str, tuple[float, Resolution]] = {}
        for row in store.live_branches():    # restart recovery: rebuild index (03 SS2.3)
            rows = store.get_msgs(row["branch_id"])
            msgs = [CanonMsg(r["role"], "", r["content_hash"]) for r in rows]
            chains = [r["chain_hash"] for r in rows]
            self.index.add_branch(BranchView(row["branch_id"], row["session_id"],
                                             msgs, chains, row["last_seen"] or 0.0))

    # ------------------------------------------------------------------ entry
    def observe(
        self, stamp: Optional[Stamp], body: bytes, *, defer_swipe: bool = False
    ) -> Optional[Resolution]:
        """Proxy entry point. Caller wraps in fail-open try/except; bytes are never touched."""
        # 08 S7 dedup keys the REQUEST, and the stamp is part of the request: a swipe whose
        # payload equals the original after sentinel-strip is NOT a retry (P3 fixture catch —
        # dedup used to short-circuit before the stamp's type=swipe was ever consulted).
        stamp_key = (f"{stamp.session}|{stamp.turn}|{stamp.gen_type}|"
                     f"{stamp.parent}|{stamp.fork_pos}".encode()
                     if stamp else b"")
        key = hashlib.blake2b(body + stamp_key, digest_size=8).hexdigest()
        now = time.time()
        hit = self._dedup.get(key)
        if hit and now - hit[0] < self.cfg.dedup_window_s:      # 08 S7: network retry
            return Resolution(**{**hit[1].__dict__, "duplicate": True})
        if len(self._dedup) > 512:
            self._dedup = {k: v for k, v in self._dedup.items()
                           if now - v[0] < self.cfg.dedup_window_s}

        doc = json.loads(body)
        messages = doc.get("messages")
        if not isinstance(messages, list):
            return None
        res = (self.resolve_stamped(stamp, messages, defer_swipe=defer_swipe) if stamp
               else self.resolve_heuristic(messages, defer_swipe=defer_swipe))
        if res:
            self._dedup[key] = (now, res)
        return res

    # ---------------------------------------------------------------- stamped
    def resolve_stamped(
        self,
        stamp: Stamp,
        messages: Optional[list] = None,
        *,
        defer_swipe: bool = False,
    ) -> Optional[Resolution]:
        if stamp.gen_type == "quiet":
            return None  # ST background utility prompts never touch state (03 SS2.1)
        canon = canonicalize(messages) if messages else []

        sess = self._session_for_external(stamp, canon)
        if sess is None:
            return None
        branch = sess["active_branch"]
        sid = sess["session_id"]
        view = self._session_view(sess)
        gt = stamp.gen_type
        if defer_swipe and self._has_reserved_semantic_head(branch):
            tip_lifecycle = (
                self._exact_semantic_tip_lifecycle(view, canon)
                if gt == "normal"
                else None
            )
            if tip_lifecycle is not None and tip_lifecycle[0] == "reserved":
                return Resolution(
                    sid,
                    branch,
                    self._head(branch),
                    tip_lifecycle[1],
                    stamp,
                    replay_reason="resume_reserved",
                )
            log.warning(
                "refusing stamped transcript while semantic head is still reserved for %s",
                sid,
            )
            return None
        if gt in ("normal", "impersonate") and self._canonical_diverges(view, canon):
            if not self._exact_forward_window(stamp, branch, canon):
                log.warning("refusing divergent stamped transcript for existing session %s", sid)
                return None
        if branch not in self.index.branches:
            self.index.add_branch(view)
        self._touch_session(sid, branch)

        if gt in ("swipe", "regenerate"):
            if defer_swipe:
                keep, swipe_count = self._deferred_swipe_plan(branch, canon)
                return Resolution(
                    sid,
                    branch,
                    self._head(branch),
                    TurnClass.swipe,
                    stamp,
                    deferred_swipe=True,
                    deferred_swipe_keep=keep,
                    deferred_swipe_count=swipe_count,
                )
            self._retract_tip(branch, canon)
            self.store.bump_swipe(branch)
            return Resolution(sid, branch, self._head(branch), TurnClass.swipe, stamp)
        if gt == "continue":
            return Resolution(sid, branch, self._head(branch), TurnClass.continue_, stamp)

        tip_lifecycle = (
            self._exact_semantic_tip_lifecycle(view, canon)
            if defer_swipe and gt == "normal"
            else None
        )
        if tip_lifecycle is not None:
            lifecycle_status, original_class = tip_lifecycle
            return Resolution(
                sid,
                branch,
                self._head(branch),
                original_class,
                stamp,
                replay_reason=(
                    "lost_reply" if lifecycle_status == "committed" else "resume_reserved"
                ),
            )

        klass = TurnClass.impersonate if gt == "impersonate" else (
            TurnClass.new_session if self._head(branch) < 0 else TurnClass.new_turn)
        # 2026-07-09 turn-regression guard: the ST extension resets its turn counter to 0 on
        # chat reload / CHAT_CHANGED, so a stamped turn can fall BELOW the real head after a
        # page refresh. Trusting it verbatim landed the roll/ops on an early turn while the
        # [DIRECTIVE] rendered at the true head (meta.turn) -> the resolution silently vanished.
        # 2026-07-10 (Arinvale): forward jumps are no longer honored either — the extension's
        # counter also ticks on continues / stopped generations that never reach the proxy,
        # which SKIPPED indices (live session recorded turns 1,3,4,5) and desynced every
        # turn-arithmetic surface (cooldown maturation, regen, mastery caps). Once a branch
        # HAS a head, the next turn is EXACTLY head+1 — a forward-jumped OR reset counter never
        # moves it. The stamp only ESTABLISHES the base on a brand-new branch (normally 1: ST's
        # counter is 0 then ticks to 1 on the first generation, and genesis pre-seeds turn 0);
        # thereafter stamp.turn is a dedup/debug hint only. (The extension no longer counts
        # continues — build 2026-07-10.)
        _head = self._head(branch)
        turn = (stamp.turn if (_head < 0 and stamp.turn is not None and stamp.turn >= 0)
                else _head + 1)
        self.store.record_turn(branch, turn, klass.value, gt)
        if canon:
            self._append_tail(branch, self._unseen_tail(branch, canon), record_turns=False)
        return Resolution(sid, branch, turn, klass, stamp)

    def _unseen_tail(self, branch_id: str, canon: list[CanonMsg]) -> list[CanonMsg]:
        """Return only transcript content that can be safely synced to a stamped branch."""
        view = self.index.branches.get(branch_id)
        if view is None:
            return canon
        L = 0
        for a, b in zip(view.msgs, canon):
            if a.role != b.role or a.content_hash != b.content_hash:
                break
            L += 1
        if L == len(view.msgs):
            return canon[L:]
        if L:
            return []  # ambiguous shared-prefix rewrite: resolve the turn, preserve stored history
        for overlap in range(min(len(view.msgs), len(canon)), 0, -1):
            suffix = view.msgs[-overlap:]
            prefix = canon[:overlap]
            if all(
                    left.role == right.role and left.content_hash == right.content_hash
                    for left, right in zip(suffix, prefix)):
                return canon[overlap:]
        return canon  # disjoint bounded window: the exact forward stamp owns its new content

    @staticmethod
    def _canonical_diverges(view: BranchView, canon: list[CanonMsg]) -> bool:
        """True when two canonical transcripts disagree inside their shared span."""
        return any(
            left.role != right.role or left.content_hash != right.content_hash
            for left, right in zip(view.msgs, canon)
        )

    def _exact_forward_window(self, stamp: Stamp, branch_id: str,
                              canon: list[CanonMsg]) -> bool:
        """Accept one bounded stamped window only for the exact next normal Player turn."""
        if stamp.gen_type != "normal" or stamp.turn is None or not canon:
            return False
        if canon[-1].role not in ("user", "text"):
            return False
        head = self._head(branch_id)
        return head >= 0 and not self._has_reserved_semantic_head(branch_id) \
            and stamp.turn == head + 1

    def _has_reserved_semantic_head(self, branch_id: str) -> bool:
        """True only when the branch tip is fenced but has no terminal artifact yet."""
        row = self.store.db.execute(
            "SELECT l.status AS lifecycle_status, a.status AS attempt_status"
            " FROM branches b"
            " JOIN semantic_turn_lifecycles l ON l.branch_id=b.branch_id"
            "  AND l.turn_index=b.head_turn"
            " JOIN semantic_turn_attempts a ON a.lifecycle_key=l.lifecycle_key"
            "  AND a.attempt_index=COALESCE(l.active_attempt_index, 0)"
            " WHERE b.branch_id=?",
            (branch_id,),
        ).fetchone()
        return row is not None \
            and row["lifecycle_status"] == "reserved" \
            and row["attempt_status"] == "reserved"

    def _session_view(self, session) -> BranchView:
        """Return a persisted session's active view without mutating the prefix index."""
        branch = session["active_branch"]
        existing = self.index.branches.get(branch)
        if existing is not None:
            return existing
        rows = self.store.get_msgs(branch)
        msgs = [CanonMsg(row["role"], "", row["content_hash"]) for row in rows]
        chains = [row["chain_hash"] for row in rows]
        return BranchView(branch, session["session_id"], msgs, chains,
                          session["last_seen"] or 0.0)

    def _exact_semantic_tip_lifecycle(
        self, view: BranchView, canon: list[CanonMsg]
    ) -> Optional[tuple[str, TurnClass]]:
        """Recognize a byte-stable Player tip owned by one reserved or committed lifecycle.

        This is deliberately lifecycle-backed.  Repeating the same words after a delivered
        assistant reply remains a new Player action because that request contains the assistant
        message followed by a new Player tip.  An exact transcript that still ends at the same
        Player tip means the frontend did not retain the reply, even when the server already
        recorded that it emitted the artifact.  A crash after fresh T0/T1 identity commit but before
        T1 settlement leaves a reserved lifecycle; the exact retry resumes that same turn instead of
        advancing to T2.  Server-side assistant text/hash is not a client acknowledgement and
        therefore cannot turn either case into a second settlement.
        """
        if not getattr(self.cfg, "reserve_lost_turns", True):
            return None
        if not canon or canon[-1].role not in ("user", "text") \
                or len(canon) != len(view.msgs):
            return None
        if any(
            left.role != right.role or left.content_hash != right.content_hash
            for left, right in zip(view.msgs, canon)
        ):
            return None
        head = self._head(view.branch_id)
        if head < 0:
            return None
        row = self.store.db.execute(
            "SELECT t.user_hash, t.klass, l.lifecycle_key, l.status AS lifecycle_status,"
            " a.status AS attempt_status"
            " FROM turns t"
            " JOIN semantic_turn_lifecycles l ON l.branch_id=t.branch_id"
            "  AND l.turn_index=t.turn_index"
            " JOIN semantic_turn_attempts a ON a.lifecycle_key=l.lifecycle_key"
            "  AND a.attempt_index=0"
            " WHERE t.branch_id=? AND t.turn_index=?",
            (view.branch_id, head),
        ).fetchone()
        if row is None or not row["lifecycle_key"] \
                or row["user_hash"] != canon[-1].content_hash:
            return None
        lifecycle_status = str(row["lifecycle_status"])
        attempt_status = str(row["attempt_status"])
        if lifecycle_status == "reserved" and attempt_status != "reserved":
            return None
        if lifecycle_status == "committed" and attempt_status not in {
            "fallback_ready", "fallback_final", "accepted"
        }:
            return None
        if lifecycle_status not in {"reserved", "committed"}:
            return None
        try:
            original_class = TurnClass(str(row["klass"]))
        except ValueError:
            return None
        return lifecycle_status, original_class

    def _deferred_swipe_plan(
        self, branch_id: str, canon: list[CanonMsg]
    ) -> tuple[int, int]:
        """Freeze the exact transcript/count mutation without applying it yet."""
        view = self.index.branches.get(branch_id)
        if view is None:
            rows = self.store.get_msgs(branch_id)
            keep = len(rows)
            if rows and rows[-1]["role"] == "assistant" and (
                    not canon or rows[-1]["content_hash"] != canon[-1].content_hash):
                keep -= 1
        else:
            keep = len(view.msgs)
            if view.msgs and view.msgs[-1].role == "assistant" and (
                    not canon or view.msgs[-1].content_hash != canon[-1].content_hash):
                keep -= 1
        row = self.store.db.execute(
            "SELECT t.swipe_count FROM turns t JOIN branches b ON b.branch_id=t.branch_id"
            " AND b.head_turn=t.turn_index WHERE t.branch_id=?",
            (branch_id,),
        ).fetchone()
        if row is None:
            raise ValueError("semantic swipe has no recorded turn")
        return keep, int(row["swipe_count"] or 0)

    def apply_deferred_swipe_db(self, res: Resolution) -> None:
        """Apply a gated swipe's DB changes inside the lifecycle proof transaction.

        The prefix index is intentionally updated only after that transaction commits.  If proof,
        CAS, or persistence fails, SQLite rolls every mutation here back and the prior assistant
        tip/hash/count remains authoritative.
        """
        if res.klass is not TurnClass.swipe or not res.deferred_swipe \
                or not isinstance(res.deferred_swipe_keep, int) \
                or isinstance(res.deferred_swipe_keep, bool) \
                or not isinstance(res.deferred_swipe_count, int) \
                or isinstance(res.deferred_swipe_count, bool):
            raise ValueError("semantic swipe has no deferred mutation plan")
        keep = int(res.deferred_swipe_keep)
        before = int(res.deferred_swipe_count)
        if keep < 0 or before < 0:
            raise ValueError("semantic swipe mutation plan is invalid")
        with self.store.transaction():
            branch = self.store.db.execute(
                "SELECT head_turn, status FROM branches WHERE branch_id=?",
                (res.branch_id,),
            ).fetchone()
            turn = self.store.db.execute(
                "SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                (res.branch_id, res.turn_index),
            ).fetchone()
            if branch is None or branch["status"] != "live" \
                    or int(branch["head_turn"]) != int(res.turn_index) or turn is None:
                raise ValueError("semantic swipe branch advanced before terminal proof")
            current = int(turn["swipe_count"] or 0)
            if current != before:
                raise ValueError("semantic swipe count changed before terminal proof")
            self.store.db.execute(
                "DELETE FROM branch_msgs WHERE branch_id=? AND pos>=?",
                (res.branch_id, keep),
            )
            self.store.retract_extraction_at(res.branch_id, res.turn_index)
            updated = self.store.db.execute(
                "UPDATE turns SET swipe_count=?, assistant_hash=NULL"
                " WHERE branch_id=? AND turn_index=? AND swipe_count=?",
                (before + 1, res.branch_id, res.turn_index, before),
            )
            if updated.rowcount != 1:
                raise ValueError("semantic swipe lost its count CAS")

    def finalize_deferred_swipe_index(self, res: Resolution) -> None:
        """Make the in-memory prefix view match a terminal deferred swipe."""
        if not res.deferred_swipe \
                or not isinstance(res.deferred_swipe_keep, int) \
                or isinstance(res.deferred_swipe_keep, bool):
            return
        view = self.index.branches.get(res.branch_id)
        if view is not None and len(view.msgs) > int(res.deferred_swipe_keep):
            self.index.truncate(res.branch_id, int(res.deferred_swipe_keep))

    def _explicit_branch_session(self, stamp: Stamp, canon: list[CanonMsg]):
        """Create one child session only after its parent prefix is independently proven."""
        parent_external = stamp.parent
        fork_pos = stamp.fork_pos
        if not parent_external or not isinstance(fork_pos, int) \
                or isinstance(fork_pos, bool) or fork_pos < 0:
            return None
        if parent_external == stamp.session or len(canon) <= fork_pos:
            return None
        if canon[fork_pos].role not in ("user", "text"):
            return None

        parent = self.store.db.execute(
            "SELECT * FROM sessions WHERE external_id=?", (parent_external,)).fetchone()
        if parent is None:
            return None
        parent_view = self._session_view(parent)
        if fork_pos > len(parent_view.msgs):
            return None
        shared = fork_pos
        if shared <= 0:
            return None
        if any(
                parent_view.msgs[pos].role != canon[pos].role
                or parent_view.msgs[pos].content_hash != canon[pos].content_hash
                for pos in range(shared)):
            return None
        with self.store.apply_guard():
            if not self._fork_prefix_ready(parent_view, fork_pos):
                return None
            if parent_view.branch_id not in self.index.branches:
                self.index.add_branch(parent_view)
            sid, empty_branch = self.store.create_session(
                external_id=stamp.session, frontend="stamped-explicit-branch")
            if not self.store.inherit_session_settings(parent["session_id"], sid):
                return None
            self._fork(parent_view, fork_pos, new_session_id=sid, kill_source=False,
                       discard_empty_branch=empty_branch)
        log.info("explicit branch from session %s at canonical position %d",
                 parent["session_id"], fork_pos)
        return self.store.db.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()

    def _session_for_external(self, stamp: Stamp, canon: list[CanonMsg]):
        external_id = stamp.session
        row = self.store.db.execute("SELECT * FROM sessions WHERE external_id=?",
                                    (external_id,)).fetchone()
        if row:
            return row
        has_lineage = stamp.parent is not None or stamp.fork_pos is not None
        if has_lineage:
            return self._explicit_branch_session(stamp, canon)
        # Never-seen L1 id: weigh L3 chain evidence before minting (08 S4/S5).
        match = self._match(canon) if len(canon) >= self.cfg.adopt_min_lcp else None
        if match and match.lcp_branch >= self.cfg.adopt_min_lcp:
            view = match.branch
            if match.lcp_branch >= len(view.msgs):     # full-prefix match -> chat rename (S4)
                with self.store.apply_guard():
                    if not self._fork_prefix_ready(view, len(view.msgs)):
                        return None
                    log.info("relinking external id %s to session %s (S4)",
                             external_id, view.session_id)
                    self.store.relink_external(view.session_id, external_id)
            else:                                      # divergence -> adopt-fork (S5)
                log.info("adopt-fork from session %s at %d (S5)",
                         view.session_id, match.lcp_branch)
                with self.store.apply_guard():
                    if not self._fork_prefix_ready(view, match.lcp_branch):
                        return None
                    sid, empty_branch = self.store.create_session(
                        external_id=external_id, frontend="stamped-adopted")
                    self._fork(view, match.lcp_branch, new_session_id=sid, kill_source=False,
                               discard_empty_branch=empty_branch)
                return self.store.db.execute("SELECT * FROM sessions WHERE session_id=?",
                                             (sid,)).fetchone()
            return self.store.db.execute("SELECT * FROM sessions WHERE session_id=?",
                                         (view.session_id,)).fetchone()
        sess = self.store.get_or_create_session(external_id)
        return sess

    # -------------------------------------------------------------- heuristic
    def resolve_heuristic(
        self, messages: list, *, defer_swipe: bool = False
    ) -> Optional[Resolution]:
        canon = canonicalize(messages)
        if not canon:
            return None
        match = self._match(canon)

        if match is None or self._too_shallow(match):
            return self._new_session(canon)

        view, L = match.branch, match.lcp_branch
        n, m = len(view.msgs), len(canon)
        end = match.offset + m
        term = canon[-1].role
        sid, branch = view.session_id, view.branch_id
        if defer_swipe and self._has_reserved_semantic_head(branch):
            tip_lifecycle = self._exact_semantic_tip_lifecycle(view, canon)
            if tip_lifecycle is not None and tip_lifecycle[0] == "reserved":
                return Resolution(
                    sid,
                    branch,
                    self._head(branch),
                    tip_lifecycle[1],
                    path="l3",
                    replay_reason="resume_reserved",
                )
            log.warning(
                "refusing heuristic transcript while semantic head is still reserved for %s",
                sid,
            )
            return None
        if L == n and end > n:                                   # unseen tail
            self._touch_session(sid, branch)
            if term == "assistant":                              # partial tail extended
                return Resolution(sid, branch, self._head(branch),
                                  TurnClass.continue_, path="l3")
            return self._new_turn(view, canon, match)            # case 1
        if L == end:                                             # incoming fully matched
            if end == n and term != "assistant":                 # regen of in-flight tail
                self._touch_session(sid, branch)
                tip_lifecycle = (
                    self._exact_semantic_tip_lifecycle(view, canon) if defer_swipe else None
                )
                if tip_lifecycle is not None:
                    lifecycle_status, original_class = tip_lifecycle
                    return Resolution(
                        sid,
                        branch,
                        self._head(branch),
                        original_class,
                        path="l3",
                        replay_reason=(
                            "lost_reply"
                            if lifecycle_status == "committed"
                            else "resume_reserved"
                        ),
                    )
                if defer_swipe:
                    keep, swipe_count = self._deferred_swipe_plan(branch, canon)
                    return Resolution(
                        sid,
                        branch,
                        self._head(branch),
                        TurnClass.swipe,
                        path="l3",
                        deferred_swipe=True,
                        deferred_swipe_keep=keep,
                        deferred_swipe_count=swipe_count,
                    )
                self.store.bump_swipe(branch)
                return Resolution(sid, branch, self._head(branch), TurnClass.swipe,
                                  path="l3")
            if end == n - 1 and view.msgs[n - 1].role == "assistant" \
                    and term != "assistant":                     # case 2: replace tip
                self._touch_session(sid, branch)
                if defer_swipe:
                    keep, swipe_count = self._deferred_swipe_plan(branch, canon)
                    return Resolution(
                        sid,
                        branch,
                        self._head(branch),
                        TurnClass.swipe,
                        path="l3",
                        deferred_swipe=True,
                        deferred_swipe_keep=keep,
                        deferred_swipe_count=swipe_count,
                    )
                self._retract_tip(branch, canon)
                self.store.bump_swipe(branch)
                return Resolution(sid, branch, self._head(branch), TurnClass.swipe,
                                  path="l3")
            if end == n and term == "assistant":
                self._touch_session(sid, branch)
                return Resolution(sid, branch, self._head(branch),
                                  TurnClass.continue_, path="l3")
            # strict prefix, several msgs gone: truncate-fork (08 S6 without hint)
            return self._edit_fork(view, canon, match, at=end)
        return self._edit_fork(view, canon, match, at=L)         # case 3

    # ---------------------------------------------------------------- actions
    def _match(self, canon: list[CanonMsg]) -> Optional[Match]:
        if not canon:
            return None
        match = self.index.longest_prefix(chain(canon))
        if match is None:                        # index-0 miss: window slide? (08 B1/S3)
            match = self.index.align(canon, k=self.cfg.align_k)
        return match

    def _too_shallow(self, match: Match) -> bool:
        """min_anchor guards against trivial-overlap merges ('hi'), but a match covering the
        ENTIRE stored branch is a true continuation of a short session — accept it."""
        return (match.matched < self.cfg.min_anchor_msgs
                and match.lcp_branch < len(match.branch.msgs))

    def _new_session(self, canon: list[CanonMsg]) -> Optional[Resolution]:
        users = [c for c in canon if c.role == "user"]
        if not users and not all(c.role == "text" for c in canon):
            return None    # 08 S1: zero-user-msg payloads (greeting swipes) are ephemeral
        anchor = (users[0] if users else canon[0]).content_hash
        sid, bid = self.store.create_session(anchor_hash=anchor)
        last_seen = self.store.touch_session(sid)
        self.index.add_branch(BranchView(bid, sid, [], [], last_seen))
        turn = self._append_tail(bid, canon, record_turns=True)
        return Resolution(sid, bid, turn, TurnClass.new_session, path="l3")

    def _new_turn(self, view: BranchView, canon: list[CanonMsg], match: Match) -> Resolution:
        tail = canon[len(view.msgs) - match.offset:]
        turn = self._append_tail(view.branch_id, tail, record_turns=True)
        return Resolution(view.session_id, view.branch_id, turn, TurnClass.new_turn,
                          path="l3")

    def _edit_fork(self, view: BranchView, canon: list[CanonMsg], match: Match,
                   at: int) -> Optional[Resolution]:
        if not self._fork_prefix_ready(view, at):
            return None
        new_branch = self._fork(view, at, kill_source=True)
        tail = canon[max(0, at - match.offset):]
        turn = self._append_tail(new_branch, tail, record_turns=True)
        return Resolution(view.session_id, new_branch, turn, TurnClass.edit_fork,
                          path="l3")

    def _fork(self, view: BranchView, at: int, new_session_id: Optional[str] = None,
              kill_source: bool = False,
              discard_empty_branch: Optional[str] = None) -> str:
        # Copy the prefix BEFORE drop_branch: dropping truncates the view in place.
        prefix_msgs = list(view.msgs[:at])
        prefix_chains = list(view.chains[:at])
        shared_turns = sum(1 for c in prefix_msgs if c.role in ("user", "text"))
        # L3 turns start at zero, while SillyTavern stamps start at one. Resolve the
        # ordinal through the source branch instead of assuming either numbering base.
        fork_turn = self.store.turn_for_message_ordinal(view.branch_id, shared_turns)
        self.store.turn_lifecycle.assert_fork_prefix_ready(
            view.branch_id, at, fork_turn
        )
        bid = self.store.fork_branch(view.branch_id, at, fork_turn,
                                     new_session_id=new_session_id,
                                     kill_source=kill_source,
                                     discard_empty_branch=discard_empty_branch)
        if kill_source:
            self.index.drop_branch(view.branch_id)
        target_session = new_session_id or view.session_id
        last_seen = self.store.touch_session(target_session)
        self.index.add_branch(BranchView(
            bid, target_session, prefix_msgs, prefix_chains, last_seen))
        return bid

    def _fork_prefix_ready(self, view: BranchView, at: int) -> bool:
        """Preflight semantic prefix terminality before any child session or branch is minted."""
        prefix_msgs = list(view.msgs[:at])
        shared_turns = sum(1 for message in prefix_msgs if message.role in ("user", "text"))
        fork_turn = self.store.turn_for_message_ordinal(view.branch_id, shared_turns)
        try:
            self.store.turn_lifecycle.assert_fork_prefix_ready(
                view.branch_id, at, fork_turn
            )
        except TurnLifecycleError:
            log.warning(
                "refusing fork while inherited semantic prefix is not terminal for %s",
                view.session_id,
            )
            return False
        return True

    def _append_tail(self, branch_id: str, tail: list[CanonMsg],
                     record_turns: bool) -> int:
        """Append unseen canonical msgs; turn index = running count of user msgs - 1."""
        view = self.index.branches[branch_id]
        start = len(view.msgs)
        self.index.append(branch_id, tail)
        rows = [(c.role, c.content_hash, self.index.branches[branch_id].chains[start + i])
                for i, c in enumerate(tail)]
        self.store.append_msgs(branch_id, start, rows)
        turn = self._head(branch_id)
        if record_turns:
            base = sum(1 for c in view.msgs[:start] if c.role in ("user", "text"))
            count = base
            for c in tail:
                if c.role in ("user", "text"):
                    self.store.record_turn(branch_id, count, "new_turn", "l3")
                    count += 1
            turn = count - 1 if count > base else self._head(branch_id)
        return turn

    def _retract_tip(self, branch_id: str, canon: list[CanonMsg]) -> None:
        """Swipe with a RECORDED assistant tip: remove it from transcript + index."""
        view = self.index.branches.get(branch_id)
        if view and view.msgs and view.msgs[-1].role == "assistant" and (
                not canon or canon[-1].content_hash != view.msgs[-1].content_hash):
            keep = len(view.msgs) - 1
            self.index.truncate(branch_id, keep)
            self.store.truncate_msgs(branch_id, keep)

    def _head(self, branch_id: str) -> int:
        row = self.store.db.execute(
            "SELECT head_turn FROM branches WHERE branch_id=?", (branch_id,)).fetchone()
        return row["head_turn"] if row else -1

    def _touch_session(self, session_id: str, branch_id: str) -> None:
        """Keep durable session recency and its live prefix-index projection byte-identical."""
        self.index.touch(branch_id, self.store.touch_session(session_id))
