"""MockUpstream (11 SS1-SS2): in-process ASGI app serving scripted replies.

Personas here are Phase-0 scope: exact-bytes SSE replay + fault injection.
Capability personas (rung tiers) arrive in Phase 3 with the probe protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Reply:
    status: int = 200
    headers: dict = field(default_factory=lambda: {"content-type": "application/json"})
    body: bytes = b"{}"
    sse_chunks: Optional[list[bytes]] = None   # exact bytes, sent as-is, in order
    fault: Optional[str] = None                # midstream_cut | malformed_sse handled via chunks


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict
    raw_headers: list[tuple[str, str]]
    body: bytes


class MockUpstream:
    """Minimal raw-ASGI app: pops the next scripted Reply per request; records everything."""

    def __init__(self) -> None:
        self.script: list[Reply] = []
        self.requests: list[Recorded] = []

    def enqueue(self, reply: Reply) -> None:
        self.script.append(reply)

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        raw_headers = [(k.decode(), v.decode()) for k, v in scope["headers"]]
        self.requests.append(Recorded(
            method=scope["method"],
            path=scope["path"],
            headers=dict(raw_headers),
            raw_headers=raw_headers,
            body=body))

        reply = self.script.pop(0) if self.script else Reply(status=500, body=b'{"error":"unscripted"}')
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in reply.headers.items()]
        await send({"type": "http.response.start", "status": reply.status, "headers": raw_headers})
        if reply.sse_chunks is not None:
            for i, chunk in enumerate(reply.sse_chunks):
                last = i == len(reply.sse_chunks) - 1
                if reply.fault == "midstream_cut" and last:
                    # abrupt end: send what we have, then close without the rest
                    await send({"type": "http.response.body", "body": chunk, "more_body": False})
                    return
                await send({"type": "http.response.body", "body": chunk, "more_body": not last})
        else:
            await send({"type": "http.response.body", "body": reply.body, "more_body": False})
