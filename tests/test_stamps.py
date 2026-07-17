"""stamps.py unit suite (05 SS4, 06 B.4, 09 I3)."""
from __future__ import annotations

import json

from aetherstate.stamps import MARKER, Stamp, parse_and_strip

SENT = "<<AETHER:v=1;session=chat-abc;turn=44;type=normal;speaker=Dane;user=Bean>>"


def _body(messages):
    return json.dumps({"model": "m", "messages": messages, "min_p": 0.05}).encode()


def test_no_marker_no_header_untouched_bytes():
    body = _body([{"role": "user", "content": "hi"}])
    stamp, out = parse_and_strip({}, body)
    assert stamp is None and out is body            # same object: zero-copy transparency


def test_header_only():
    body = _body([{"role": "user", "content": "hi"}])
    stamp, out = parse_and_strip({"X-AetherState-Session": "chat-9"}, body)
    assert stamp == Stamp(session="chat-9", source="header") and out is body


def test_sentinel_message_stripped_and_dropped():
    body = _body([{"role": "system", "content": SENT},
                  {"role": "user", "content": "hi"}])
    stamp, out = parse_and_strip({}, body)
    assert stamp.session == "chat-abc" and stamp.turn == 44
    assert stamp.gen_type == "normal" and stamp.user == "Bean" and stamp.source == "sentinel"
    doc = json.loads(out)
    assert len(doc["messages"]) == 1                # carrier message dropped entirely
    assert doc["min_p"] == 0.05                     # unknown fields survive re-serialization
    assert MARKER not in out


def test_sentinel_merged_into_other_content():
    """Strict/single-user post-processing merges roles (06 B.4) — strip from WITHIN content."""
    merged = f"You are a narrator.\n{SENT}\nStay in character."
    body = _body([{"role": "user", "content": merged}])
    stamp, out = parse_and_strip({}, body)
    assert stamp.session == "chat-abc"
    doc = json.loads(out)
    assert "narrator" in doc["messages"][0]["content"]
    assert "Stay in character" in doc["messages"][0]["content"]
    assert MARKER not in out


def test_sentinel_wins_on_mismatch():
    # Reversed 2026-07-04: the header is persisted frontend config and can go stale;
    # the sentinel is rebuilt per request. A stale header must never steal turns.
    body = _body([{"role": "system", "content": "<<AETHER:session=sentinel-id;type=swipe>>"}])
    stamp, out = parse_and_strip({"x-aetherstate-session": "header-id"}, body)
    assert stamp.session == "sentinel-id" and stamp.source == "both"
    assert stamp.gen_type == "swipe"                # volatile fields still come from sentinel


def test_sentinel_parses_explicit_branch_lineage():
    body = _body([{"role": "system", "content":
                   "<<AETHER:session=child;parent=source;fork=5;type=normal>>"}])
    stamp, out = parse_and_strip({"x-aetherstate-session": "stale-header"}, body)
    assert stamp == Stamp(
        session="child", gen_type="normal", parent="source", fork_pos=5, source="both")
    assert MARKER not in out


def test_sentinel_rejects_negative_or_malformed_fork_position():
    for raw in ("-1", "five"):
        body = _body([{"role": "system", "content":
                       f"<<AETHER:session=child;parent=source;fork={raw}>>"}])
        stamp, _ = parse_and_strip({}, body)
        assert stamp.parent == "source"
        assert stamp.fork_pos is None


def test_multimodal_part_list_content():
    body = _body([{"role": "user", "content": [
        {"type": "text", "text": f"{SENT}\nlook at this"}, {"type": "image_url", "image_url": {}}]}])
    stamp, out = parse_and_strip({}, body)
    assert stamp is not None and MARKER not in out
    assert b"look at this" in out


def test_marker_never_survives_weird_shapes():
    raw = json.dumps({"messages": "not-a-list <<AETHER:session=x>> tail"}).encode()
    stamp, out = parse_and_strip({}, raw)
    assert MARKER not in out                        # brute scrub path (09 I3)
