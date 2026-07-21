"""Deterministic OpenAI-compatible chat artifacts for gated RPG delivery.

Transparent non-RPG relay remains byte-for-byte. A gated RPG turn, however, needs an exact
fallback artifact durably persisted before visibility. This module is deliberately pure: the
same story, model, stream mode, and immutable artifact reference always produce the same bytes.
It also provides a strict completed-response decoder; malformed or truncated candidate streams
must never look like a successfully finalized story.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


JSON_CONTENT_TYPE = "application/json"
SSE_CONTENT_TYPE = "text/event-stream"


class ChatWireError(ValueError):
    """A chat artifact cannot be encoded or is not one complete supported response."""


@dataclass(frozen=True)
class ChatWireArtifact:
    raw: bytes
    content_type: str
    content_sha256: str
    story_sha256: str
    completion_id: str
    stream: bool


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _clean_text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ChatWireError(f"{label} must be text")
    if not allow_empty and not value.strip():
        raise ChatWireError(f"{label} cannot be empty")
    return value


def _completion_id(story: str, model: str, stream: bool, artifact_ref: str) -> str:
    material = json.dumps(
        {
            "artifact_ref": artifact_ref,
            "model": model,
            "story": story,
            "stream": stream,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "chatcmpl-aether-" + hashlib.blake2b(material, digest_size=12).hexdigest()


def encode_chat_story(
    story: str,
    *,
    model: str,
    stream: bool,
    artifact_ref: str,
) -> ChatWireArtifact:
    """Encode one immutable story into deterministic JSON or completed SSE bytes."""
    story = _clean_text(story, "story")
    model = _clean_text(model or "aetherstate-local", "model")
    artifact_ref = _clean_text(artifact_ref, "artifact_ref")
    stream = bool(stream)
    completion_id = _completion_id(story, model, stream, artifact_ref)
    base: dict[str, Any] = {
        "id": completion_id,
        "created": 0,
        "model": model,
    }
    if stream:
        content = {
            **base,
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": story},
                "finish_reason": None,
            }],
        }
        finish = {
            **base,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        raw = (
            b"data: "
            + json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n\ndata: "
            + json.dumps(finish, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n\ndata: [DONE]\n\n"
        )
        content_type = SSE_CONTENT_TYPE
    else:
        payload = {
            **base,
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": story},
                "finish_reason": "stop",
            }],
        }
        raw = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        content_type = JSON_CONTENT_TYPE
    return ChatWireArtifact(
        raw=raw,
        content_type=content_type,
        content_sha256=_sha256(raw),
        story_sha256=_sha256(story.encode("utf-8")),
        completion_id=completion_id,
        stream=stream,
    )


def _choice(doc: object, label: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ChatWireError(f"{label} must be an object")
    choices = doc.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ChatWireError(f"{label} must contain one choice")
    if choices[0].get("index", 0) != 0:
        raise ChatWireError(f"{label} choice index must be zero")
    return choices[0]


def _is_usage_only_event(doc: object, label: str) -> bool:
    """Recognize the one choice-less SSE event permitted by ``include_usage``."""
    if not isinstance(doc, dict) or doc.get("choices") != []:
        return False
    if not isinstance(doc.get("usage"), dict):
        raise ChatWireError(f"{label} empty choices require a usage object")
    return True


def _json_doc(raw: bytes, label: str) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChatWireError(f"{label} is not valid UTF-8 JSON") from exc


def decode_chat_story(raw: bytes, content_type: str = "") -> str:
    """Strictly decode one complete supported candidate response.

    SSE must terminate with [DONE]. Every data event before it must be valid JSON, so a malformed
    or cut stream cannot silently drop a dangerous clause and certify a partial story.
    """
    if not isinstance(raw, bytes) or not raw:
        raise ChatWireError("response bytes cannot be empty")
    is_sse = "text/event-stream" in str(content_type).lower() or raw.lstrip().startswith(b"data:")
    if not is_sse:
        choice = _choice(_json_doc(raw, "response"), "response")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else choice.get("text")
        return _clean_text(content, "response content")

    parts: list[str] = []
    done = False
    saw_event = False
    saw_usage = False
    normalized = raw.replace(b"\r\n", b"\n")
    for block in normalized.split(b"\n\n"):
        if not block.strip():
            continue
        data_lines = [
            line[5:].lstrip()
            for line in block.split(b"\n")
            if line.startswith(b"data:")
        ]
        if not data_lines:
            if all(not line.strip() or line.lstrip().startswith(b":")
                   for line in block.split(b"\n")):
                continue
            raise ChatWireError("SSE block contains unsupported non-data content")
        if done:
            raise ChatWireError("SSE contains data after [DONE]")
        payload = b"\n".join(data_lines).strip()
        if payload == b"[DONE]":
            done = True
            continue
        if saw_usage:
            raise ChatWireError("SSE contains data after terminal usage event")
        doc = _json_doc(payload, "SSE event")
        if _is_usage_only_event(doc, "SSE event"):
            if not saw_event:
                raise ChatWireError("SSE usage event precedes completion events")
            saw_usage = True
            continue
        saw_event = True
        choice = _choice(doc, "SSE event")
        delta = choice.get("delta")
        message = choice.get("message")
        content: object = None
        if isinstance(delta, dict):
            content = delta.get("content")
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if content is not None:
            parts.append(_clean_text(content, "SSE content", allow_empty=True))
    if not done:
        raise ChatWireError("SSE response is incomplete: missing [DONE]")
    if not saw_event:
        raise ChatWireError("SSE response has no completion events")
    return _clean_text("".join(parts), "SSE story")
