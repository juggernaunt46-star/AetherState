"""Transparent byte-relay proxy plus proof-carrying semantic delivery.

Invariants enforced here (planning/01 SS2):
- Non-gated traffic fails open: this module never parses request/response bodies. Bytes in, bytes out.
- Non-gated chunks are forwarded as received without blocking, corruption, or delay.
- Upstream errors (status >= 400) are relayed verbatim (09 U2).
- x-aetherstate-* request headers are consumed by the proxy, never forwarded upstream.
- A semantic-gated RPG turn fails closed and relays only its claimed terminal artifact.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import AsyncIterator, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .semantic_selection_transport import MAX_SELECTION_RESPONSE_BYTES
from .stamps import MARKER, parse_and_strip
from .turn_lifecycle import TurnLifecycleError

log = logging.getLogger("aetherstate.proxy")

# Hop-by-hop headers (RFC 9110) + ones the client stack recomputes.
_SKIP_REQ = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate",
             "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "accept-encoding"}
_SKIP_RESP = {"content-length", "connection", "keep-alive", "transfer-encoding",
              "trailer", "upgrade"}
_SEMANTIC_AUTH_HEADERS = {"x-api-key", "api-key", "x-goog-api-key"}


def _authorization_is_usable(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    parts = stripped.split(maxsplit=1)
    if parts[0].lower() == "bearer":
        return len(parts) == 2 and bool(parts[1].strip())
    return True


def _semantic_selection_headers(request: Request, cfg) -> dict[str, str]:
    """Copy only auth transport into the isolated selector request.

    Transcript-adjacent cookies, provider extensions, cache/routing metadata, frontend tracing,
    and arbitrary vendor headers never cross the selector membrane.  The selector body already
    fixes content type, output shape, and model identity.
    """
    headers = {
        "accept": "application/json, text/event-stream",
        "accept-encoding": "identity",
        "content-type": "application/json",
    }
    authorization_values: list[str] = []
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered == "authorization":
            authorization_values.append(value)
        elif lowered in _SEMANTIC_AUTH_HEADERS:
            headers[lowered] = value
    authorization = next(
        (value for value in authorization_values if _authorization_is_usable(value)),
        None,
    )
    if authorization is not None:
        headers["authorization"] = authorization
    elif cfg.upstream.api_key:
        headers["authorization"] = f"Bearer {cfg.upstream.api_key}"
    elif authorization_values:
        headers["authorization"] = authorization_values[0]
    return headers


async def _buffer_semantic_selection(
    get_client: Callable[[], httpx.AsyncClient],
    cfg,
    path: str,
    request: Request,
    request_bytes: bytes,
) -> tuple[bytes | None, str | None, bool, bool]:
    """Run one internal selector call and return only bounded, fully buffered evidence.

    The tuple is ``(raw, content_type, upstream_error, timed_out)``.  Error and oversized paths
    discard every selector byte.  They are resolved by Pipeline into the already-persisted exact
    fallback; this helper never returns a response to the frontend or invokes a cold extractor.
    """
    if not cfg.upstream.base_url:
        return None, None, True, False
    response = None
    try:
        client = get_client()
        # Query parameters belong to the frontend/vendor request, not the sealed selector.
        url = upstream_url(cfg.upstream.base_url, path)
        upstream_request = client.build_request(
            "POST",
            url,
            content=request_bytes,
            headers=_semantic_selection_headers(request, cfg),
        )
        response = await client.send(upstream_request, stream=True)
        content_type = response.headers.get("content-type", "")
        buffered = bytearray()
        oversized = False
        try:
            async for chunk in response.aiter_raw():
                if len(buffered) + len(chunk) > MAX_SELECTION_RESPONSE_BYTES:
                    oversized = True
                    break
                buffered.extend(chunk)
        except httpx.TimeoutException as exc:
            log.warning("semantic selector timed out while buffering: %s", type(exc).__name__)
            return None, None, True, True
        except httpx.HTTPError as exc:
            log.warning("semantic selector stream failed: %s", type(exc).__name__)
            return None, None, True, False
        if response.status_code >= 400:
            log.warning("semantic selector returned HTTP %d", response.status_code)
            return None, None, True, False
        if oversized:
            log.warning("semantic selector response exceeded its bounded buffer")
            return None, None, True, False
        return bytes(buffered), content_type, False, False
    except httpx.TimeoutException as exc:
        log.warning("semantic selector timed out: %s", type(exc).__name__)
        return None, None, True, True
    except httpx.HTTPError as exc:
        log.warning("semantic selector unavailable: %s", type(exc).__name__)
        return None, None, True, False
    except Exception as exc:
        log.warning("semantic selector transport failed closed: %s", type(exc).__name__)
        return None, None, True, False
    finally:
        if response is not None:
            try:
                await response.aclose()
            except Exception as exc:
                log.warning("semantic selector close failed: %s", type(exc).__name__)


def upstream_url(base_url: str, path: str, query: str = "") -> str:
    """Map the proxy's OpenAI surface onto the configured upstream base.

    Convention (same as SillyTavern's custom-endpoint rule): `upstream.base_url` is the FULL
    OpenAI-compatible base INCLUDING the version segment — e.g. https://api.openai.com/v1
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


def _encode_local_chat_response(ctx) -> tuple[bytes, str]:
    """Encode one complete local story on the caller's requested OpenAI wire shape."""
    local = getattr(ctx, "local_response", None)
    story = getattr(local, "story", None)
    if not isinstance(story, str) or not story.strip():
        raise ValueError("local response has no story")
    model = str(getattr(local, "model", "") or "aetherstate-local")
    material = f"{ctx.response_key}|{getattr(local, 'provenance', '')}|{story}"
    completion_id = "chatcmpl-aether-" + hashlib.blake2b(
        material.encode("utf-8"), digest_size=10
    ).hexdigest()
    created = int(time.time())
    if bool(getattr(local, "stream", False)):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": story},
                "finish_reason": None,
            }],
        }
        finish = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        raw = (
            b"data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n\ndata: "
            + json.dumps(finish, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n\ndata: [DONE]\n\n"
        )
        return raw, "text/event-stream"
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": story},
            "finish_reason": "stop",
        }],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), \
        "application/json"


