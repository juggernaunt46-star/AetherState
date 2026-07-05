"""In-memory LCP + B1 alignment index over live-branch canonical transcripts (03 SS2.2, 08 B1).

Two lookups, both O(1)-per-probe dicts:
- by_chain: chain_hash -> branch ids whose prefix [0..i] hashes to it. Chain hashes are
  prefix-commitments, so membership is monotone in depth -> LCP by binary search.
- by_content: content_hash -> [(branch_id, pos)]. When LCP fails at index 0 (ST trimmed the
  oldest messages — the #1 real L3 breaker), locate incoming[0] mid-sequence, verify k
  consecutive content matches, and classify on the aligned tail (08 B1/S3).

Rebuilt from the Store on startup (restart recovery, 03 SS2.3); mutated in lockstep with it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .canon import CanonMsg, chain


@dataclass
class BranchView:
    branch_id: str
    session_id: str
    msgs: list[CanonMsg]        # canonical transcript
    chains: list[str]           # chain_hash per position
    last_seen: float = 0.0


@dataclass
class Match:
    branch: BranchView
    offset: int                 # branch position that aligns with incoming[0] (0 unless slid)
    matched: int                # consecutive canonical msgs matched from incoming[0]

    @property
    def lcp_branch(self) -> int:      # match end in BRANCH coordinates
        return self.offset + self.matched


class PrefixIndex:
    def __init__(self) -> None:
        self.branches: dict[str, BranchView] = {}
        self.by_chain: dict[str, set[str]] = {}
        self.by_content: dict[str, list[tuple[str, int]]] = {}

    # -- mutation (keep in lockstep with Store) --------------------------------
    def add_branch(self, view: BranchView) -> None:
        self.branches[view.branch_id] = view
        for pos in range(len(view.msgs)):
            self._index_pos(view, pos)

    def append(self, branch_id: str, msgs: list[CanonMsg]) -> None:
        view = self.branches[branch_id]
        base = view.msgs + msgs
        view.chains = view.chains[:len(view.msgs)] + chain(base)[len(view.msgs):]
        view.msgs = base
        for pos in range(len(base) - len(msgs), len(base)):
            self._index_pos(view, pos)

    def truncate(self, branch_id: str, keep: int) -> None:
        view = self.branches[branch_id]
        for pos in range(keep, len(view.msgs)):
            self.by_chain.get(view.chains[pos], set()).discard(branch_id)
            entry = (branch_id, pos)
            lst = self.by_content.get(view.msgs[pos].content_hash, [])
            if entry in lst:
                lst.remove(entry)
        view.msgs, view.chains = view.msgs[:keep], view.chains[:keep]

    def drop_branch(self, branch_id: str) -> None:
        if branch_id in self.branches:
            self.truncate(branch_id, 0)
            del self.branches[branch_id]

    def touch(self, branch_id: str, ts: float) -> None:
        if branch_id in self.branches:
            self.branches[branch_id].last_seen = ts

    def _index_pos(self, view: BranchView, pos: int) -> None:
        self.by_chain.setdefault(view.chains[pos], set()).add(view.branch_id)
        self.by_content.setdefault(view.msgs[pos].content_hash, []).append(
            (view.branch_id, pos))

    # -- lookup ----------------------------------------------------------------
    def longest_prefix(self, hashes: list[str]) -> Match | None:
        """Deepest i with hashes[i] indexed. Ties (identical openings, 08 S2) -> recency."""
        if not hashes or not self.by_chain.get(hashes[0]):
            return None    # truthiness, not key presence: truncation leaves empty sets behind
        lo, hi = 0, len(hashes) - 1        # invariant: hashes[lo] is a hit
        while lo < hi:                     # monotone membership -> binary search
            mid = (lo + hi + 1) // 2
            if self.by_chain.get(hashes[mid]):
                lo = mid
            else:
                hi = mid - 1
        candidates = [self.branches[b] for b in self.by_chain[hashes[lo]]
                      if b in self.branches]
        if not candidates:
            return None
        best = max(candidates, key=lambda v: v.last_seen)
        return Match(branch=best, offset=0, matched=lo + 1)

    def align(self, incoming: list[CanonMsg], k: int = 3) -> Match | None:
        """08 B1: locate incoming[0] by content hash mid-branch; verify k consecutive matches."""
        if not incoming:
            return None
        need = min(k, len(incoming))
        best: Match | None = None
        for branch_id, pos in self.by_content.get(incoming[0].content_hash, []):
            view = self.branches.get(branch_id)
            if view is None:
                continue
            matched = 0
            for j, msg in enumerate(incoming):
                if pos + j >= len(view.msgs) or \
                        view.msgs[pos + j].content_hash != msg.content_hash:
                    break
                matched += 1
            if matched < need:
                continue
            cand = Match(branch=view, offset=pos, matched=matched)
            if best is None or cand.matched > best.matched or (
                    cand.matched == best.matched and
                    cand.branch.last_seen > best.branch.last_seen):
                best = cand
        return best
