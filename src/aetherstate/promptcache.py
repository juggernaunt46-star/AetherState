"""Phase 0a — KV-cache / prompt-caching enablement (the mechanics contract, verified 2026-07-09).

Venice (and most OpenAI-compatible providers) cache prompts by TOKEN PREFIX,
byte-exact: one changed early byte re-prefills everything after it. RP prompts
are exactly the shape caching rewards — a huge stable head (card + history,
append-only) and a small volatile tail (state briefing, fresh rolls) — so an
enriched session gets up to ~90% off input cost and a large prefill-latency cut
for free, IF the proxy keeps volatile bytes at the tail (compose's default
`depth` placement already does) and routes every turn of a conversation to the
same warm cache server. This module owns the engine-side pieces:

- `add_cache_key` — `prompt_cache_key` = the internal session id, added ONLY to
  requests the engine already enriched. An untouched request stays byte-identical
  (transparency invariant); the client's own key is never overwritten.
- `parse_usage` + `CacheStats` — cold-path observability (pillar 17): read the
  `usage` block a response carries, surface hit rates in /aether/status + Console.
- `add_usage_probe` — opt-in `stream_options.include_usage` so a streaming
  upstream reports usage at all (one extra spec-standard SSE chunk — a knob,
  default off, because it changes what the upstream sends the frontend).
- `prewarm` — opt-in chat-open pre-warm: re-send the last enriched prompt once
  with max_tokens=1 so the player's first real message hits a warm prefix.
  Costs one full-price prefill; cooldown-limited; fail-open. Default off.

Nothing here touches the hot path except `add_cache_key`/`add_usage_probe`
(dict inserts, ns). Every entry point is fail-open (invariant 1).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("aetherstate.promptcache")

PREWARM_COOLDOWN_S = 240          # one prewarm per session per window (~ the provider TTL)
LAST_DOCS_MAX = 16                # sessions of last-enriched-doc memory (the prewarm source)


def add_cache_key(doc: dict, session_id: str) -> bool:
    """Routing hint: every turn of a conversation lands on the same warm cache
    server. The value is the stable internal session id (opaque, no prose)."""
    if not isinstance(doc, dict) or doc.get("prompt_cache_key"):
        return False                  # the client's own key wins (transparency)
    doc["prompt_cache_key"] = f"aether-{session_id}"
    return True


def add_usage_probe(doc: dict) -> bool:
    """stream_options.include_usage on a STREAMING request (non-streaming replies
    carry usage anyway). Opt-in via [upstream].include_usage — the upstream will
    append one spec-standard usage chunk before [DONE], which the frontend sees;
    that visible change is why this is a knob and default off."""
    if not isinstance(doc, dict) or not doc.get("stream"):
        return False
    so = doc.get("stream_options")
    so = dict(so) if isinstance(so, dict) else {}
    if so.get("include_usage"):
        return False
    so["include_usage"] = True
    doc["stream_options"] = so
    return True


def parse_usage(raw: bytes, content_type: str) -> Optional[dict]:
    """The `usage` object from a completed response — the last SSE data chunk
    carrying one, or the plain-JSON body. None when nothing was reported."""
    try:
        if b"data:" in raw[:256] or "text/event-stream" in (content_type or ""):
            usage = None
            for line in raw.split(b"\n"):
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                if b'"usage"' not in payload:
                    continue
                try:
                    doc = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(doc, dict) and isinstance(doc.get("usage"), dict):
                    usage = doc["usage"]
            return usage
        doc = json.loads(raw)
        u = doc.get("usage") if isinstance(doc, dict) else None
        return u if isinstance(u, dict) else None
    except Exception:
        return None


def cached_token_count(usage: dict) -> int:
    """Provider dialects: OpenAI/Venice `prompt_tokens_details.cached_tokens`;
    some relays flatten it to `cached_tokens`; Anthropic-style relays report
    `cache_read_input_tokens`. First readable value wins."""
    det = usage.get("prompt_tokens_details")
    candidates = (det.get("cached_tokens") if isinstance(det, dict) else None,
                  usage.get("cached_tokens"), usage.get("cache_read_input_tokens"))
    for v in candidates:
        try:
            if v is not None:
                return max(0, int(v))
        except (TypeError, ValueError):
            continue
    return 0


class CacheStats:
    """In-memory, process-lifetime counters for /aether/status + the Console chip.
    Pure observability — losing them on a restart costs nothing."""

    def __init__(self) -> None:
        self.requests = 0             # enriched responses observed (the key-carrying set)
        self.with_usage = 0           # ...that reported a usage block at all
        self.hits = 0                 # ...with cached_tokens > 0
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.prewarms = 0
        self.prewarm_fails = 0
        self.sessions: dict[str, dict] = {}     # sid -> last-response numbers (bounded)

    def observe(self, session_id: str, usage: Optional[dict]) -> None:
        self.requests += 1
        if not isinstance(usage, dict):
            return
        try:
            pt = max(0, int(usage.get("prompt_tokens") or 0))
        except (TypeError, ValueError):
            pt = 0
        ct = cached_token_count(usage)
        self.with_usage += 1
        self.prompt_tokens += pt
        self.cached_tokens += ct
        if ct > 0:
            self.hits += 1
        self.sessions[session_id] = {"prompt_tokens": pt, "cached_tokens": ct}
        while len(self.sessions) > 32:          # bounded — no unbounded growth, ever
            self.sessions.pop(next(iter(self.sessions)))

    def snapshot(self, cfg=None) -> dict:
        up = getattr(cfg, "upstream", None)
        rate = (round(self.cached_tokens / self.prompt_tokens, 3)
                if self.prompt_tokens else None)
        return {"enabled": bool(getattr(up, "cache_key", True)),
                "include_usage": bool(getattr(up, "include_usage", False)),
                "prewarm": bool(getattr(up, "prewarm", False)),
                "requests": self.requests, "with_usage": self.with_usage,
                "hits": self.hits, "prompt_tokens": self.prompt_tokens,
                "cached_tokens": self.cached_tokens,
                "hit_rate_tokens": rate,        # cached/prompt whole-run; None = no usage yet
                "prewarms_sent": self.prewarms, "prewarm_fails": self.prewarm_fails,
                "sessions": dict(self.sessions)}


async def prewarm(get_client, cfg, doc: dict, stats: Optional[CacheStats] = None) -> bool:
    """Re-send the last enriched prompt with max_tokens=1 / stream off so the NEXT
    real message hits a warm prefix. The provider cache keys on the tokenized
    `messages` prefix — max_tokens/stream are transport fields and don't split it.
    Fail-open: errors are logged and swallowed; no state, no journal, no tee."""
    try:
        from .proxy import upstream_url
        body = dict(doc)              # shallow: messages are shared and never mutated
        body["max_tokens"] = 1
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = 1
        body["stream"] = False
        body.pop("stream_options", None)
        headers = {"content-type": "application/json"}
        if cfg.upstream.api_key:
            headers["Authorization"] = f"Bearer {cfg.upstream.api_key}"
        r = await get_client().post(
            upstream_url(cfg.upstream.base_url, "v1/chat/completions"),
            json=body, headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=None))
        ok = r.status_code < 400
        if stats is not None:
            stats.prewarms += 1
            if not ok:
                stats.prewarm_fails += 1
        log.info("prompt-cache prewarm %s (%d)", "ok" if ok else "failed", r.status_code)
        return ok
    except Exception as exc:
        if stats is not None:
            stats.prewarms += 1
            stats.prewarm_fails += 1
        log.info("prompt-cache prewarm failed open: %s", type(exc).__name__)
        return False
