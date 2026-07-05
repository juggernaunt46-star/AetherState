"""Memory tiers + retrieval + rules-mode reflection (02 SS10, 03 SS7, 08 L2, Proxy SS4).

The journal is the source of truth: reducer state["memories"] stays replay-deterministic.
THIS module maintains the retrieval INDEX (store.memories) on the cold path — access
bumps and consolidation are retrieval metadata, never journaled ops (losing them on
rollback is harmless by design; the rows themselves follow the fork/rollback spine).

Scoring is the Stanford generative-agents composite (Proxy SS4, verbatim): score =
w_rec*norm(recency_decay^Δturns_since_access) + w_imp*norm(importance/10) + w_rel*norm(rel),
min-max normalized per candidate set, equal weights default, top-k 3-5. Relevance is
keyword idf-overlap ("bm25ish", 03 SS7) — the embeddings group is OFF by default (Q8);
if enabled but unwired we warn once and fall back to keywords (fail-open, rules-complete).

Reflection (rules mode, 08 L2): episodic rows from scenes older than
reflection_every_scenes are consolidated per scene under a summary node — text is an
importance-weighted DIGEST of member texts (consolidation, not synthesis: rules mode
never fakes prose it can't write; LLM synthesis arrives with the assist wiring).
Members get parent_id set and leave retrieval; the summary node enters it.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Optional

log = logging.getLogger("aetherstate.memory")

_TOKEN = re.compile(r"[a-z0-9']+")
_STOP = {"the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "is", "are",
         "was", "were", "be", "been", "it", "its", "with", "for", "as", "that", "this",
         "she", "he", "they", "her", "his", "their", "you", "your", "i", "my", "we"}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP}


def _loads(blob, default):
    try:
        v = json.loads(blob) if isinstance(blob, str) else blob
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


# ---- index writes (cold path, post-apply) --------------------------------------------------
def index_applied(store, session_id: str, branch_id: str, applied_ops: list,
                  state: dict) -> int:
    """Mirror APPLIED memory_event ops into the retrieval index. Idempotence comes from
    the apply path (duplicates never double-apply, 08 S7); location/scene stamped from
    the post-apply state."""
    n = 0
    sc = state.get("scene", {}) if isinstance(state, dict) else {}
    for op in applied_ops:
        if not isinstance(op, dict) or op.get("op") != "memory_event":
            continue
        turn = int(op.get("_turn", state.get("meta", {}).get("turn", 0)))
        tags = list(op.get("tags", []))
        mode = sc.get("mode")
        if mode in ("flashback", "dream") and mode not in tags:
            tags.append(mode)                              # 08 B4 parity with the reducer
        store.memories_add(session_id, branch_id, tier="episodic",
                           text=str(op.get("text", "")),
                           participants=list(op.get("participants", [])),
                           location_id=sc.get("location_id"), tags=tags,
                           importance=max(1, min(10, int(op.get("importance", 3)))),
                           created_turn=turn,
                           scene_index=int(sc.get("scene_index", 0)))
        n += 1
    return n


# ---- retrieval (03 SS7) --------------------------------------------------------------------
def _prefilter(rows, state: dict, limit: int) -> list:
    """participants ∩ scene.participants OR location = scene.location OR tags ∩ active_tags
    (03 SS7). None==None is no-information, never a match. Zero structured matches falls
    back to the most recent rows — the prefilter exists to CAP candidates at scale
    (08 L3), not to starve recall in scenes with no overlapping metadata yet."""
    sc = state.get("scene", {})
    scene_parts = set(sc.get("participants", []))
    scene_loc = sc.get("location_id")
    active_tags = {sc.get("mode")} - {None, "live"}
    hits = []
    for r in rows:
        parts = set(_loads(r["participants"], []))
        tags = set(_loads(r["tags"], []))
        if ((scene_parts and parts & scene_parts)
                or (scene_loc is not None and r["location_id"] == scene_loc)
                or (active_tags and tags & active_tags)):
            hits.append(r)
        if len(hits) >= limit:
            return hits
    return hits if hits else list(rows[:min(50, limit)])


def _bm25ish(query: str, rows) -> list[float]:
    """Deterministic idf-weighted token overlap over the candidate set (03 SS7 keyword
    fallback). Not real BM25 — it doesn't need to be; min-max norm eats the scale."""
    q = _tokens(query)
    if not q or not rows:
        return [0.0] * len(rows)
    docs = [_tokens(r["text"]) for r in rows]
    df: dict[str, int] = {}
    for d in docs:
        for t in q & d:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    return [sum(math.log(1.0 + n / (1.0 + df[t])) for t in (q & d)) for d in docs]


def _norm(xs: list[float]) -> list[float]:
    lo, hi = (min(xs), max(xs)) if xs else (0.0, 0.0)
    if hi - lo < 1e-12:
        return [1.0] * len(xs)          # constant term contributes equally to every candidate
    return [(x - lo) / (hi - lo) for x in xs]


