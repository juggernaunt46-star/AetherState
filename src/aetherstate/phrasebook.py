"""Local deterministic prose-construction glossary for the semantic perception ladder.

The phrasebook translates parameterized constructions into evidence. It never sees an LLM,
constructs no prompt, and owns no outcome or state mutation. Grounding and authority still follow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


_TOKEN_RE = re.compile(r"[a-z0-9']+", re.IGNORECASE)
_TEMPLATE_RE = re.compile(r"\{([a-z_]+)\}|([a-z0-9']+)", re.IGNORECASE)
_STEM_SUFFIXES = ("ing", "edly", "ed", "es", "s")
_SLOT_RANGES = {"person": (1, 4), "weapon": (1, 4), "place": (1, 5), "terms": (1, 10)}
_LEADING_SLOT_WORDS = {"a", "an", "the", "my", "his", "her", "their", "our", "your"}


def _stem(word: str) -> str:
    word = "".join(re.findall(r"[a-z0-9]+", word.lower()))
    if len(word) <= 3:
        return word
    for suffix in _STEM_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[:-len(suffix)]
            break
    if len(word) >= 4 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    if len(word) >= 4 and word.endswith("e"):
        word = word[:-1]
    return word


def normalize(text: str) -> str:
    return " ".join(match.group(0).lower() for match in _TOKEN_RE.finditer(text or ""))


@dataclass(frozen=True)
class Construction:
    id: str
    action: str
    skill: str
    templates: tuple[str, ...]
    target_slot: str = ""
    instrument_slot: str = ""
    attack: bool = False


@dataclass(frozen=True)
class PhrasebookMatch:
    construction_id: str
    action: str
    skill: str
    template: str
    start: int
    end: int
    slots: dict[str, str]
    bindings: dict[str, str]
    target: Optional[str] = None
    instrument: Optional[str] = None
    attack: bool = False


@dataclass(frozen=True)
class _Token:
    text: str
    stem: str
    start: int
    end: int


def _default_path() -> Path:
    return Path(__file__).with_name("registry") / "mechanics_phrasebook.toml"


@lru_cache(maxsize=8)
def load(path: str = "") -> tuple[Construction, ...]:
    source = Path(path) if path else _default_path()
    with source.open("rb") as handle:
        doc = tomllib.load(handle)
    if int(doc.get("schema", 0)) != 1:
        raise ValueError(f"unsupported mechanics phrasebook schema: {doc.get('schema')}")
    out = []
    for row in doc.get("construction", []):
        templates = tuple(str(value).strip() for value in row.get("templates", []) if str(value).strip())
        if not row.get("id") or not row.get("skill") or not templates:
            raise ValueError("phrasebook construction requires id, skill, and templates")
        out.append(Construction(
            id=str(row["id"]),
            action=str(row.get("action") or row["id"]),
            skill=str(row["skill"]),
            templates=templates,
            target_slot=str(row.get("target_slot") or ""),
            instrument_slot=str(row.get("instrument_slot") or ""),
            attack=bool(row.get("attack", False)),
        ))
    return tuple(out)


def _template_parts(template: str) -> tuple[tuple[str, str], ...]:
    parts = []
    for match in _TEMPLATE_RE.finditer(template):
        if match.group(1):
            slot = match.group(1).lower()
            if slot not in _SLOT_RANGES:
                raise ValueError(f"unknown phrasebook slot {{{slot}}}")
            parts.append(("slot", slot))
        else:
            parts.append(("literal", _stem(match.group(2))))
    return tuple(parts)


def _bind_slot(slot: str, words: list[str], values: dict[str, str] | None) -> Optional[str]:
    phrase_words = [word.lower() for word in words]
    while phrase_words and phrase_words[0] in _LEADING_SLOT_WORDS:
        phrase_words.pop(0)
    phrase = " ".join(phrase_words)
    if values is None:
        return phrase or None
    exact = values.get(phrase)
    if exact:
        return exact
    padded = f" {phrase} "
    hits = [(len(key.split()), canonical) for key, canonical in values.items()
            if key and f" {key} " in padded]
    if not hits:
        return None
    best = max(size for size, _canonical in hits)
    canonicals = {canonical for size, canonical in hits if size == best}
    return next(iter(canonicals)) if len(canonicals) == 1 else None


def _match_from(tokens: list[_Token], parts: tuple[tuple[str, str], ...], start: int,
                slot_values: dict[str, dict[str, str]], part_index: int = 0,
                token_index: Optional[int] = None,
                captures: Optional[dict[str, tuple[int, int]]] = None):
    token_index = start if token_index is None else token_index
    captures = {} if captures is None else captures
    if part_index == len(parts):
        return token_index, captures
    kind, value = parts[part_index]
    if kind == "literal":
        if token_index >= len(tokens) or tokens[token_index].stem != value:
            return None
        return _match_from(tokens, parts, start, slot_values, part_index + 1,
                           token_index + 1, captures)
    low, high = _SLOT_RANGES[value]
    for size in range(low, min(high, len(tokens) - token_index) + 1):
        words = [token.text for token in tokens[token_index:token_index + size]]
        if _bind_slot(value, words, slot_values.get(value)) is None:
            continue
        next_captures = {**captures, value: (token_index, token_index + size)}
        found = _match_from(tokens, parts, start, slot_values, part_index + 1,
                            token_index + size, next_captures)
        if found is not None:
            return found
    return None


def match(text: str, slot_values: Optional[dict[str, dict[str, str]]] = None,
          path: str = "") -> list[PhrasebookMatch]:
    """Return grounded construction evidence found in unchanged local prose."""
    slot_values = slot_values or {}
    tokens = [_Token(m.group(0).lower(), _stem(m.group(0)), m.start(), m.end())
              for m in _TOKEN_RE.finditer(text or "")]
    found: list[PhrasebookMatch] = []
    for construction in load(path):
        for template in construction.templates:
            parts = _template_parts(template)
            for start in range(len(tokens)):
                result = _match_from(tokens, parts, start, slot_values)
                if result is None:
                    continue
                end_index, captures = result
                slots: dict[str, str] = {}
                bindings: dict[str, str] = {}
                for slot, (lo, hi) in captures.items():
                    words = [token.text for token in tokens[lo:hi]]
                    slots[slot] = " ".join(words)
                    binding = _bind_slot(slot, words, slot_values.get(slot))
                    if binding:
                        bindings[slot] = binding
                found.append(PhrasebookMatch(
                    construction_id=construction.id,
                    action=construction.action,
                    skill=construction.skill,
                    template=template,
                    start=tokens[start].start,
                    end=tokens[end_index - 1].end,
                    slots=slots,
                    bindings=bindings,
                    target=bindings.get(construction.target_slot) if construction.target_slot else None,
                    instrument=(bindings.get(construction.instrument_slot)
                                if construction.instrument_slot else None),
                    attack=construction.attack,
                ))
                break
    unique: dict[tuple[str, int, int], PhrasebookMatch] = {}
    for row in found:
        key = (row.construction_id, row.start, row.end)
        unique.setdefault(key, row)
    return sorted(unique.values(), key=lambda row: (row.start, -(row.end - row.start),
                                                     row.construction_id))
