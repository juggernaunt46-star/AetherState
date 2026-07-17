from __future__ import annotations

import json

import pytest

from aetherstate.response_wire import (
    ChatWireError,
    JSON_CONTENT_TYPE,
    SSE_CONTENT_TYPE,
    decode_chat_story,
    encode_chat_story,
)


def test_json_artifact_is_deterministic_and_round_trips_unicode():
    first = encode_chat_story(
        "Éranmor watches the frost settle.",
        model="glm-5.2",
        stream=False,
        artifact_ref="envelope.branch.turn.attempt",
    )
    second = encode_chat_story(
        "Éranmor watches the frost settle.",
        model="glm-5.2",
        stream=False,
        artifact_ref="envelope.branch.turn.attempt",
    )

    assert first == second
    assert first.content_type == JSON_CONTENT_TYPE
    assert decode_chat_story(first.raw, first.content_type) == "Éranmor watches the frost settle."
    assert json.loads(first.raw)["created"] == 0
    assert len(first.content_sha256) == len(first.story_sha256) == 64


def test_sse_artifact_is_complete_deterministic_and_round_trips():
    artifact = encode_chat_story(
        "The ledger records one exact outcome.",
        model="glm-5.2",
        stream=True,
        artifact_ref="envelope.2",
    )

    assert artifact.content_type == SSE_CONTENT_TYPE
    assert artifact.raw.endswith(b"data: [DONE]\n\n")
    assert decode_chat_story(artifact.raw, artifact.content_type) \
        == "The ledger records one exact outcome."
    assert artifact == encode_chat_story(
        "The ledger records one exact outcome.",
        model="glm-5.2",
        stream=True,
        artifact_ref="envelope.2",
    )


def test_artifact_identity_changes_wire_identity_without_changing_story():
    left = encode_chat_story("Same truth.", model="m", stream=False, artifact_ref="attempt.1")
    right = encode_chat_story("Same truth.", model="m", stream=False, artifact_ref="attempt.2")

    assert left.completion_id != right.completion_id
    assert left.raw != right.raw
    assert left.story_sha256 == right.story_sha256


@pytest.mark.parametrize(
    "raw, content_type",
    [
        (b"", JSON_CONTENT_TYPE),
        (b"{broken", JSON_CONTENT_TYPE),
        (b'{"choices":[]}', JSON_CONTENT_TYPE),
        (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', SSE_CONTENT_TYPE),
        (b"data: {broken}\n\ndata: [DONE]\n\n", SSE_CONTENT_TYPE),
        (b"data: [DONE]\n\ndata: {}\n\n", SSE_CONTENT_TYPE),
    ],
)
def test_malformed_or_partial_candidate_fails_closed(raw: bytes, content_type: str):
    with pytest.raises(ChatWireError):
        decode_chat_story(raw, content_type)


def test_sse_keepalive_metadata_is_allowed_but_story_is_required():
    raw = (
        b": keepalive\n\n"
        b'data: {"choices":[{"index":0,"delta":{"content":"Ready."}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    assert decode_chat_story(raw, SSE_CONTENT_TYPE) == "Ready."
