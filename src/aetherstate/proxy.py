"""Transparent byte-relay proxy (Phase 0).

Invariants enforced here (planning/01 SS2):
- Fail open: this module never parses request/response bodies. Bytes in, bytes out.
- Never block/corrupt/delay the stream: chunks are forwarded as received.
- Upstream errors (status >= 400) are relayed verbatim (09 U2).
- x-aetherstate-* request headers are consumed by the proxy, never forwarded upstream.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .stamps import MARKER, parse_and_strip

log = logging.getLogger("aetherstate.proxy")

# Hop-by-hop headers (RFC 9110) + ones the client stack recomputes.
_SKIP_REQ = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate",
             "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "accept-encoding"}
_SKIP_RESP = {"content-length", "connection", "keep-alive", "transfer-encoding",
              "trailer", "upgrade"}


def upstream_url(base_url: str, path: str, query: str = "") -> str:
    """Map the proxy's OpenAI surface onto the configured upstream base.

    Convention (same as SillyTavern's custom-endpoint rule): `upstream.base_url` is the FULL
    OpenAI-compatible base INCLUDING the version segment â€” e.g. https://api.openai.com/v1
    or https://api.openai.com/v1. Frontends call the proxy at /v1/<rest>; we strip the /v1
    marker and graft <rest> onto the base. Regression: naive joining produced /api/v1/v1/... (404).
    """
    segs = path.lstrip("/")
    if segs == "v1":
        rest = ""
    elif segs.startswith("v1/"):
        rest = segs[len("v1/"):]
    else:
        rest = segs                      # non-/v1 callers graft as-is
    url = base_url.rstrip("/") + (f"/{rest}" if rest else "")
    return f"{url}?{query}" if query else url


def make_relay_router(get_client: Callable[[], httpx.AsyncClient], cfg, engine=None,
                      pipeline=None) -> APIRouter:
    router = APIRouter()

    @router.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
    async def relay(path: str, request: Request):
        body = await request.body()  # raw bytes; parsed ONLY to strip sentinels / observe
        stamp = None
        post_ctx = None              # set by the pipeline; consumed by the response tee
        if engine is not None and (MARKER in body or cfg.stamp.header_name in
                                   {k.lower() for k in request.headers.keys()}):
            try:  # fail-open supervisor (invariant 1): any error -> forward as-is minus sentinel
                stamp, body = parse_and_strip(dict(request.headers), body,
                                              header_name=cfg.stamp.header_name)
            except Exception as exc:
                log.warning("stamp path failed open: %s", type(exc).__name__)
                if MARKER in body:  # last-resort scrub â€” a sentinel never goes upstream (09 I3)
                    from .stamps import SENTINEL_ANY
                    body = SENTINEL_ANY.sub("", body.decode(errors="replace")).encode()
        if request.method == "POST" \
                and path.rstrip("/").endswith("chat/completions") \
                and len(body) <= cfg.upstream.max_parse_mb * 1024 * 1024:
            if pipeline is not None:
                try:  # P2 enrichment: Tier-0 + compose. ANY failure -> original bytes (invariant 1)
                    body, post_ctx = pipeline.process(stamp, body)
                except Exception as exc:
                    log.warning("enrichment failed open: %s", type(exc).__name__)
            elif engine is not None:
                try:  # observe-only wiring (P1 harness compat); forwarded bytes never altered
                    engine.observe(stamp, body)
                except Exception as exc:
                    log.warning("session engine failed open: %s", type(exc).__name__)
        headers = {}
        for name, value in request.headers.items():
            lname = name.lower()
            if lname in _SKIP_REQ or lname.startswith("x-aetherstate"):
                continue
            headers[name] = value
        # Force identity: httpx injects its own accept-encoding otherwise, and a teeing proxy
        # must relay bytes it can also read. Content is unchanged; only transport encoding.
        headers["accept-encoding"] = "identity"
        if cfg.upstream.api_key and "authorization" not in {h.lower() for h in headers}:
            headers["Authorization"] = f"Bearer {cfg.upstream.api_key}"

        if not cfg.upstream.base_url:
            return Response(
                content=(b'{"error":{"message":"AetherState: upstream.base_url is not configured "'
                         b'"(set it in config.toml, e.g. https://api.openai.com/v1)",'
                         b'"type":"not_configured","code":502}}'),
                status_code=502, media_type="application/json")

        client = get_client()
        url = upstream_url(cfg.upstream.base_url, path, request.url.query)
        try:
            upstream_req = client.build_request(request.method, url, content=body, headers=headers)
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:  # 09 U1: OpenAI-shaped 502, no state effects
            log.warning("upstream unreachable: %s", type(exc).__name__)
            return Response(
                content=(b'{"error":{"message":"AetherState: upstream unreachable",'
                         b'"type":"upstream_unreachable","code":502}}'),
                status_code=502, media_type="application/json")

        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESP}

        tee_cap = 4 * 1024 * 1024      # bounded copy; oversized replies skip extraction

        async def stream_bytes() -> AsyncIterator[bytes]:
            buf = bytearray()
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk  # verbatim â€” invariant 2 (09 U7: even malformed SSE relays)
                    if post_ctx is not None and len(buf) <= tee_cap:
                        buf.extend(chunk)   # copy AFTER yield: the tee never delays the stream
            except httpx.HTTPError as exc:  # 09 U4: upstream died mid-stream -> end as received
                log.warning("upstream stream ended abnormally: %s", type(exc).__name__)
            finally:
                await upstream.aclose()
                # cold path starts HERE (03 SS1): text capture + extraction scheduling
                if pipeline is not None and post_ctx is not None \
                        and upstream.status_code < 400 and len(buf) <= tee_cap:
                    try:
                        pipeline.on_response(post_ctx, bytes(buf),
                                             upstream.headers.get("content-type", ""))
                    except Exception as exc:
                        log.warning("tee failed open: %s", type(exc).__name__)

        return StreamingResponse(stream_bytes(), status_code=upstream.status_code,
                                 headers=resp_headers, media_type=upstream.headers.get("content-type"))

    return router
