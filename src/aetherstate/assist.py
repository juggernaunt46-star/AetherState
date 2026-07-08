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
    "You verify a roleplay passage against a numbered list of ESTABLISHED FACTS. Reply with "
    'ONLY a JSON object: {"contradictions": [{"premise": <index>, "quote": "<short span '
    'from the passage>", "score": <0.0-1.0>}]} — an empty list if none.\n'
    "Flag a fact ONLY when the passage asserts something that makes it FALSE (a real "
    "contradiction). Do NOT flag a fact just because the passage adds NEW detail the facts "
    "do not mention — unstated, new information is allowed and is NOT a contradiction. "
    "score is your confidence that it is a genuine contradiction.")


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
        # per-group endpoint override (Q8): a group may name a specific [[assist.endpoints]];
        # unset OR an unknown name falls open to endpoints[0] — byte-identical to 1.0 when empty.
        name = (getattr(getattr(cfg.assist, "group_endpoints", None), group, "") or "").strip()
        e = next((x for x in eps if x.name == name), None) if name else None
        if e is None:
            e = eps[0]
        return Endpoint(base_url=e.base_url, model=e.model or model_hint,
                        api_key=e.api_key, assist_tier=e.tier in ("nano", "small"))
    if mode == "main":
        if not cfg.upstream.base_url:
            return None
        return Endpoint(base_url=cfg.upstream.base_url, model=model_hint)
    return None


async def _chat(get_client, cfg, ep: Endpoint, system: str, user: str,
                max_tokens: int = 500, temperature: float = 0.0,
                timeout_s: float | None = None):
    """One JSON-seeking generation; single retry on 429/5xx AND on transport errors
    (stale keep-alive pool after an assist restart — handoff 2026-07-04 item 3);
    None on any failure.

    Mechanics calls never need reasoning: thinking is disabled at Venice hosts the
    same way extraction/probes do it, and reasoning_content is used as a fallback
    when a thinking model replies with empty content anyway. Live repro 2026-07-04:
    GLM-5-2 with thinking ON burned the whole token budget reasoning about a long
    prose card — content came back as prose/empty and genesis Stage B seeded 0 ops.

    temperature/timeout_s (2026-07-06, live repro): mechanics stay at 0.0/25 s, but
    CREATIVE cold-path callers (creator authoring, genesis stage B) pass a higher
    temperature and a much longer timeout — a large model writing 2-4k tokens of
    world JSON takes well over 25 s, and the old fixed timeout made every authoring
    call silently fall back to templates."""
    body = {"model": ep.model, "max_tokens": max_tokens, "temperature": temperature,
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
                                     timeout=timeout_s or _TIMEOUT_S)
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


_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


def _heal_stray_quotes(t: str, rounds: int = 40):
    """Last-resort healer for the classic large-model failure: an UNESCAPED double quote
    inside a JSON string ('the hero "Hawks" swaggers...') closes the string early and no
    brace/comma repair can save it. Bounded loop: at each decode error, escape the quote
    nearest before the failure point and retry (2026-07-07 live repro: GLM-5.2's MHA
    world JSON died exactly this way). Returns the parsed object or None."""
    for _ in range(rounds):
        try:
            return json.loads(t)
        except json.JSONDecodeError as e:
            q = t.rfind('"', 0, e.pos)
            if q <= 0:
                return None
            t = t[:q] + '\\"' + t[q + 1:]
        except ValueError:
            return None
    return None


def _json_or_none(text):
    """Parse a JSON object out of a model reply. 2026-07-06 live repro: GLM wraps its
    (otherwise perfect) creator JSON in a ```json fence — strip fences first, then try
    the whole reply, the outermost {...} slice (prose-prefixed replies), and the BALANCED
    slice (trailing commentary after the JSON). Unescaped-quote healing is the last rung."""
    if not text:
        return None
    text = _FENCE_RE.sub("", text)
    i, k = text.find("{"), text.rfind("}")
    cands = [text]
    if 0 <= i < k:
        from .extraction import _last_balanced
        cands.append(text[i:k + 1])
        cands.append(text[i:_last_balanced(text, i)])   # ignores junk past the object
    for cand in cands:
        if not cand:
            continue
        try:
            return json.loads(repair_json(cand))
        except (json.JSONDecodeError, ValueError):
            continue
    for cand in cands[1:] if len(cands) > 1 else cands:   # heal unescaped inner quotes
        doc = _heal_stray_quotes(repair_json(cand))
        if isinstance(doc, dict):
            return doc
    return None