def _cos(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _relevance(store, cands, query_text: str, query_vec) -> list[float]:
    """Cosine over stored vectors when the cold path embedded the query (03 SS7);
    keyword fallback otherwise or when fewer than half the candidates are embedded."""
    if query_vec:
        try:
            from .assist import unpack
            blobs = store.embeddings_get([r["memory_id"] for r in cands])
            if len(blobs) * 2 >= len(cands):
                return [_cos(query_vec, unpack(blobs[r["memory_id"]]))
                        if r["memory_id"] in blobs else 0.0 for r in cands]
        except Exception as exc:                        # fail-open to keyword
            log.warning("vector relevance failed open: %s", type(exc).__name__)
    return _bm25ish(query_text, cands)


def retrieve(store, cfg, branch_id: str, state: dict, query_text: str,
             now_turn: int, query_vec=None) -> list:
    """Top-k memory rows by the composite score; bumps last_accessed_turn on the winners
    (recency is decay since last ACCESS — Proxy SS4/Park et al., not since creation)."""
    m = cfg.memory
    rows = store.memories_candidates(branch_id)
    cands = _prefilter(rows, state, m.prefilter_limit)
    if not cands:
        return []
    rec = _norm([m.recency_decay ** max(0, now_turn - r["last_accessed_turn"]) for r in cands])
    imp = _norm([r["importance"] / 10.0 for r in cands])
    rel = _norm(_relevance(store, cands, query_text, query_vec))
    scored = sorted(
        ((m.w_recency * rec[i] + m.w_importance * imp[i] + m.w_relevance * rel[i], i)
         for i in range(len(cands))),
        key=lambda t: (-t[0], cands[t[1]]["created_turn"] * -1))    # deterministic tie-break
    top = [cands[i] for _, i in scored[:max(1, m.top_k)]]
    store.memories_bump_access([r["memory_id"] for r in top], now_turn)
    return top


# ---- rendering (04 SS3.5) ------------------------------------------------------------------
def when_phrase(delta_turns: int) -> str:
    """Numbers are never shown to the model (04 SS3.5) — deterministic buckets only."""
    if delta_turns <= 2:
        return "just now"
    if delta_turns <= 8:
        return "recently"
    if delta_turns <= 20:
        return "earlier"
    if delta_turns <= 60:
        return "a while back"
    return "long ago"


def recall_lines(rows, now_turn: int) -> list[str]:
    lines = []
    for r in rows:
        tags = set(_loads(r["tags"], []))
        prefix = ""
        for t in ("dream", "flashback"):                    # 08 B4: non-live marked as such
            if t in tags:
                prefix = f"({t}) "
        lines.append(f"- {prefix}{r['text']} ({when_phrase(now_turn - r['created_turn'])})")
    return lines


def render_recall(lines: list[str], who: Optional[str]) -> str:
    head = f"[RECALL] (things {who} remembers)" if who else "[RECALL] (remembered)"
    return head + "\n" + "\n".join(lines)


# ---- reflection (rules mode, 08 L2) --------------------------------------------------------
_DIGEST_MEMBERS = 3
_DIGEST_CHARS = 400


def reflect(store, cfg, session_id: str, branch_id: str, state: dict) -> int:
    """Consolidate episodic rows from scenes older than reflection_every_scenes into
    per-scene summary nodes. Deterministic; returns number of summaries created."""
    cur = int(state.get("scene", {}).get("scene_index", 0))
    horizon = cur - int(cfg.memory.reflection_every_scenes)
    if horizon < 0:
        return 0
    stale = store.memories_stale_episodic(branch_id, horizon)
    by_scene: dict[int, list] = {}
    for r in stale:
        by_scene.setdefault(r["scene_index"], []).append(r)
    made = 0
    for scene_idx in sorted(by_scene):
        rows = by_scene[scene_idx]
        best = sorted(rows, key=lambda r: (-r["importance"], r["created_turn"]))
        digest = "; ".join(r["text"] for r in best[:_DIGEST_MEMBERS])[:_DIGEST_CHARS]
        participants = sorted({p for r in rows for p in _loads(r["participants"], [])})
        tags = sorted({t for r in rows for t in _loads(r["tags"], [])} | {"summary"})
        loc = rows[0]["location_id"]
        sid = store.memories_add(session_id, branch_id, tier="summary", text=digest,
                                 participants=participants, location_id=loc, tags=tags,
                                 importance=max(r["importance"] for r in rows),
                                 created_turn=max(r["created_turn"] for r in rows),
                                 scene_index=scene_idx)
        store.memories_set_parent([r["memory_id"] for r in rows], sid)
        made += 1
    if made:
        log.info("reflection consolidated %d scene(s) on branch %s", made, branch_id[:8])
    return made


# ---- Q15 slice precompute (cold path) ------------------------------------------------------
def precompute_recall(store, cfg, session_id: str, branch_id: str, state: dict,
                      query_text: str, now_turn: int, query_vec=None) -> None:
    """Retrieve against the just-settled exchange and stage [RECALL] lines for the NEXT
    request (hot path does one SELECT). Fail-open: any error leaves the previous row."""
    try:
        rows = retrieve(store, cfg, branch_id, state, query_text, now_turn, query_vec)
        store.write_recall(session_id, now_turn + 1, recall_lines(rows, now_turn))
    except Exception as exc:
        log.warning("recall precompute skipped: %s", type(exc).__name__)
