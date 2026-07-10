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
    def observe(self, stamp: Optional[Stamp], body: bytes) -> Optional[Resolution]:
        """Proxy entry point. Caller wraps in fail-open try/except; bytes are never touched."""
        # 08 S7 dedup keys the REQUEST, and the stamp is part of the request: a swipe whose
        # payload equals the original after sentinel-strip is NOT a retry (P3 fixture catch —
        # dedup used to short-circuit before the stamp's type=swipe was ever consulted).
        stamp_key = (f"{stamp.session}|{stamp.turn}|{stamp.gen_type}".encode()
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
        res = (self.resolve_stamped(stamp, messages) if stamp
               else self.resolve_heuristic(messages))
        if res:
            self._dedup[key] = (now, res)
        return res

    # ---------------------------------------------------------------- stamped
    def resolve_stamped(self, stamp: Stamp, messages: Optional[list] = None,
                        ) -> Optional[Resolution]:
        if stamp.gen_type == "quiet":
            return None  # ST background utility prompts never touch state (03 SS2.1)
        canon = canonicalize(messages) if messages else []

        sess = self._session_for_external(stamp.session, canon)
        branch = sess["active_branch"]
        sid = sess["session_id"]
        self.store.touch_session(sid)
        self.index.touch(branch, time.time())
        gt = stamp.gen_type

        if gt in ("swipe", "regenerate"):
            self._retract_tip(branch, canon)
            self.store.bump_swipe(branch)
            return Resolution(sid, branch, self._head(branch), TurnClass.swipe, stamp)
        if gt == "continue":
            return Resolution(sid, branch, self._head(branch), TurnClass.continue_, stamp)

        klass = TurnClass.impersonate if gt == "impersonate" else (
            TurnClass.new_session if self._head(branch) < 0 else TurnClass.new_turn)
        # 2026-07-09 turn-regression guard: the ST extension resets its turn counter to 0 on
        # chat reload / CHAT_CHANGED, so a stamped turn can fall BELOW the real head after a
        # page refresh. Trusting it verbatim landed the roll/ops on an early turn while the
        # [DIRECTIVE] rendered at the true head (meta.turn) -> the resolution silently vanished.
        # 2026-07-10 (Eranmor): forward jumps are no longer honored either — the extension's
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
        """Direct-compare LCP against one branch (stamped path transcript sync)."""
        view = self.index.branches.get(branch_id)
        if view is None:
            return canon
        L = 0
        for a, b in zip(view.msgs, canon):
            if a.content_hash != b.content_hash:
                break
            L += 1
        return canon[L:] if L == len(view.msgs) else []   # divergence: stamps rule; skip sync

    def _session_for_external(self, external_id: str, canon: list[CanonMsg]):
        row = self.store.db.execute("SELECT * FROM sessions WHERE external_id=?",
                                    (external_id,)).fetchone()
        if row:
            if row["active_branch"] not in self.index.branches:
                self.index.add_branch(BranchView(row["active_branch"], row["session_id"],
                                                 [], [], time.time()))
            return row
        # Never-seen L1 id: weigh L3 chain evidence before minting (08 S4/S5).
        match = self._match(canon) if len(canon) >= self.cfg.adopt_min_lcp else None
        if match and match.lcp_branch >= self.cfg.adopt_min_lcp:
            view = match.branch
            if match.lcp_branch >= len(view.msgs):     # full-prefix match -> chat rename (S4)
                log.info("relinking external id %s to session %s (S4)",
                         external_id, view.session_id)
                self.store.relink_external(view.session_id, external_id)
            else:                                      # divergence -> adopt-fork (S5)
                log.info("adopt-fork from session %s at %d (S5)",
                         view.session_id, match.lcp_branch)
                sid, _ = self.store.create_session(external_id=external_id,
                                                   frontend="stamped-adopted")
                self._fork(view, match.lcp_branch, new_session_id=sid, kill_source=False)
                return self.store.db.execute("SELECT * FROM sessions WHERE session_id=?",
                                             (sid,)).fetchone()
            return self.store.db.execute("SELECT * FROM sessions WHERE session_id=?",
                                         (view.session_id,)).fetchone()
        sess = self.store.get_or_create_session(external_id)
        if sess["active_branch"] not in self.index.branches:
            self.index.add_branch(BranchView(sess["active_branch"], sess["session_id"],
                                             [], [], time.time()))
        return sess

    # -------------------------------------------------------------- heuristic
    def resolve_heuristic(self, messages: list) -> Optional[Resolution]:
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
        self.store.touch_session(sid)
        self.index.touch(branch, time.time())

        if L == n and end > n:                                   # unseen tail
            if term == "assistant":                              # partial tail extended
                return Resolution(sid, branch, self._head(branch),
                                  TurnClass.continue_, path="l3")
            return self._new_turn(view, canon, match)            # case 1
        if L == end:                                             # incoming fully matched
            if end == n and term != "assistant":                 # regen of in-flight tail
                self.store.bump_swipe(branch)
                return Resolution(sid, branch, self._head(branch), TurnClass.swipe,
                                  path="l3")
            if end == n - 1 and view.msgs[n - 1].role == "assistant" \
                    and term != "assistant":                     # case 2: replace tip
                self._retract_tip(branch, canon)
                self.store.bump_swipe(branch)
                return Resolution(sid, branch, self._head(branch), TurnClass.swipe,
                                  path="l3")
            if end == n and term == "assistant":
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
        self.index.add_branch(BranchView(bid, sid, [], [], time.time()))
        turn = self._append_tail(bid, canon, record_turns=True)
        return Resolution(sid, bid, turn, TurnClass.new_session, path="l3")

    def _new_turn(self, view: BranchView, canon: list[CanonMsg], match: Match) -> Resolution:
        tail = canon[len(view.msgs) - match.offset:]
        turn = self._append_tail(view.branch_id, tail, record_turns=True)
        return Resolution(view.session_id, view.branch_id, turn, TurnClass.new_turn,
                          path="l3")

    def _edit_fork(self, view: BranchView, canon: list[CanonMsg], match: Match,
                   at: int) -> Resolution:
        new_branch = self._fork(view, at, kill_source=True)
        tail = canon[max(0, at - match.offset):]
        turn = self._append_tail(new_branch, tail, record_turns=True)
        return Resolution(view.session_id, new_branch, turn, TurnClass.edit_fork,
                          path="l3")

    def _fork(self, view: BranchView, at: int, new_session_id: Optional[str] = None,
              kill_source: bool = False) -> str:
        # Copy the prefix BEFORE drop_branch: dropping truncates the view in place.
        prefix_msgs = list(view.msgs[:at])
        prefix_chains = list(view.chains[:at])
        fork_turn = sum(1 for c in prefix_msgs if c.role in ("user", "text")) - 1
        bid = self.store.fork_branch(view.branch_id, at, fork_turn,
                                     new_session_id=new_session_id,
                                     kill_source=kill_source)
        if kill_source:
            self.index.drop_branch(view.branch_id)
        self.index.add_branch(BranchView(
            bid, new_session_id or view.session_id,
            prefix_msgs, prefix_chains, time.time()))
        return bid

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
