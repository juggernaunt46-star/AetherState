"""Assist-group wiring (Q8, 06 C): embeddings retrieval, LLM reflection synthesis, and
the advisory NLI contradiction pass — the three deferred P4 features that need a model.

Every function here is COLD PATH ONLY and fail-open (invariants 1-3): any endpoint
error, timeout, or malformed reply degrades to the rules-mode behavior that already
shipped (keyword retrieval, digest summaries, no NLI rows). Rules mode stays a complete
product (Q8) — this module only upgrades quality when a backend is available.

Routing per group (12 [assist.groups]): "off"/"rules" -> None (feature stays rules-mode),
"assist" -> first [assist] endpoint (warn once if none configured), "main" -> the main
upstream. All live-toggleable (01 SS9b)."""
from __future__ import annotations

import json
import logging
import re
from array import array

import httpx

from .extraction import Endpoint, is_venice_host, repair_json

log = logging.getLogger("aetherstate.assist")

_TIMEOUT_S = 25.0
# some thinking models leak inline <think> blocks into content — never useful here
_THINK_RE = re.compile(r"<think>.*?(?:</think>\s*|\Z)", re.DOTALL | re.IGNORECASE)
_warned: set = set()

_SYNTH_SYSTEM = (
    "You compress roleplay event logs. Reply with ONLY a JSON object: "
    '{"summary": "<1-2 past-tense sentences>", "facts": ["<durable fact>", ...]} — '
    "0-3 facts, each a thing that stays true going forward (a debt, a secret learned, "
    "an injury, a promise). No commentary.")
_NLI_SYSTEM = (
    "You check a roleplay passage against established facts. Reply with ONLY a JSON "
    'object: {"contradictions": [{"fact": <index>, "quote": "<short quote from the '
    'passage>"}]} — empty list if none. Only clear, direct contradictions count.')


def endpoint_for_group(cfg, group: str, model_hint: str = ""):
    """Endpoint for an assist group, or None when the group stays rules/off."""
    mode = getattr(cfg.assist.groups, group, "off") or "off"
    if mode in ("off", "rules", ""):
        return None
    if mode == "assist":
        eps = cfg.assist.endpoints
        if not eps:
            if group not in _warned:
                _warned.add(group)
                log.warning("group %s='assist' but no [assist] endpoints configured — "
                            "staying in rules mode (fail-open)", group)
            return None
        e = eps[0]
        return Endpoint(base_url=e.base_url, model=e.model or model_hint,
                        api_key=e.api_key, assist_tier=e.tier in ("nano", "small"))
    if mode == "main":
        if not cfg.upstream.base_url:
            return None
        return Endpoint(base_url=cfg.upstream.base_url, model=model_hint)
    return None


