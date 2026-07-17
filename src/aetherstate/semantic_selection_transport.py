"""Isolated transport for bounded narration-plan ID selection.

This module is a one-way membrane.  It receives a code-authored realization plan and an ordinary
chat request, copies only that request's model identifier, and emits a fresh non-stream
OpenAI-compatible request.  Transcript messages, tools, credentials, prose, vendor extensions, and
arbitrary sampling parameters are never copied.  The buffered response must decode to one exact
JSON selection object and is revalidated against the sealed plan before it can leave this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
from typing import Any

from .capability_glossary import raw_fingerprint
from .narration_plan_runtime import (
    MAX_SELECTION_BYTES,
    NarrationPlanRuntimeError,
    build_narration_plan_request,
    validate_narration_plan_selection,
    validate_narration_realization_plan,
)
from .response_wire import ChatWireError, decode_chat_story


SELECTION_TRANSPORT_SCHEMA = "semantic-selection-transport-request/1"
SELECTION_SYSTEM_INSTRUCTION = (
    "You are AetherState's bounded narration-plan selector. Return exactly one JSON object "
    "matching narration-plan-selection/1. Choose only the supplied occurrence, claim, atom, and "
    "slot IDs. Do not invent keys or values. Do not return story prose, Markdown, code fences, "
    "commentary, tool calls, or any text outside the JSON object."
)

MAX_MODEL_BYTES = 256
MAX_ORIGINAL_REQUEST_BYTES = 8_000_000
MAX_PLAN_REQUEST_BYTES = 512_000
MAX_SELECTION_REQUEST_BYTES = 560_000
MAX_SELECTION_RESPONSE_BYTES = 128_000
MAX_SELECTION_OUTPUT_TOKENS = 16_384

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}$")
_RESPONSE_CONTENT_TYPES = frozenset({"application/json", "text/event-stream"})


class SemanticSelectionTransportError(ValueError):
    """An isolated request or buffered selection response violated its sealed contract."""


@dataclass(frozen=True)
class SealedSelectionTransportRequest:
    """Exact isolated request bytes and the identities they are allowed to carry."""

    request_bytes: bytes
    model: str
    plan_fingerprint: str
    plan_request_fingerprint: str
    request_fingerprint: str
    reasoning_hard_off: bool


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SemanticSelectionTransportError(
            "selection transport data must be finite canonical JSON"
        ) from exc


def _original_request_object(value: object) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        if not value or len(value) > MAX_ORIGINAL_REQUEST_BYTES:
            raise SemanticSelectionTransportError(
                "original request is empty or exceeds the bounded parse size"
            )
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SemanticSelectionTransportError("original request is not one JSON object") from exc
        if not isinstance(decoded, dict):
            raise SemanticSelectionTransportError("original request is not one JSON object")
        return decoded
    if not isinstance(value, Mapping):
        raise SemanticSelectionTransportError("original request must be bytes or an object")
    return value


def _model_from_original(value: object) -> str:
    model = _original_request_object(value).get("model")
    if not isinstance(model, str) or not model or model != model.strip() \
            or len(model.encode("utf-8")) > MAX_MODEL_BYTES \
            or _MODEL_ID.fullmatch(model) is None:
        raise SemanticSelectionTransportError(
            "original request model must be one strict bounded identifier"
        )
    return model


def _request_body(
    model: str,
    plan_request: Mapping[str, Any],
    *,
    reasoning_hard_off: bool,
) -> dict[str, Any]:
    plan_json = _canonical_bytes(plan_request)
    if len(plan_json) > MAX_PLAN_REQUEST_BYTES:
        raise SemanticSelectionTransportError("narration plan request exceeds its bounded size")
    body = {
        "max_tokens": MAX_SELECTION_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": SELECTION_SYSTEM_INSTRUCTION},
            {"role": "user", "content": plan_json.decode("utf-8")},
        ],
        "model": model,
        "response_format": {"type": "json_object"},
        "stream": False,
        "temperature": 0,
    }
    if reasoning_hard_off:
        body["reasoning"] = {"enabled": False}
        body["venice_parameters"] = {
            "disable_thinking": True,
            "strip_thinking_response": True,
        }
    return body


def build_semantic_selection_request(
    plan: object,
    original_request: object,
    *,
    reasoning_hard_off: bool = False,
) -> SealedSelectionTransportRequest:
    """Build a fresh request carrying only code-owned instructions, plan IDs, and model ID."""
    try:
        valid_plan = validate_narration_realization_plan(plan)
        plan_request = build_narration_plan_request(valid_plan)
    except NarrationPlanRuntimeError as exc:
        raise SemanticSelectionTransportError("narration realization plan is invalid") from exc
    model = _model_from_original(original_request)
    hard_off = bool(reasoning_hard_off)
    request_bytes = _canonical_bytes(
        _request_body(model, plan_request, reasoning_hard_off=hard_off)
    )
    if len(request_bytes) > MAX_SELECTION_REQUEST_BYTES:
        raise SemanticSelectionTransportError("isolated selection request exceeds its bounded size")
    return SealedSelectionTransportRequest(
        request_bytes=request_bytes,
        model=model,
        plan_fingerprint=valid_plan["fingerprint"],
        plan_request_fingerprint=plan_request["fingerprint"],
        request_fingerprint=raw_fingerprint(request_bytes),
        reasoning_hard_off=hard_off,
    )


def _validate_sealed_request(
    sealed: object,
    plan: object,
) -> dict[str, Any]:
    if not isinstance(sealed, SealedSelectionTransportRequest):
        raise SemanticSelectionTransportError("selection request is not a sealed transport request")
    if not isinstance(sealed.request_bytes, bytes) \
            or not sealed.request_bytes \
            or len(sealed.request_bytes) > MAX_SELECTION_REQUEST_BYTES \
            or raw_fingerprint(sealed.request_bytes) != sealed.request_fingerprint:
        raise SemanticSelectionTransportError("sealed selection request fingerprint is invalid")
    try:
        valid_plan = validate_narration_realization_plan(plan)
        plan_request = build_narration_plan_request(valid_plan)
    except NarrationPlanRuntimeError as exc:
        raise SemanticSelectionTransportError("sealed selection plan is invalid") from exc
    if sealed.plan_fingerprint != valid_plan["fingerprint"] \
            or sealed.plan_request_fingerprint != plan_request["fingerprint"]:
        raise SemanticSelectionTransportError("sealed selection request belongs to another plan")
    model = _model_from_original({"model": sealed.model})
    if not isinstance(sealed.reasoning_hard_off, bool):
        raise SemanticSelectionTransportError("sealed selector reasoning control is invalid")
    expected_bytes = _canonical_bytes(
        _request_body(
            model,
            plan_request,
            reasoning_hard_off=sealed.reasoning_hard_off,
        )
    )
    if sealed.request_bytes != expected_bytes:
        raise SemanticSelectionTransportError(
            "sealed selection request differs from its exact code-owned form"
        )
    return valid_plan


def _response_content_type(raw: bytes, content_type: object) -> str:
    if not isinstance(content_type, str):
        raise SemanticSelectionTransportError("selection response content type is invalid")
    base = content_type.split(";", 1)[0].strip().lower()
    if base not in _RESPONSE_CONTENT_TYPES:
        raise SemanticSelectionTransportError(
            "selection response content type is not JSON or event stream"
        )
    stripped = raw.lstrip()
    if base == "application/json" and stripped.startswith(b"data:"):
        raise SemanticSelectionTransportError(
            "selection response wire differs from its declared content type"
        )
    if base == "text/event-stream" and not stripped.startswith(b"data:"):
        raise SemanticSelectionTransportError(
            "selection response wire differs from its declared content type"
        )
    return base


def parse_semantic_selection_response(
    sealed_request: object,
    plan: object,
    response_bytes: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Decode one buffered OpenAI response and return only a plan-validated ID selection."""
    valid_plan = _validate_sealed_request(sealed_request, plan)
    if not isinstance(response_bytes, bytes) or not response_bytes:
        raise SemanticSelectionTransportError("selection response is empty")
    if len(response_bytes) > MAX_SELECTION_RESPONSE_BYTES:
        raise SemanticSelectionTransportError("selection response exceeds the bounded wire size")
    response_type = _response_content_type(response_bytes, content_type)
    try:
        story = decode_chat_story(response_bytes, response_type)
    except (ChatWireError, UnicodeError, ValueError) as exc:
        raise SemanticSelectionTransportError(
            "selection response is not one complete OpenAI JSON/SSE response"
        ) from exc
    story_bytes = story.encode("utf-8")
    if not story_bytes or len(story_bytes) > MAX_SELECTION_BYTES:
        raise SemanticSelectionTransportError("decoded selection exceeds the bounded JSON size")
    if story != story.strip():
        raise SemanticSelectionTransportError(
            "selection response contains text outside its JSON object"
        )
    try:
        selection = json.loads(story)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise SemanticSelectionTransportError(
            "selection response is not exactly one JSON object; prose or Markdown is forbidden"
        ) from exc
    if not isinstance(selection, dict):
        raise SemanticSelectionTransportError("selection response JSON must be one object")
    try:
        return validate_narration_plan_selection(selection, valid_plan)
    except NarrationPlanRuntimeError as exc:
        raise SemanticSelectionTransportError(
            f"selection failed the sealed plan contract: {exc}"
        ) from exc
