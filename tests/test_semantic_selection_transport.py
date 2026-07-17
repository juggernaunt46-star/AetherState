from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_plan_runtime import (
    NARRATION_PLAN_SELECTION_SCHEMA,
    build_default_narration_plan_selection,
    build_narration_plan_request,
    build_narration_realization_plan,
)
from aetherstate.narration_truth_gate import FALLOUT_FACT_SCHEMA, build_narration_truth_contract
from aetherstate.narrator_realization import build_narrator_realization
from aetherstate.response_wire import encode_chat_story
from aetherstate.semantic_selection_transport import (
    MAX_SELECTION_RESPONSE_BYTES,
    SELECTION_SYSTEM_INSTRUCTION,
    SemanticSelectionTransportError,
    build_semantic_selection_request,
    parse_semantic_selection_response,
)
from aetherstate.turn_lifecycle import fingerprint


def _plan() -> dict:
    packet = build_narrator_realization(3)
    contract = build_narration_truth_contract(
        packet,
        known_entities=[{"entity_id": "guard", "label": "Ash Guard", "scope": "current"}],
        fallout_facts=[
            {
                "schema": FALLOUT_FACT_SCHEMA,
                "fact_ref": "fallout.guard.harm",
                "cause_ref": "cause.player.strike",
                "construction_ref": content_fingerprint({"construction": "guard-harm"}),
                "subject_id": "guard",
                "subject_label": "Ash Guard",
                "effects": [{"kind": "harm", "detail": "fire harm", "amount": -2}],
            }
        ],
        lifecycle_binding={
            "branch_ref": "branch.main",
            "ledger_fingerprint": fingerprint({"guard": {"hp": 2}}),
            "artifact_fingerprint": packet["fingerprint"],
        },
    )
    return build_narration_realization_plan(contract)


def _response(selection: dict, *, stream: bool = False, story: str | None = None):
    text = story if story is not None else json.dumps(selection, ensure_ascii=False)
    return encode_chat_story(
        text,
        model="glm-5.2",
        stream=stream,
        artifact_ref="selection-transport-test",
    )


