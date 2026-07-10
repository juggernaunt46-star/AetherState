"""Canonicalization + hashing for the L3 heuristic session path (03 SS2.2, 08 B1).

The canonical transcript is the stable core of a chat: system messages dropped (WI/AN/depth
injections churn there — Proxy research SS2 pitfall #1), sentinels stripped, whitespace and
markdown emphasis normalized. Two hash kinds:

- content_hash: identifies one message body (text only, role-free) — feeds the B1 inverted
  index, so alignment survives role churn from post-processing merges (08 S8/S9).
- chain_hash at position i: identifies the exact prefix [0..i] including roles (vLLM
  chained-block model) — one equal chain hash proves whole-prefix identity.

Hash function: blake2b(digest_size=8) — stdlib, deterministic, no C-extension dependency.
(Planning drafts said xxh64; substituted to avoid a dep. Same 64-bit collision profile class.)
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .stamps import SENTINEL_ANY

_WS = re.compile(r"\s+")
_MD = re.compile(r"[*_`~]+")
# "Name: " line prefix for splitting single-user-collapsed blobs (Strict / KoboldLite TC).
_NAME_LINE = re.compile(r"^[^\s:][^:\n]{0,31}:\s", re.MULTILINE)

SEED = b"aetherstate-l3-v1"
_COLLAPSED_MIN_PREFIXES = 3   # a lone "hi" must never be mistaken for a collapsed transcript


@dataclass(frozen=True)
class CanonMsg:
    role: str            # user | assistant | text (text = collapsed blob segment, role unknown)
    text: str
    content_hash: str


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = SENTINEL_ANY.sub("", text)
    text = _MD.sub("", text)
    return _WS.sub(" ", text).strip()


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # multimodal part-list: concatenate the text parts
        return " ".join(p.get("text", "") for p in content
                        if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def _mk(role: str, raw: str) -> CanonMsg | None:
    text = normalize(raw)
    if not text:
        return None
    return CanonMsg(role, text, content_hash(text))


def split_collapsed(raw: str) -> list[str]:
    """Split a single-user-collapsed transcript blob on `Name: ` line prefixes (03 SS2.2)."""
    starts = [m.start() for m in _NAME_LINE.finditer(raw)]
    if len(starts) < _COLLAPSED_MIN_PREFIXES:
        return [raw]
    if starts[0] != 0:
        starts = [0] + starts
    return [raw[a:b] for a, b in zip(starts, starts[1:] + [len(raw)])]


def canonicalize(messages: list) -> list[CanonMsg]:
    out: list[CanonMsg] = []
    raws: list[str] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") == "system":
            continue    # system churn (WI/AN/depth) never enters the transcript core
        raw = _text_of(m.get("content"))
        msg = _mk(str(m.get("role", "")), raw)
        if msg:
            out.append(msg)
            raws.append(raw)
    if len(out) == 1:   # possible single-user-collapsed payload -> text-mode canonicalization
        parts = split_collapsed(raws[0])
        if len(parts) > 1:
            out = [c for c in (_mk("text", p) for p in parts) if c]
    return out


def chain(msgs: list[CanonMsg]) -> list[str]:
    """Chained prefix hashes: chain[i] commits to roles+contents of msgs[0..i]."""
    h = SEED
    out = []
    for m in msgs:
        h = hashlib.blake2b(h + m.role.encode() + bytes.fromhex(m.content_hash),
                            digest_size=8).digest()
        out.append(h.hex())
    return out