async def _chat(get_client, cfg, ep: Endpoint, system: str, user: str,
                max_tokens: int = 500):
    """One JSON-seeking generation; single retry on 429/5xx AND on transport errors
    (stale keep-alive pool after an assist restart — handoff 2026-07-04 item 3);
    None on any failure.

    Mechanics calls never need reasoning: thinking is disabled at Venice hosts the
    same way extraction/probes do it, and reasoning_content is used as a fallback
    when a thinking model replies with empty content anyway. Live repro 2026-07-04:
    GLM-5-2 with thinking ON burned the whole token budget reasoning about a long
    prose card — content came back as prose/empty and genesis Stage B seeded 0 ops."""
    body = {"model": ep.model, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    if is_venice_host(ep.base_url):
        body["venice_parameters"] = {"disable_thinking": True,
                                     "include_venice_system_prompt": False}
    headers = {"content-type": "application/json"}
    key = ep.api_key or cfg.upstream.api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = ep.base_url.rstrip("/") + "/chat/completions"
    client = get_client()
    for attempt in (0, 1):
        try:
            resp = await client.post(url, json=body, headers=headers,
                                     timeout=_TIMEOUT_S)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 0:
                    continue
                return None
            if resp.status_code >= 400:
                log.warning("assist chat upstream %d: %s",
                            resp.status_code, resp.text[:200])
                return None
            msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            if not content and msg.get("reasoning_content"):
                log.warning("assist chat: empty content, falling back to "
                            "reasoning_content (%d chars) — thinking not disabled "
                            "at %s?", len(msg["reasoning_content"]), ep.base_url)
                content = msg["reasoning_content"]
            return _THINK_RE.sub("", content)
        except httpx.TransportError as exc:
            log.warning("assist chat transport error: %s — %s", type(exc).__name__,
                        "retrying once" if attempt == 0 else "failing open")
            if attempt == 0:
                continue
            return None
        except Exception as exc:
            log.warning("assist chat failed open: %s", type(exc).__name__)
            return None
    return None


def _json_or_none(text):
    if not text:
        return None
    try:
        return json.loads(repair_json(text))
    except (json.JSONDecodeError, ValueError):
        return None


# ------------------------------------------------------------------ embeddings (03 SS7)
async def embed_texts(get_client, cfg, ep: Endpoint, texts: list):
    """OpenAI-compatible /embeddings; list of vectors or None (fail-open)."""
    if not texts:
        return []
    headers = {"content-type": "application/json"}
    key = ep.api_key or cfg.upstream.api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = ep.base_url.rstrip("/") + "/embeddings"
    try:
        resp = await get_client().post(url, json={"model": ep.model, "input": texts},
                                       headers=headers, timeout=_TIMEOUT_S)
        if resp.status_code >= 400:
            return None
        data = resp.json().get("data") or []
        vecs = [d.get("embedding") for d in sorted(data, key=lambda d: d.get("index", 0))]
        if len(vecs) != len(texts) or any(not isinstance(v, list) for v in vecs):
            return None
        return vecs
    except Exception as exc:
        log.warning("embeddings failed open: %s", type(exc).__name__)
        return None


async def embed_missing(store, cfg, get_client, ep: Endpoint, branch_id: str,
                        limit: int = 64) -> int:
    """Vectorize memories that lack embeddings (cold path, batched). Returns count."""
    rows = store.embeddings_missing(branch_id, limit)
    if not rows:
        return 0
    vecs = await embed_texts(get_client, cfg, ep, [r["text"] for r in rows])
    if vecs is None:
        return 0
    store.embeddings_put([(rows[i]["memory_id"], _pack(vecs[i]), len(vecs[i]))
                          for i in range(len(rows))])
    return len(rows)


async def embed_query(get_client, cfg, ep: Endpoint, query_text: str):
    vecs = await embed_texts(get_client, cfg, ep, [query_text[:2000]])
    return vecs[0] if vecs else None


def _pack(vec: list) -> bytes:
    return array("f", [float(x) for x in vec]).tobytes()


def unpack(blob: bytes) -> list:
    a = array("f")
    a.frombytes(blob)
    return list(a)


# ------------------------------------------------------------------ reflection (03 SS7)
async def synthesize(store, cfg, get_client, ep: Endpoint, session_id: str,
                     branch_id: str, limit: int = 3) -> int:
    """Upgrade rules-mode digest summaries to LLM prose + episodic->semantic facts.
    Any failure leaves the digest standing (honest rules product). Returns upgrades."""
    done = 0
    for s in store.summaries_unsynthesized(branch_id, limit):
        members = store.memories_members(s["memory_id"])
        if not members:
            continue
        events = "\n".join(f"- {m['text']}" for m in members[:12])
        raw = await _chat(get_client, cfg, ep, _SYNTH_SYSTEM,
                          f"Events, oldest first:\n{events}")
        doc = _json_or_none(raw)
        if not isinstance(doc, dict) or not str(doc.get("summary", "")).strip():
            continue
        store.memories_update_text(s["memory_id"], str(doc["summary"]).strip()[:600],
                                   add_tag="synthesized")
        for fact in list(doc.get("facts") or [])[:3]:
            if isinstance(fact, str) and fact.strip():
                store.memories_add(
                    session_id, branch_id, tier="semantic", text=fact.strip()[:300],
                    participants=json.loads(s["participants"] or "[]"),
                    location_id=s["location_id"],
                    tags=["semantic", "reflection"],
                    importance=5, created_turn=s["created_turn"],
                    scene_index=s["scene_index"])
        done += 1
    if done:
        log.info("reflection synthesized %d summary(ies) on %s", done, branch_id[:8])
    return done


# ------------------------------------------------------------------ NLI linter (03 SS9)
async def nli_pass(store, cfg, get_client, ep: Endpoint, branch_id: str, turn: int,
                   state: dict, text: str) -> int:
    """Advisory-only contradiction check of new prose vs established facts (03 SS9:
    'NLI is advisory-only — rules stay the authority'). Inspector rows, never notes."""
    from .linter import Violation                       # local import: no cycle at load
    facts = [f.get("statement", "") for f in (state.get("facts") or {}).values()][-12:]
    if not facts or not (text and text.strip()):
        return 0
    listing = "\n".join(f"{i}: {f}" for i, f in enumerate(facts))
    raw = await _chat(get_client, cfg, ep, _NLI_SYSTEM,
                      f"Established facts:\n{listing}\n\nPassage:\n{text[:1500]}")
    doc = _json_or_none(raw)
    if not isinstance(doc, dict):
        return 0
    vios = []
    for c in list(doc.get("contradictions") or [])[:3]:
        try:
            fact = facts[int(c["fact"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        quote = str(c.get("quote", ""))[:160]
        vios.append(Violation("NLI", "low", ("nli", fact[:40]),
                              f"passage may contradict: {fact[:120]}",
                              evidence=quote, advisory=True))
    if vios:
        store.lint_add(branch_id, turn, vios)
    return len(vios)