async def resolve_endpoint(get_client, cfg, ep: Endpoint) -> Endpoint:
    """Make sure a cold-path endpoint actually names a model (2026-07-06 live repro:
    at chat-open genesis nothing has been proxied yet, so jobs.endpoint_for returned
    model='' and every stage-B call died on an upstream 400 — then marked itself done).

    Resolution order, all fail-open: (1) the endpoint already has a model — keep it;
    (2) a configured [[assist.endpoints]] model (Bean's explicit mechanics pick) — use
    that endpoint wholesale; (3) [upstream].model default when set; (4) detect via
    GET /models at the endpoint itself. Returns the endpoint unchanged when nothing
    resolves — the caller's own fail-open handles it."""
    try:
        if ep.model:
            return ep
        eps = cfg.assist.endpoints
        if eps and eps[0].model:
            e = eps[0]
            return Endpoint(base_url=e.base_url, model=e.model, api_key=e.api_key,
                            assist_tier=e.tier in ("nano", "small"))
        default = getattr(cfg.upstream, "model", "") or ""
        if default:
            ep.model = default
            return ep
        base = (ep.base_url or "").rstrip("/")
        if not base:
            return ep
        headers = {}
        key = ep.api_key or cfg.upstream.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = await get_client().get(base + "/models", headers=headers, timeout=15.0)
        if r.status_code < 400:
            ids = sorted([m.get("id") for m in (r.json().get("data") or []) if m.get("id")])
            if ids:
                ep.model = ids[0]
    except Exception as exc:
        log.debug("endpoint model resolution failed open: %s", type(exc).__name__)
    return ep


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


# ------------------------------------------------------------------ NLI linter (03 SS9, L10)
async def nli_pass(get_client, cfg, ep: Endpoint, premises: list, hypotheses: list,
                   *, threshold: float = 0.6, max_hits: int = 4) -> list:
    """Cold-path, fail-open contradiction detector behind the L10 ledger check (03 SS9).

    Takes PREMISES (the committed ledger slice as short declarative sentences — the truth) and
    HYPOTHESES (the narrator's prose split into claims) and returns the CONTRADICTION hits only
    — [{"premise": <idx>, "quote": "<span>", "score": <float>}] — or [] on ANY error
    (invariant 1: a missing or broken judge degrades silently to the rules floor, never raises).

    Fires ONLY on contradiction (the prose asserts a premise is false). Prose that merely adds
    detail the ledger does not cover is NEUTRAL/new fiction, not a hallucination, and is left
    alone — 'freedom of fiction, constraint on fact'. The contradiction-only instruction and the
    score threshold are the two filters that keep new authoring from tripping it.

    Model-agnostic + OpenAI-compatible: the assist tier (a LOCAL MiniCheck / 3-way NLI server
    behind a chat shim) and the main tier (a big judge on a DIFFERENT endpoint than the narrator
    — self-judging inflates scores) travel the same _chat path; the caller selects the endpoint
    via endpoint_for_group('linter_nli')."""
    if not premises or not hypotheses:
        return []
    facts = "\n".join(f"{i}: {p}" for i, p in enumerate(premises))
    passage = "\n".join(f"- {h}" for h in hypotheses)
    raw = await _chat(get_client, cfg, ep, _NLI_SYSTEM,
                      f"Established facts:\n{facts}\n\nPassage claims:\n{passage[:1800]}")
    doc = _json_or_none(raw)
    if not isinstance(doc, dict):
        return []
    hits: list = []
    for c in list(doc.get("contradictions") or [])[:max_hits]:
        if not isinstance(c, dict):
            continue
        try:
            idx = int(c.get("premise", c.get("fact")))
            score = float(c.get("score", 1.0))
        except (TypeError, ValueError):
            continue
        if score < threshold or not 0 <= idx < len(premises):
            continue
        hits.append({"premise": idx, "quote": str(c.get("quote", ""))[:160], "score": score})
    return hits
