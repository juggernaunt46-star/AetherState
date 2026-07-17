"""Identity stamps: L1 header + L2 sentinel (planning/05 SS4, 06 B.4, 03 SS1).

Leak-proofing (09 I3): if structured stripping cannot verify removal, a brute textual pass
removes any <<AETHER:...>> span. A sentinel can never reach the upstream model.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

SENTINEL_LINE = re.compile(r"^\s*<<AETHER:([^>]*)>>\s*$", re.MULTILINE)
SENTINEL_ANY = re.compile(r"<<AETHER:[^>]*>>\s?")
MARKER = b"<<AETHER:"


@dataclass
class Stamp:
    session: str
    turn: Optional[int] = None
    gen_type: str = "normal"      # normal|swipe|regenerate|continue|impersonate|quiet
    speaker: Optional[str] = None
    card_role: Optional[str] = None  # narrator|character|legacy/unknown
    user: Optional[str] = None
    parent: Optional[str] = None    # explicit branch parent external session id
    fork_pos: Optional[int] = None  # canonical transcript position inherited from parent
    source: str = "header"        # header | sentinel | both


def _parse_kv(kv: str) -> dict:
    out = {}
    for part in kv.split(";"):
        key, _, val = part.partition("=")
        if key.strip():
            out[key.strip()] = val.strip()
    return out


def _strip_content(content):
    """Remove sentinel lines from a message content (str or multimodal part-list)."""
    found = None
    if isinstance(content, str):
        m = SENTINEL_LINE.search(content) or SENTINEL_ANY.search(content)
        if m:
            found = m.group(1) if m.re is SENTINEL_LINE else m.group(0)[len("<<AETHER:"):-2]
            content = SENTINEL_ANY.sub("", SENTINEL_LINE.sub("", content))
        return content, found
    if isinstance(content, list):
        new_parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text, f = _strip_content(part["text"])
                found = found or f
                part = {**part, "text": text}
            new_parts.append(part)
        return new_parts, found
    return content, None


def parse_and_strip(headers: dict, body: bytes, header_name: str = "x-aetherstate-session",
                    ) -> tuple[Optional[Stamp], bytes]:
    """Returns (stamp | None, body ready to forward). Never lets a sentinel survive in the output.

    Raises only on JSON bodies that *contain* the marker but cannot be re-serialized —
    callers treat any raise as fail-open passthrough of a brute-scrubbed body.
    """
    header_session = None
    for k, v in headers.items():
        if k.lower() == header_name:
            header_session = v.strip()

    if MARKER not in body:  # cheap path: untouched bytes (transparency)
        if header_session:
            return Stamp(session=header_session, source="header"), body
        return None, body

    doc = json.loads(body)  # marker present -> we must parse to strip
    kv_raw = None
    messages = doc.get("messages")
    if isinstance(messages, list):
        kept = []
        for msg in messages:
            if isinstance(msg, dict):
                new_content, found = _strip_content(msg.get("content"))
                if found is not None:
                    kv_raw = kv_raw or found
                    msg = {**msg, "content": new_content}
                    if msg.get("role") == "system" and isinstance(new_content, str) \
                            and not new_content.strip():
                        continue  # sentinel-only carrier message: drop entirely
            kept.append(msg)
        doc["messages"] = kept

    out = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    if MARKER in out:  # nesting/exotic shape: brute scrub — leaking is not an option (09 I3)
        out = SENTINEL_ANY.sub("", out.decode(errors="replace")).encode()

    stamp = None
    if kv_raw is not None:
        kv = _parse_kv(kv_raw)
        stamp = Stamp(
            session=kv.get("session", "") or header_session or "",
            turn=int(kv["turn"]) if kv.get("turn", "").isdigit() else None,
            gen_type=kv.get("type", "normal"),
            speaker=kv.get("speaker") or None,
            card_role=(kv.get("card_role") or "").strip().lower()[:32] or None,
            user=kv.get("user") or None,
            parent=kv.get("parent") or None,
            fork_pos=int(kv["fork"]) if kv.get("fork", "").isdigit() else None,
            source="both" if header_session else "sentinel")
        if header_session and kv.get("session") and header_session != kv["session"]:
            # L2 (sentinel) wins on mismatch — REVERSED from 01 SS6 after the live
            # 2026-07-04 incident: the header lives in the frontend's PERSISTED global
            # settings; when the extension cannot rewrite it (ST build differences),
            # it goes stale and routes every chat's turns into one old session. The
            # sentinel is rebuilt per request from live chat context and cannot stale.
            stamp.session = kv["session"]
    elif header_session:
        stamp = Stamp(session=header_session, source="header")
    if stamp and not stamp.session:
        stamp = None
    return stamp, out