def _semantic_delivery_error(status_code: int) -> Response:
    """Return a content-safe OpenAI error without exposing unproved narration."""
    if status_code == 409:
        message = "AetherState: semantic response is no longer active"
        error_type = "semantic_delivery_conflict"
    else:
        message = "AetherState: semantic response is temporarily unavailable"
        error_type = "semantic_delivery_unavailable"
        status_code = 503
    raw = json.dumps(
        {"error": {"message": message, "type": error_type, "code": status_code}},
        separators=(",", ":"),
    ).encode("utf-8")
    return Response(
        content=raw,
        status_code=status_code,
        headers={"content-type": "application/json", "cache-control": "no-store"},
    )


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
                if MARKER in body:  # last-resort scrub — a sentinel never goes upstream (09 I3)
                    from .stamps import SENTINEL_ANY
                    body = SENTINEL_ANY.sub("", body.decode(errors="replace")).encode()
        is_chat_post = (
            request.method == "POST"
            and path.rstrip("/").endswith("chat/completions")
        )
        spec = getattr(cfg, "specialization", None)
        semantic_required = bool(
            spec is not None
            and getattr(spec, "name", "none") == "rpg"
            and getattr(spec, "semantic_truth_gate", False)
        )
        quiet = bool(stamp is not None and stamp.gen_type == "quiet")
        passthrough = False
        if semantic_required and pipeline is not None and stamp is not None:
            try:
                passthrough = bool(pipeline._semantic_explicit_passthrough(stamp))
            except Exception:
                passthrough = False
        semantic_quarantine = semantic_required and not quiet and not passthrough
        within_parse_limit = len(body) <= cfg.upstream.max_parse_mb * 1024 * 1024
        if is_chat_post and not within_parse_limit:
            # The size ceiling is an availability boundary, never a truth-gate bypass.  An
            # oversized visible RPG request cannot be parsed/proved and therefore cannot expose
            # unchecked upstream prose.  Background quiet work and the explicit per-session kill
            # switch retain their byte-exact passthrough contract.
            if semantic_quarantine:
                return _semantic_delivery_error(503)
        if is_chat_post and within_parse_limit:
            if pipeline is not None:
                try:  # P2 enrichment: Tier-0 + compose. ANY failure -> original bytes (invariant 1)
                    body, post_ctx = pipeline.process(stamp, body)
                except Exception as exc:
                    if semantic_quarantine:
                        log.error("semantic enrichment failed closed: %s", type(exc).__name__)
                        return _semantic_delivery_error(503)
                    log.warning("enrichment failed open: %s", type(exc).__name__)
            elif engine is not None:
                try:  # observe-only wiring (P1 harness compat); forwarded bytes never altered
                    engine.observe(stamp, body)
                except Exception as exc:
                    log.warning("session engine failed open: %s", type(exc).__name__)
            if semantic_quarantine and (
                post_ctx is None or not bool(getattr(post_ctx, "semantic_gate", False))
            ):
                log.error("semantic enrichment returned no quarantined response context")
                return _semantic_delivery_error(503)
        if pipeline is not None and post_ctx is not None \
                and bool(getattr(post_ctx, "semantic_gate", False)):
            # The semantic gate is deliberately fail-closed.  A failed proof transaction must
            # never fall through to local rendering or the raw upstream model response.  A
            # pending selector runs before the first-byte claim and is fully quarantined here.
            replay = getattr(post_ctx, "semantic_replay", None)
            semantic_error = getattr(post_ctx, "semantic_error", None)
            if replay is None or semantic_error:
                semantic_status = getattr(post_ctx, "semantic_status", 503)
                return _semantic_delivery_error(409 if semantic_status == 409 else 503)
            semantic_started = time.perf_counter()
            pending_selection = getattr(post_ctx, "semantic_selection", None)
            if pending_selection is not None:
                selection_raw, selection_content_type, selection_error, selection_timed_out = \
                    await _buffer_semantic_selection(
                        get_client,
                        cfg,
                        path,
                        request,
                        body,
                    )
                try:
                    replay = pipeline.complete_semantic_selection(
                        post_ctx,
                        selection_raw,
                        selection_content_type,
                        timed_out=selection_timed_out,
                        upstream_error=selection_error,
                    )
                except TurnLifecycleError as exc:
                    log.warning("semantic selection terminalization conflicted: %s",
                                type(exc).__name__)
                    return _semantic_delivery_error(409)
                except Exception as exc:
                    log.warning("semantic selection terminalization failed closed: %s",
                                type(exc).__name__)
                    return _semantic_delivery_error(503)
                if replay is None or getattr(post_ctx, "semantic_selection", None) is not None:
                    log.error("semantic selector returned no terminal proof artifact")
                    return _semantic_delivery_error(503)
                post_ctx.semantic_replay = replay
            try:
                claimed = pipeline.store.turn_lifecycle.claim_delivery(
                    replay.lifecycle_key,
                    replay.attempt_index,
                    expected_logical_message_id=replay.logical_message_id,
                    expected_artifact_digest=replay.selected_artifact_digest,
                )
            except TurnLifecycleError as exc:
                log.warning("semantic delivery claim refused: %s", type(exc).__name__)
                return _semantic_delivery_error(409)
            except Exception as exc:
                log.warning("semantic delivery claim unavailable: %s", type(exc).__name__)
                return _semantic_delivery_error(503)

            # The durable claim is authoritative.  Cold response handling must verify the exact
            # same artifact rather than the pre-claim object held by the request path.
            post_ctx.semantic_replay = claimed

            async def semantic_bytes() -> AsyncIterator[bytes]:
                first_chunk_ms = (time.perf_counter() - semantic_started) * 1000.0
                emitted_normally = False
                try:
                    yield claimed.payload
                    # StreamingResponse has successfully consumed the one exact payload and asked
                    # the generator to finish.  Cancellation or disconnect while ``yield`` is
                    # suspended skips this assignment and may not unlock a later swipe.
                    emitted_normally = True
                finally:
                    if emitted_normally:
                        try:
                            pipeline.on_response(post_ctx, claimed.payload, claimed.content_type)
                        except Exception as exc:
                            log.warning("semantic response tee failed closed: %s", type(exc).__name__)
                        try:
                            pipeline.store.turn_lifecycle.complete_delivery(
                                claimed.lifecycle_key,
                                claimed.attempt_index,
                                expected_logical_message_id=claimed.logical_message_id,
                                expected_artifact_digest=claimed.selected_artifact_digest,
                            )
                        except Exception as exc:
                            # Bytes may already have reached the client, so they cannot be recalled.
                            # Keeping completion absent is the safe state: a retry replays this exact
                            # artifact and cannot advance the swipe pointer or context count.
                            log.warning(
                                "semantic delivery completion remained recoverable: %s",
                                type(exc).__name__,
                            )
                        try:
                            pipeline.record_response_trace(
                                post_ctx,
                                source="semantic",
                                status=200,
                                headers_ms=0.0,
                                first_chunk_ms=first_chunk_ms,
                                total_ms=(time.perf_counter() - semantic_started) * 1000.0,
                                byte_count=len(claimed.payload),
                                content_sha256=hashlib.sha256(claimed.payload).hexdigest(),
                                content_type=claimed.content_type,
                            )
                        except Exception as exc:
                            log.warning("semantic response trace failed closed: %s",
                                        type(exc).__name__)

            return StreamingResponse(
                semantic_bytes(),
                status_code=200,
                headers={
                    "content-type": claimed.content_type,
                    "cache-control": "no-store",
                },
            )
        if pipeline is not None and post_ctx is not None \
                and getattr(post_ctx, "local_response", None) is not None:
            local_started = time.perf_counter()
            try:
                local_raw, local_content_type = _encode_local_chat_response(post_ctx)
            except Exception as exc:
                # A local rendering optimization can never strand the request.  Fall through to
                # the normal upstream relay with the fully composed packet.
                log.warning("local response encoding failed open: %s", type(exc).__name__)
            else:
                async def local_bytes() -> AsyncIterator[bytes]:
                    first_chunk_ms = (time.perf_counter() - local_started) * 1000.0
                    try:
                        yield local_raw
                    finally:
                        try:
                            pipeline.on_response(post_ctx, local_raw, local_content_type)
                        except Exception as exc:
                            log.warning("local response tee failed open: %s", type(exc).__name__)
                        try:
                            pipeline.record_response_trace(
                                post_ctx,
                                source="local",
                                status=200,
                                headers_ms=0.0,
                                first_chunk_ms=first_chunk_ms,
                                total_ms=(time.perf_counter() - local_started) * 1000.0,
                                byte_count=len(local_raw),
                                content_sha256=hashlib.sha256(local_raw).hexdigest(),
                                content_type=local_content_type,
                            )
                        except Exception as exc:
                            log.warning("local response trace failed open: %s",
                                        type(exc).__name__)

                return StreamingResponse(
                    local_bytes(), status_code=200, media_type=local_content_type,
                    headers={"cache-control": "no-store"},
                )
        headers = {}
        authorization_values = []
        for name, value in request.headers.items():
            lname = name.lower()
            if lname in _SKIP_REQ or lname.startswith("x-aetherstate"):
                continue
            if lname == "authorization":
                authorization_values.append(value)
                continue
            headers[name] = value
        # Force identity: httpx injects its own accept-encoding otherwise, and a teeing proxy
        # must relay bytes it can also read. Content is unchanged; only transport encoding.
        headers["accept-encoding"] = "identity"
        authorization = next(
            (value for value in authorization_values if _authorization_is_usable(value)),
            None,
        )
        if authorization is not None:
            headers["Authorization"] = authorization
        elif cfg.upstream.api_key:
            headers["Authorization"] = f"Bearer {cfg.upstream.api_key}"
        elif authorization_values:
            headers["Authorization"] = authorization_values[0]

        if not cfg.upstream.base_url:
            not_configured = (
                b'{"error":{"message":"AetherState: upstream.base_url is not configured "'
                b'"(set it in config.toml, e.g. https://api.openai.com/v1)",'
                b'"type":"not_configured","code":502}}'
            )
            if pipeline is not None and post_ctx is not None:
                pipeline.record_response_trace(
                    post_ctx,
                    source="proxy",
                    status=502,
                    headers_ms=None,
                    first_chunk_ms=None,
                    total_ms=0.0,
                    byte_count=len(not_configured),
                    content_sha256=hashlib.sha256(not_configured).hexdigest(),
                    content_type="application/json",
                    error_type="not_configured",
                )
            return Response(
                content=not_configured,
                status_code=502, media_type="application/json")

        client = get_client()
        url = upstream_url(cfg.upstream.base_url, path, request.url.query)
        upstream_started = time.perf_counter()
        try:
            upstream_req = client.build_request(request.method, url, content=body, headers=headers)
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:  # 09 U1: OpenAI-shaped 502, no state effects
            log.warning("upstream unreachable: %s", type(exc).__name__)
            unreachable = (
                b'{"error":{"message":"AetherState: upstream unreachable",'
                b'"type":"upstream_unreachable","code":502}}'
            )
            if pipeline is not None and post_ctx is not None:
                pipeline.record_response_trace(
                    post_ctx,
                    source="upstream",
                    status=502,
                    headers_ms=None,
                    first_chunk_ms=None,
                    total_ms=(time.perf_counter() - upstream_started) * 1000.0,
                    byte_count=len(unreachable),
                    content_sha256=hashlib.sha256(unreachable).hexdigest(),
                    content_type="application/json",
                    error_type=type(exc).__name__,
                )
            return Response(
                content=unreachable,
                status_code=502, media_type="application/json")

        headers_ms = (time.perf_counter() - upstream_started) * 1000.0

        if pipeline is not None and post_ctx is not None:
            mark_player_lessons_delivered = getattr(
                pipeline, "mark_player_lessons_delivered", None
            )
            if callable(mark_player_lessons_delivered):
                try:
                    # Response headers prove only that the configured provider received and
                    # answered the request.  Error statuses still count as transport delivery;
                    # narrator adherence and complete response delivery are separate evidence.
                    mark_player_lessons_delivered(post_ctx)
                except Exception as exc:
                    log.warning(
                        "player lesson delivery evidence failed open: %s",
                        type(exc).__name__,
                    )

        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESP}

        tee_cap = 4 * 1024 * 1024      # bounded copy; oversized replies skip extraction

        guarded_success = bool(
            pipeline is not None
            and post_ctx is not None
            and getattr(post_ctx, "narration_guard", None)
            and 200 <= upstream.status_code < 300
        )
        if guarded_success:
            # A mechanically authoritative narrator reply is the one narrow exception to the
            # transparent stream.  Buffer it privately with a strict pre-append cap, decide it,
            # and only then create the ASGI response.  No candidate header or byte is visible
            # before the verdict, and interrupted/oversized candidates become the safe fallback.
            candidate = bytearray()
            buffer_error = ""
            try:
                async for chunk in upstream.aiter_raw():
                    if len(candidate) + len(chunk) > tee_cap:
                        buffer_error = "response_too_large"
                        break
                    candidate.extend(chunk)
            except httpx.HTTPError as exc:
                buffer_error = type(exc).__name__
                log.warning("guarded upstream response ended before verdict: %s",
                            type(exc).__name__)
            finally:
                await upstream.aclose()

            candidate_raw = bytes(candidate) if not buffer_error else b""
            candidate_type = upstream.headers.get("content-type", "")
            try:
                final_raw, final_type = pipeline.guard_response(
                    post_ctx,
                    candidate_raw,
                    candidate_type,
                )
            except Exception as exc:
                # The candidate is quarantined already.  An unavailable local guard cannot make
                # those bytes safe, so return a content-free error instead of releasing them.
                log.error("narration pre-display verdict unavailable: %s", type(exc).__name__)
                return _semantic_delivery_error(503)

            guarded_headers = {
                key: value for key, value in resp_headers.items()
                if key.lower() not in {
                    "content-type", "content-length", "content-encoding", "cache-control",
                }
            }
            guarded_headers["content-type"] = final_type
            guarded_headers["cache-control"] = "no-store"

            async def guarded_bytes() -> AsyncIterator[bytes]:
                first_chunk_ms = (time.perf_counter() - upstream_started) * 1000.0
                emitted_normally = False
                try:
                    yield final_raw
                    emitted_normally = True
                finally:
                    if emitted_normally:
                        try:
                            pipeline.on_response(post_ctx, final_raw, final_type)
                        except Exception as exc:
                            log.warning("guarded response tee failed open: %s",
                                        type(exc).__name__)
                        try:
                            pipeline.record_response_trace(
                                post_ctx,
                                source="upstream_guard",
                                status=upstream.status_code,
                                headers_ms=headers_ms,
                                first_chunk_ms=first_chunk_ms,
                                total_ms=(time.perf_counter() - upstream_started) * 1000.0,
                                byte_count=len(final_raw),
                                content_sha256=hashlib.sha256(final_raw).hexdigest(),
                                content_type=final_type,
                                error_type=buffer_error,
                            )
                        except Exception as exc:
                            log.warning("guarded response trace failed open: %s",
                                        type(exc).__name__)

            return StreamingResponse(
                guarded_bytes(),
                status_code=upstream.status_code,
                headers=guarded_headers,
            )

        async def stream_bytes() -> AsyncIterator[bytes]:
            buf = bytearray()
            response_hash = hashlib.sha256()
            response_bytes = 0
            first_chunk_ms: float | None = None
            stream_error = ""
            try:
                async for chunk in upstream.aiter_raw():
                    if chunk and first_chunk_ms is None:
                        first_chunk_ms = (time.perf_counter() - upstream_started) * 1000.0
                    response_bytes += len(chunk)
                    response_hash.update(chunk)
                    yield chunk  # verbatim — invariant 2 (09 U7: even malformed SSE relays)
                    if post_ctx is not None and len(buf) <= tee_cap:
                        buf.extend(chunk)   # copy AFTER yield: the tee never delays the stream
            except httpx.HTTPError as exc:  # 09 U4: upstream died mid-stream -> end as received
                stream_error = type(exc).__name__
                log.warning("upstream stream ended abnormally: %s", type(exc).__name__)
            finally:
                await upstream.aclose()
                # cold path starts HERE (03 SS1): text capture + extraction scheduling
                if pipeline is not None and post_ctx is not None and len(buf) <= tee_cap:
                    try:
                        if upstream.status_code < 400:
                            pipeline.on_response(post_ctx, bytes(buf),
                                                 upstream.headers.get("content-type", ""))
                        else:
                            pipeline.on_upstream_error(post_ctx, upstream.status_code, bytes(buf))
                    except Exception as exc:
                        log.warning("tee failed open: %s", type(exc).__name__)
                if pipeline is not None and post_ctx is not None:
                    try:
                        pipeline.record_response_trace(
                            post_ctx,
                            source="upstream",
                            status=upstream.status_code,
                            headers_ms=headers_ms,
                            first_chunk_ms=first_chunk_ms,
                            total_ms=(time.perf_counter() - upstream_started) * 1000.0,
                            byte_count=response_bytes,
                            content_sha256=response_hash.hexdigest(),
                            content_type=upstream.headers.get("content-type", ""),
                            error_type=stream_error,
                        )
                    except Exception as exc:
                        log.warning("response trace failed open: %s", type(exc).__name__)

        return StreamingResponse(stream_bytes(), status_code=upstream.status_code,
                                 headers=resp_headers, media_type=upstream.headers.get("content-type"))

    return router