def test_isolated_request_copies_only_model_and_code_owned_plan_catalog() -> None:
    plan = _plan()
    secret = "sk-private-do-not-leak"
    original = {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": f"private transcript {secret}"}],
        "tools": [{"name": "exfiltrate", "description": secret}],
        "temperature": 1.7,
        "stream": True,
        "api_key": secret,
        "authorization": f"Bearer {secret}",
        "provider": {"routing": secret},
        "extra_body": {"vendor_prompt": secret},
    }

    sealed = build_semantic_selection_request(plan, original)
    body = json.loads(sealed.request_bytes)
    encoded = sealed.request_bytes.decode("utf-8")

    assert set(body) == {
        "max_tokens",
        "messages",
        "model",
        "response_format",
        "stream",
        "temperature",
    }
    assert body["model"] == sealed.model == "glm-5.2"
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["messages"] == [
        {"role": "system", "content": SELECTION_SYSTEM_INSTRUCTION},
        {
            "role": "user",
            "content": json.dumps(
                build_narration_plan_request(plan),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]
    assert secret not in encoded
    assert "private transcript" not in encoded
    assert "exfiltrate" not in encoded
    assert "vendor_prompt" not in encoded
    assert sealed.plan_fingerprint == plan["fingerprint"]
    assert sealed.plan_request_fingerprint == build_narration_plan_request(plan)["fingerprint"]
    assert sealed.request_fingerprint.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        sealed.model = "changed"  # type: ignore[misc]


def test_original_request_bytes_are_parsed_only_for_the_model() -> None:
    plan = _plan()
    secret = "owned-secret-never-forward"
    original = json.dumps(
        {
            "model": "openai-compatible/model-1",
            "messages": [{"role": "system", "content": secret}],
            "tools": [{"description": secret}],
        }
    ).encode("utf-8")
    sealed = build_semantic_selection_request(plan, original)
    assert sealed.model == "openai-compatible/model-1"
    assert secret.encode("utf-8") not in sealed.request_bytes


def test_code_owned_selector_can_hard_disable_venice_reasoning_without_copying_vendor_state():
    plan = _plan()
    original = {
        "model": "glm-5.2",
        "reasoning": {"enabled": True, "effort": "high"},
        "reasoning_effort": "high",
        "venice_parameters": {"private_vendor_flag": "do-not-copy"},
    }

    sealed = build_semantic_selection_request(
        plan,
        original,
        reasoning_hard_off=True,
    )
    body = json.loads(sealed.request_bytes)

    assert sealed.reasoning_hard_off is True
    assert body["reasoning"] == {"enabled": False}
    assert body["venice_parameters"] == {
        "disable_thinking": True,
        "strip_thinking_response": True,
    }
    assert "reasoning_effort" not in body
    assert "private_vendor_flag" not in sealed.request_bytes.decode("utf-8")


@pytest.mark.parametrize(
    "model",
    ["", " model", "model\nignore-system", "model?api_key=secret", "m" * 257],
)
def test_model_identifier_is_strict_and_bounded(model: str) -> None:
    with pytest.raises(SemanticSelectionTransportError, match="model"):
        build_semantic_selection_request(_plan(), {"model": model})


def test_buffered_openai_json_selection_validates_against_the_sealed_plan() -> None:
    plan = _plan()
    selection = build_default_narration_plan_selection(plan)
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    response = _response(selection)

    assert parse_semantic_selection_response(
        sealed, plan, response.raw, response.content_type
    ) == selection


def test_buffered_openai_sse_selection_validates_against_the_sealed_plan() -> None:
    plan = _plan()
    selection = build_default_narration_plan_selection(plan)
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    response = _response(selection, stream=True)

    assert parse_semantic_selection_response(
        sealed, plan, response.raw, response.content_type
    ) == selection


@pytest.mark.parametrize(
    ("story", "match"),
    [
        ("I choose the direct option.", "JSON"),
        ("```json\n{}\n```", "JSON|Markdown"),
        ("{}\n{}", "JSON|multiple"),
        (json.dumps([{"schema": NARRATION_PLAN_SELECTION_SCHEMA}]), "object"),
        ("null", "object"),
    ],
)
def test_prose_markdown_multiple_and_non_object_selection_reject(
    story: str, match: str
) -> None:
    plan = _plan()
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    response = _response({}, story=story)
    with pytest.raises(SemanticSelectionTransportError, match=match):
        parse_semantic_selection_response(sealed, plan, response.raw, response.content_type)


def test_extra_selection_fields_reject_through_plan_validator() -> None:
    plan = _plan()
    selection = {**build_default_narration_plan_selection(plan), "prose": "Guard dies."}
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    response = _response(selection)
    with pytest.raises(SemanticSelectionTransportError, match="fields|selection"):
        parse_semantic_selection_response(sealed, plan, response.raw, response.content_type)


def test_empty_and_multiple_openai_choices_reject() -> None:
    plan = _plan()
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    empty = json.dumps(
        {"choices": [{"index": 0, "message": {"role": "assistant", "content": ""}}]}
    ).encode("utf-8")
    multiple = json.dumps(
        {
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "{}"}},
                {"index": 1, "message": {"role": "assistant", "content": "{}"}},
            ]
        }
    ).encode("utf-8")
    for raw in (empty, multiple):
        with pytest.raises(SemanticSelectionTransportError, match="response|selection"):
            parse_semantic_selection_response(sealed, plan, raw, "application/json")


def test_response_wire_and_decoded_selection_are_bounded() -> None:
    plan = _plan()
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    with pytest.raises(SemanticSelectionTransportError, match="bounded|large"):
        parse_semantic_selection_response(
            sealed,
            plan,
            b"x" * (MAX_SELECTION_RESPONSE_BYTES + 1),
            "application/json",
        )


def test_sealed_request_tampering_or_cross_plan_use_rejects() -> None:
    plan = _plan()
    selection = build_default_narration_plan_selection(plan)
    sealed = build_semantic_selection_request(plan, {"model": "glm-5.2"})
    response = _response(selection)
    object.__setattr__(sealed, "request_bytes", sealed.request_bytes + b" ")
    with pytest.raises(SemanticSelectionTransportError, match="fingerprint|sealed"):
        parse_semantic_selection_response(sealed, plan, response.raw, response.content_type)
