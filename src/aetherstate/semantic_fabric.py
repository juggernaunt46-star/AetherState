"""Shared, recognition-only translation memories for the Semantic World Fabric.

The fabric is deliberately smaller than a parser and weaker than an authority system.  It turns
surface language into content-addressed candidates with explicit semantic valence.  Contextual
semantics may bind those candidates into an ActionFrame or another typed frame; WorldLex and the
state reducers remain the only paths from recognized meaning to authorized, executable mechanics.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .capability_glossary import (
    GLOSSARY_SCHEMA,
    CapabilityGlossary,
    GlossaryError,
    content_fingerprint,
    normalize_phrase,
)


FABRIC_MANIFEST_SCHEMA = "aetherstate-semantic-fabric/1"
TRANSLATION_MEMORY_SCHEMA = "semantic-translation-memory/2"
LEGACY_TRANSLATION_MEMORY_SCHEMA = "semantic-translation-memory/1"
SOURCES_SCHEMA = "semantic-fabric-sources/1"
MEANING_SCHEMA = "semantic-fabric-meaning/1"
MEANING_RECEIPT_SCHEMA = "semantic-fabric-meaning-receipt/1"

LEX_IDS = ("capability", "referent", "scene", "action", "claim")
PACK_LEX_IDS = tuple(item for item in LEX_IDS if item != "capability")

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# ActionLex remains recognition-only, but Tier-0 consumes ``features.action_class`` as topology.
# Keep that one consumed field code-owned: corpus authors may extend terms and genre surfaces for a
# stable concept, but resealing the corpus cannot turn inspection into an attack. Unknown concepts
# may still be recognized when they do not claim a code-consumed action class.
_ACTION_CLASS_CONTRACTS = {
    "action.communicate": "communication",
    "action.conceal": "concealment",
    "action.create": "creation",
    "action.destroy": "destruction",
    "action.detect": "detection",
    "action.inspect": "inspection",
    "action.kill_attempt": "kill_attempt",
    "action.move": "movement",
    "action.negotiate": "social_influence",
    "action.repair": "repair_or_heal",
    "action.restrain": "restraint",
    "action.transfer": "transfer",
    "action.transform": "transformation",
    "action.use_capability": "capability_use",
    "action.weapon_attack": "weapon_attack",
}
_FORBIDDEN_ACTION_FEATURE_KEYS = frozenset(
    {
        "authorized",
        "authorization",
        "executable",
        "execution_authorized",
        "mechanically_actionable",
        "mechanic_allowed",
        "mechanics_allowed",
        "mechanic_scope_allowed",
        "receipt_id",
        "receipt_ref",
        "receipt_admitted",
        "receipt_bypass",
        "bypass_receipt",
        "settlement",
        "settlement_op",
        "reducer_op",
        "damage",
        "damage_amount",
        "hp_delta",
        "cost",
        "resource_cost",
        "roll",
        "roll_formula",
        "success",
        "outcome",
        "spawn_op",
        "effect_op",
    }
)

_CLAIM_GUARD_ONLY_FEATURE_KEYS = frozenset(
    {
        "admitted",
        "authorized",
        "deliberate_lie",
        "establishes_facthood",
        "establishes_knowledge",
        "establishes_occurrence",
        "establishes_truth",
        "executable",
        "fulfilled",
        "mechanically_actionable",
        "proves_hidden_truth",
        "settled",
        "world_truth",
    }
)
_FORBIDDEN_CLAIM_FEATURE_KEYS = frozenset(
    {
        "authority_ceiling",
        "authority_grant",
        "fact_id",
        "ledger_receipt",
        "mechanic_receipt",
        "settlement_id",
        "world_event_id",
    }
)


class SemanticFabricError(RuntimeError):
    """A compiled translation-memory artifact violates the semantic-fabric contract."""


def semantic_entry_meaning_fingerprint(
    lex_id: str,
    entry: Mapping[str, Any],
) -> str:
    """Hash one non-capability Lex meaning without wording or operational metadata."""
    if lex_id not in PACK_LEX_IDS or not isinstance(entry, Mapping):
        raise SemanticFabricError("semantic entry meaning fingerprint input is invalid")
    required = (
        "concept_id",
        "kind",
        "required_roles",
        "optional_roles",
        "completion",
        "features",
    )
    if any(key not in entry for key in required):
        raise SemanticFabricError("semantic entry meaning fingerprint input is incomplete")
    payload = {
        "lex_id": lex_id,
        "concept_id": entry["concept_id"],
        "kind": entry["kind"],
        "required_roles": entry["required_roles"],
        "optional_roles": entry["optional_roles"],
        "completion": entry["completion"],
        "features": entry["features"],
    }
    try:
        return content_fingerprint(payload)
    except GlossaryError as exc:
        raise SemanticFabricError("semantic entry meaning fingerprint input is invalid") from exc


def _forbidden_action_feature_paths(value: object, prefix: str = "features") -> tuple[str, ...]:
    """Return authority/mechanic feature paths that recognition data may never self-assert."""
    out: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}"
            if (
                key in _FORBIDDEN_ACTION_FEATURE_KEYS
                or key.startswith(("grants_", "settles_", "bypasses_"))
                or key.endswith("_receipt_id")
            ):
                out.append(path)
            out.extend(_forbidden_action_feature_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            out.extend(_forbidden_action_feature_paths(nested, f"{prefix}[{index}]"))
    return tuple(out)


def _validate_action_features(
    identity: str,
    features: Mapping[str, Any],
    *,
    construction: bool,
) -> None:
    """Close code-consumed ActionLex topology without closing recognition surfaces."""
    forbidden = _forbidden_action_feature_paths(features)
    if forbidden:
        raise SemanticFabricError(
            f"{identity} has forbidden ActionLex authority/mechanic feature keys: {list(forbidden)}"
        )

    action_class = features.get("action_class")
    if construction:
        if action_class is not None:
            raise SemanticFabricError(
                f"{identity} construction cannot declare a consumed ActionLex action_class"
            )
        return

    expected = _ACTION_CLASS_CONTRACTS.get(identity)
    if expected is None:
        if action_class is not None:
            raise SemanticFabricError(f"{identity} has no code-owned ActionLex action_class contract")
        return
    if action_class != expected:
        raise SemanticFabricError(
            f"{identity} changed its consumed ActionLex action_class contract: "
            f"expected {expected!r}, got {action_class!r}"
        )


def _forbidden_claim_feature_paths(value: object, prefix: str = "features") -> tuple[str, ...]:
    """Return ClaimLex feature paths that claim authority instead of recognizing language."""
    out: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}"
            if key in _FORBIDDEN_CLAIM_FEATURE_KEYS:
                out.append(path)
            elif key in _CLAIM_GUARD_ONLY_FEATURE_KEYS and nested is not False:
                out.append(path)
            elif key.startswith(("admits_", "authorizes_", "establishes_", "settles_")) and nested is not False:
                out.append(path)
            elif key.endswith(("_receipt_id", "_settlement_id", "_world_event_id")):
                out.append(path)
            out.extend(_forbidden_claim_feature_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            out.extend(_forbidden_claim_feature_paths(nested, f"{prefix}[{index}]"))
    return tuple(out)


def _validate_claim_features(identity: str, features: Mapping[str, Any]) -> None:
    forbidden = _forbidden_claim_feature_paths(features)
    if forbidden:
        raise SemanticFabricError(
            f"{identity} has forbidden ClaimLex truth/authority feature values: {list(forbidden)}"
        )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json_bytes(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticFabricError(f"invalid semantic-fabric JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise SemanticFabricError(f"semantic-fabric artifact must be an object: {path.name}")
    return value


def _resolve_artifact(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise SemanticFabricError("semantic-fabric artifact path must be a forward-slash path")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SemanticFabricError(f"semantic-fabric artifact escapes its root: {relative!r}") from exc
    if not path.is_file():
        raise SemanticFabricError(f"missing semantic-fabric artifact: {relative}")
    return path


def _string_list(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        requirement = "a non-empty list" if not allow_empty else "a list"
        raise SemanticFabricError(f"{label} must be {requirement}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SemanticFabricError(f"{label} entries must be non-empty strings")
        text = item.strip()
        if text not in out:
            out.append(text)
    if len(out) != len(value):
        raise SemanticFabricError(f"{label} contains duplicate entries")
    return tuple(out)


def _normalized_term(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticFabricError(f"{label} must not be empty")
    normalized = normalize_phrase(value).replace("’", "'")
    if not normalized:
        raise SemanticFabricError(f"{label} does not contain a matchable term")
    return normalized


def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile one conservative surface form while retaining exact source offsets.

    Spaces, hyphens, and underscores are equivalent inside authored compounds.  Apostrophe variants
    are equivalent.  Word boundaries prevent a compact memory such as ``rim`` from matching inside
    an unrelated word such as ``grimoire``.
    """
    chunks = re.split(r"[\s_-]+", term)
    body = r"(?:[\s_-]+)".join(re.escape(chunk).replace("\\'", "['’]") for chunk in chunks)
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", re.IGNORECASE)


_FALSE_FRIEND_DETERMINERS = (
    "a",
    "an",
    "another",
    "any",
    "each",
    "every",
    "her",
    "his",
    "its",
    "my",
    "no",
    "our",
    "some",
    "that",
    "the",
    "their",
    "these",
    "this",
    "those",
    "your",
)


def _false_friend_pattern(concept_id: str, false_friend: str) -> re.Pattern[str]:
    """Compile an authored exclusion, including bounded physical-action nominal variants.

    Weapon false friends are productive English constructions: ``cut a deal`` also means
    ``cut another difficult deal``. The ActionLex source still owns the verb and nominal head;
    code only permits a determiner and up to three bounded modifiers between them. Other Lex
    exclusions retain their exact existing behavior.
    """
    normalized = _normalized_term(false_friend, f"{concept_id}.false_friends")
    words = normalized.split()
    if concept_id != "action.weapon_attack" or len(words) != 3 or words[1] not in {"a", "an", "the"}:
        return _term_pattern(normalized)
    verb, _determiner, head = (re.escape(word) for word in words)
    separator = r"(?:[\s_-]+)"
    determiner = "(?:" + "|".join(map(re.escape, _FALSE_FRIEND_DETERMINERS)) + r"|[a-z0-9][a-z0-9'-]*['’]s)"
    modifier = rf"(?!(?:{head})(?![\w]))[a-z0-9]+(?:['’-][a-z0-9]+)*"
    body = verb + separator + rf"(?:{determiner}{separator})?" + rf"(?:{modifier}{separator}){{0,3}}" + head
    return re.compile(r"(?<![\w])" + body + r"(?![\w])", re.IGNORECASE)


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """Compile a construction marker, including suffixes and punctuation delimiters."""
    if marker in {"'s", "’s"}:
        return re.compile(r"(?<=[a-z0-9])['’]s\b", re.IGNORECASE)
    if marker in {'"', "“", "”"}:
        return re.compile(re.escape(marker))
    return _term_pattern(_normalized_term(marker, "construction marker"))


@dataclass(frozen=True)
class LexiconEntry:
    """One immutable concept and its semantic valence."""

    lex_id: str
    concept_id: str
    label: str
    kind: str
    terms: tuple[str, ...]
    genres: tuple[str, ...]
    genre_terms: tuple[Mapping[str, Any], ...]
    required_roles: tuple[str, ...]
    optional_roles: tuple[str, ...]
    completion: str
    features: Mapping[str, Any]
    false_friends: tuple[str, ...]
    source_ids: tuple[str, ...]
    meaning_fingerprint: str
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "lex_id": self.lex_id,
            "concept_id": self.concept_id,
            "label": self.label,
            "kind": self.kind,
            "terms": list(self.terms),
            "genres": list(self.genres),
            "genre_terms": [json.loads(json.dumps(row, ensure_ascii=False)) for row in self.genre_terms],
            "required_roles": list(self.required_roles),
            "optional_roles": list(self.optional_roles),
            "completion": self.completion,
            "features": json.loads(json.dumps(self.features, ensure_ascii=False)),
            "false_friends": list(self.false_friends),
            "source_ids": list(self.source_ids),
            "meaning_fingerprint": self.meaning_fingerprint,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class LexiconConstruction:
    """One productive form whose arguments are resolved against runtime context."""

    lex_id: str
    construction_id: str
    kind: str
    markers: tuple[str, ...]
    required_roles: tuple[str, ...]
    optional_roles: tuple[str, ...]
    completion: str
    features: Mapping[str, Any]
    source_ids: tuple[str, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "lex_id": self.lex_id,
            "construction_id": self.construction_id,
            "kind": self.kind,
            "markers": list(self.markers),
            "required_roles": list(self.required_roles),
            "optional_roles": list(self.optional_roles),
            "completion": self.completion,
            "features": json.loads(json.dumps(self.features, ensure_ascii=False)),
            "source_ids": list(self.source_ids),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class SemanticLexMatch:
    """One recognized candidate.  It never carries ownership or receipt authority."""

    lex_id: str
    concept_id: str
    kind: str
    matched_phrase: str
    start: int
    end: int
    score: int
    genres: tuple[str, ...]
    required_roles: tuple[str, ...]
    optional_roles: tuple[str, ...]
    completion: str
    features: Mapping[str, Any]
    source_ids: tuple[str, ...]
    entry_fingerprint: str
    surface_baseline: str
    ambiguity: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "lex_id": self.lex_id,
            "concept_id": self.concept_id,
            "kind": self.kind,
            "matched_phrase": self.matched_phrase,
            "start": self.start,
            "end": self.end,
            "score": self.score,
            "genres": list(self.genres),
            "required_roles": list(self.required_roles),
            "optional_roles": list(self.optional_roles),
            "completion": self.completion,
            "features": json.loads(json.dumps(self.features, ensure_ascii=False)),
            "source_ids": list(self.source_ids),
            "entry_fingerprint": self.entry_fingerprint,
            "surface_baseline": self.surface_baseline,
            "ambiguity": list(self.ambiguity),
            "recognized": True,
            "authorized": False,
            "executable": False,
            "requires_context_binding": True,
        }


@dataclass(frozen=True)
class ClaimDialogueAttribution:
    """Exact spans for one bounded named-speaker quotation construction.

    This is recognition evidence only.  It does not establish that the quoted
    proposition is true, that the speaker was honest, or that anything occurred.
    """

    speaker: str
    speaker_start: int
    speaker_end: int
    governor_start: int
    governor_end: int
    quotation_start: int
    quotation_end: int
    proposition_start: int
    proposition_end: int
    orientation: str


_DIALOGUE_NAME_TOKEN = r"[A-Z][\w'\u2019-]*"
_DIALOGUE_NAME = rf"{_DIALOGUE_NAME_TOKEN}(?:\s+{_DIALOGUE_NAME_TOKEN}){{0,2}}"
_CLAIM_DIALOGUE_PATTERNS = (
    (
        "direct",
        re.compile(
            rf"(?<![\w'\u2019-])(?P<speaker>{_DIALOGUE_NAME})\s+"
            r"(?P<governor>says|said)\b\s*[,;:]\s*"
            r'(?P<quotation>"(?P<proposition>[^"\r\n]+)")'
        ),
    ),
    (
        "direct",
        re.compile(
            rf"(?<![\w'\u2019-])(?P<speaker>{_DIALOGUE_NAME})\s+"
            r"(?P<governor>says|said)\b\s*[,;:]\s*"
            r"(?P<quotation>\u201c(?P<proposition>[^\u201d\r\n]+)\u201d)"
        ),
    ),
    (
        "inverted",
        re.compile(
            r'(?P<quotation>"(?P<proposition>[^"\r\n]+)")\s*,?\s*'
            rf"(?P<speaker>{_DIALOGUE_NAME})\s+(?P<governor>says|said)\b"
        ),
    ),
    (
        "inverted",
        re.compile(
            r"(?P<quotation>\u201c(?P<proposition>[^\u201d\r\n]+)\u201d)\s*,?\s*"
            rf"(?P<speaker>{_DIALOGUE_NAME})\s+(?P<governor>says|said)\b"
        ),
    ),
)


def claim_dialogue_attributions(source: object) -> tuple[ClaimDialogueAttribution, ...]:
    """Recognize direct and inverted one-line dialogue with an exact named speaker."""

    text = str(source or "")
    rows: dict[tuple[int, ...], ClaimDialogueAttribution] = {}
    for orientation, pattern in _CLAIM_DIALOGUE_PATTERNS:
        for hit in pattern.finditer(text):
            row = ClaimDialogueAttribution(
                speaker=hit.group("speaker"),
                speaker_start=hit.start("speaker"),
                speaker_end=hit.end("speaker"),
                governor_start=hit.start("governor"),
                governor_end=hit.end("governor"),
                quotation_start=hit.start("quotation"),
                quotation_end=hit.end("quotation"),
                proposition_start=hit.start("proposition"),
                proposition_end=hit.end("proposition"),
                orientation=orientation,
            )
            key = (
                row.speaker_start,
                row.speaker_end,
                row.governor_start,
                row.governor_end,
                row.quotation_start,
                row.quotation_end,
                row.proposition_start,
                row.proposition_end,
            )
            rows[key] = row
    return tuple(
        rows[key]
        for key in sorted(rows, key=lambda item: (item[2], item[0], item[4]))
    )


@dataclass(frozen=True)
class CompiledMeaning:
    """Content-addressed, cross-Lex recognition result for one source message."""

    source_fingerprint: str
    fabric_fingerprint: str
    genre_ids: tuple[str, ...]
    matches: tuple[SemanticLexMatch, ...]
    unresolved: tuple[str, ...] = ()
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        payload = self._payload()
        object.__setattr__(self, "fingerprint", content_fingerprint(payload))

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": MEANING_SCHEMA,
            "source_fingerprint": self.source_fingerprint,
            "fabric_fingerprint": self.fabric_fingerprint,
            "genre_ids": list(self.genre_ids),
            "matches": [match.as_dict() for match in self.matches],
            "unresolved": list(self.unresolved),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    def for_lex(self, lex_id: str) -> tuple[SemanticLexMatch, ...]:
        return tuple(match for match in self.matches if match.lex_id == lex_id)

    def concepts(self, lex_id: Optional[str] = None) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                match.concept_id for match in self.matches if lex_id is None or match.lex_id == lex_id
            )
        )

    def receipt_dict(self) -> dict[str, Any]:
        """Privacy-safe replay receipt: exact recognition metadata without source phrases."""
        matches = []
        for match in self.matches:
            row = match.as_dict()
            row.pop("matched_phrase")
            matches.append(row)
        payload = {
            "schema": MEANING_RECEIPT_SCHEMA,
            "source_fingerprint": self.source_fingerprint,
            "fabric_fingerprint": self.fabric_fingerprint,
            "genre_ids": list(self.genre_ids),
            "matches": matches,
            "unresolved": list(self.unresolved),
        }
        return {**payload, "fingerprint": content_fingerprint(payload)}


class SemanticFabric:
    """Validated index over the compact translation-memory family."""

    def __init__(
        self,
        root: Path | str,
        *,
        capability_glossary: CapabilityGlossary | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        if capability_glossary is None:
            try:
                capability_glossary = CapabilityGlossary.load(self.root.parent / "capability-glossary")
            except (GlossaryError, OSError) as exc:
                raise SemanticFabricError("semantic fabric requires its sealed CapabilityLex corpus") from exc
        manifest_path = self.root / "manifest.json"
        if not manifest_path.is_file():
            raise SemanticFabricError("semantic-fabric manifest is missing")
        manifest = _read_json_bytes(manifest_path.read_bytes(), manifest_path)
        if manifest.get("schema") != FABRIC_MANIFEST_SCHEMA:
            raise SemanticFabricError("unsupported semantic-fabric manifest schema")
        if set(manifest) != {
            "schema",
            "family_version",
            "lex_ids",
            "artifacts",
            "capability_lex",
            "genre_ids",
            "authority_boundary",
            "fingerprint",
        }:
            raise SemanticFabricError("semantic-fabric manifest fields do not match v1")
        family_version = manifest.get("family_version")
        if family_version != 3:
            raise SemanticFabricError("semantic-fabric family_version must be 3")
        if tuple(manifest.get("lex_ids") or ()) != LEX_IDS:
            raise SemanticFabricError("semantic-fabric lex_ids must use canonical family order")
        if manifest.get("authority_boundary") != "recognition_only":
            raise SemanticFabricError("semantic-fabric may only claim recognition authority")
        self.genre_ids = _string_list(manifest.get("genre_ids"), "manifest genre_ids", allow_empty=False)
        if tuple(sorted(self.genre_ids)) != self.genre_ids:
            raise SemanticFabricError("manifest genre_ids must be sorted")

        manifest_payload = {key: manifest[key] for key in manifest if key != "fingerprint"}
        if manifest.get("fingerprint") != content_fingerprint(manifest_payload):
            raise SemanticFabricError("semantic-fabric manifest fingerprint mismatch")
        self.fingerprint = str(manifest["fingerprint"])
        self.family_version = family_version

        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list) or len(artifacts) != 5:
            raise SemanticFabricError("semantic-fabric family v3 requires sources plus four Lex packs")
        verified: dict[str, tuple[dict[str, Any], bytes]] = {}
        seen_paths: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "bytes", "sha256"}:
                raise SemanticFabricError("semantic-fabric artifact descriptor is malformed")
            relative = artifact.get("path")
            if relative in seen_paths:
                raise SemanticFabricError("semantic-fabric artifact path is duplicated")
            seen_paths.add(str(relative))
            path = _resolve_artifact(self.root, relative)
            raw = path.read_bytes()
            expected_bytes = artifact.get("bytes")
            expected_hash = artifact.get("sha256")
            if (
                isinstance(expected_bytes, bool)
                or not isinstance(expected_bytes, int)
                or expected_bytes < 1
                or len(raw) != expected_bytes
            ):
                raise SemanticFabricError(f"semantic-fabric byte length mismatch: {relative}")
            if (
                not isinstance(expected_hash, str)
                or not _SHA256_RE.fullmatch(expected_hash)
                or hashlib.sha256(raw).hexdigest() != expected_hash
            ):
                raise SemanticFabricError(f"semantic-fabric hash mismatch: {relative}")
            verified[str(relative)] = (_read_json_bytes(raw, path), raw)

        sources_matches = [
            value[0] for key, value in verified.items() if value[0].get("schema") == SOURCES_SCHEMA
        ]
        if len(sources_matches) != 1:
            raise SemanticFabricError("semantic-fabric requires exactly one sources artifact")
        sources_doc = sources_matches[0]
        if set(sources_doc) != {"schema", "sources"} or not isinstance(sources_doc["sources"], list):
            raise SemanticFabricError("semantic-fabric sources artifact is malformed")
        self.sources: dict[str, dict[str, Any]] = {}
        for source in sources_doc["sources"]:
            if not isinstance(source, dict) or set(source) != {"id", "label", "kind", "url", "note"}:
                raise SemanticFabricError("semantic-fabric source row is malformed")
            source_id = source.get("id")
            if not isinstance(source_id, str) or not _ID_RE.fullmatch(source_id):
                raise SemanticFabricError("semantic-fabric source id is invalid")
            if source_id in self.sources:
                raise SemanticFabricError(f"duplicate semantic-fabric source id: {source_id}")
            if not all(
                isinstance(source.get(key), str) and source[key].strip()
                for key in ("label", "kind", "url", "note")
            ):
                raise SemanticFabricError(f"semantic-fabric source is incomplete: {source_id}")
            if not str(source["url"]).startswith(("https://", "local:")):
                raise SemanticFabricError(f"semantic-fabric source URL is unsupported: {source_id}")
            self.sources[source_id] = dict(source)

        self.entries: dict[str, LexiconEntry] = {}
        self._entries_by_lex: dict[str, list[LexiconEntry]] = {lex_id: [] for lex_id in PACK_LEX_IDS}
        self._constructions_by_lex: dict[str, list[LexiconConstruction]] = {
            lex_id: [] for lex_id in PACK_LEX_IDS
        }
        self._patterns: dict[
            str,
            list[
                tuple[
                    LexiconEntry,
                    str,
                    re.Pattern[str],
                    tuple[str, ...],
                    tuple[str, ...],
                    str,
                ]
            ],
        ] = {lex_id: [] for lex_id in PACK_LEX_IDS}
        self._ambiguities: dict[tuple[str, str], tuple[str, ...]] = {}
        for pack_doc, _raw in verified.values():
            schema = pack_doc.get("schema")
            if schema == SOURCES_SCHEMA:
                continue
            if schema == LEGACY_TRANSLATION_MEMORY_SCHEMA:
                raise SemanticFabricError(
                    "translation-memory schema v1 is unsupported; meaning fingerprints require v2"
                )
            if schema != TRANSLATION_MEMORY_SCHEMA:
                raise SemanticFabricError(f"unsupported translation-memory schema: {schema!r}")
            self._load_pack(pack_doc)
        if set(self._entries_by_lex) != set(PACK_LEX_IDS) or any(
            not rows for rows in self._entries_by_lex.values()
        ):
            raise SemanticFabricError("semantic-fabric is missing a non-empty Lex pack")

        capability = manifest.get("capability_lex")
        if (
            not isinstance(capability, dict)
            or set(capability)
            != {
                "schema",
                "manifest_fingerprint",
                "mode",
            }
            or capability.get("schema") != GLOSSARY_SCHEMA
            or capability.get("mode") != "external_adapter"
            or not isinstance(capability.get("manifest_fingerprint"), str)
        ):
            raise SemanticFabricError("semantic-fabric CapabilityLex binding is malformed")
        self.capability_manifest_fingerprint = str(capability["manifest_fingerprint"])
        self.capability_glossary = capability_glossary
        try:
            manifest_bytes = (capability_glossary.root / "manifest.json").read_bytes()
        except OSError as exc:
            raise SemanticFabricError(
                "CapabilityLex manifest is unavailable for fabric verification"
            ) from exc
        actual = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        if actual != self.capability_manifest_fingerprint:
            raise SemanticFabricError("CapabilityLex manifest fingerprint does not match fabric")

    @classmethod
    def load(
        cls,
        root: Path | str,
        *,
        capability_glossary: CapabilityGlossary | None = None,
    ) -> "SemanticFabric":
        return cls(root, capability_glossary=capability_glossary)

    def _load_pack(self, pack: dict[str, Any]) -> None:
        if set(pack) != {
            "schema",
            "lex_id",
            "version",
            "description",
            "authority",
            "entries",
            "constructions",
            "ambiguities",
            "fingerprint",
        }:
            raise SemanticFabricError("translation-memory pack fields do not match v2")
        lex_id = pack.get("lex_id")
        if lex_id not in PACK_LEX_IDS:
            raise SemanticFabricError(f"unsupported translation-memory Lex id: {lex_id!r}")
        if self._entries_by_lex[lex_id]:
            raise SemanticFabricError(f"duplicate translation-memory pack: {lex_id}")
        if pack.get("authority") != "recognition_only":
            raise SemanticFabricError(f"{lex_id} may only claim recognition authority")
        version = pack.get("version")
        if version != 2:
            raise SemanticFabricError(f"{lex_id} translation-memory version must be 2")
        if not isinstance(pack.get("description"), str) or not pack["description"].strip():
            raise SemanticFabricError(f"{lex_id} description must not be empty")
        payload = {key: pack[key] for key in pack if key != "fingerprint"}
        if pack.get("fingerprint") != content_fingerprint(payload):
            raise SemanticFabricError(f"{lex_id} pack fingerprint mismatch")
        entries = pack.get("entries")
        if not isinstance(entries, list) or not entries:
            raise SemanticFabricError(f"{lex_id} entries must be a non-empty list")
        term_targets: dict[str, dict[str, list[tuple[str, ...]]]] = {}
        for raw in entries:
            entry = self._validate_entry(lex_id, raw)
            if entry.concept_id in self.entries:
                raise SemanticFabricError(f"duplicate semantic concept id: {entry.concept_id}")
            self.entries[entry.concept_id] = entry
            self._entries_by_lex[lex_id].append(entry)
            for term in entry.terms:
                normalized = _normalized_term(term, f"{entry.concept_id}.terms")
                term_targets.setdefault(normalized, {}).setdefault(entry.concept_id, []).append(entry.genres)
                self._patterns[lex_id].append(
                    (
                        entry,
                        term,
                        _term_pattern(normalized),
                        entry.genres,
                        entry.source_ids,
                        "core",
                    )
                )
            for surface in entry.genre_terms:
                term = str(surface["term"])
                normalized = _normalized_term(term, f"{entry.concept_id}.genre_terms")
                surface_genres = tuple(str(item) for item in surface["genres"])
                surface_sources = tuple(str(item) for item in surface["source_ids"])
                term_targets.setdefault(normalized, {}).setdefault(entry.concept_id, []).append(
                    surface_genres
                )
                self._patterns[lex_id].append(
                    (
                        entry,
                        term,
                        _term_pattern(normalized),
                        surface_genres,
                        surface_sources,
                        str(surface["baseline"]),
                    )
                )

        constructions = pack.get("constructions")
        if not isinstance(constructions, list) or not constructions:
            raise SemanticFabricError(f"{lex_id} constructions must be a non-empty list")
        construction_ids: set[str] = set()
        for raw in constructions:
            construction = self._validate_construction(lex_id, raw)
            if construction.construction_id in construction_ids:
                raise SemanticFabricError(f"duplicate {lex_id} construction: {construction.construction_id}")
            construction_ids.add(construction.construction_id)
            self._constructions_by_lex[lex_id].append(construction)

        ambiguities = pack.get("ambiguities")
        if not isinstance(ambiguities, list):
            raise SemanticFabricError(f"{lex_id} ambiguities must be a list")
        declared: dict[str, tuple[str, ...]] = {}
        for ambiguity in ambiguities:
            if (
                not isinstance(ambiguity, dict)
                or set(ambiguity)
                != {
                    "term",
                    "concept_ids",
                    "resolution",
                }
                or ambiguity.get("resolution") != "context_required"
            ):
                raise SemanticFabricError(f"{lex_id} ambiguity row is malformed")
            term = _normalized_term(ambiguity.get("term"), f"{lex_id}.ambiguity.term")
            concept_ids = _string_list(
                ambiguity.get("concept_ids"),
                f"{lex_id}.{term}.concept_ids",
                allow_empty=False,
            )
            if len(concept_ids) < 2:
                raise SemanticFabricError(f"{lex_id} ambiguity needs at least two concepts: {term}")
            if set(concept_ids) != set(term_targets.get(term, {})):
                raise SemanticFabricError(f"{lex_id} ambiguity does not match term collision: {term}")
            declared[term] = tuple(sorted(concept_ids))
            self._ambiguities[(lex_id, term)] = tuple(sorted(concept_ids))
        collisions: dict[str, set[str]] = {}
        for term, targets in term_targets.items():
            concept_ids = sorted(targets)
            overlapping: set[str] = set()
            for index, left in enumerate(concept_ids):
                for right in concept_ids[index + 1 :]:
                    if any(
                        "*" in left_scope or "*" in right_scope or bool(set(left_scope) & set(right_scope))
                        for left_scope in targets[left]
                        for right_scope in targets[right]
                    ):
                        overlapping.update((left, right))
            if overlapping:
                collisions[term] = overlapping
        if set(collisions) != set(declared):
            missing = sorted(set(collisions) ^ set(declared))
            raise SemanticFabricError(f"{lex_id} undeclared or stale phrase collisions: {missing}")
        self._entries_by_lex[lex_id].sort(key=lambda item: item.concept_id)
        self._constructions_by_lex[lex_id].sort(key=lambda item: item.construction_id)
        self._patterns[lex_id].sort(
            key=lambda item: (
                -len(normalize_phrase(item[1]).split()),
                item[1],
                item[0].concept_id,
            )
        )

    def _validate_entry(self, lex_id: str, raw: object) -> LexiconEntry:
        fields = {
            "concept_id",
            "label",
            "kind",
            "terms",
            "genres",
            "required_roles",
            "optional_roles",
            "completion",
            "features",
            "false_friends",
            "source_ids",
            "genre_terms",
            "meaning_fingerprint",
            "fingerprint",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise SemanticFabricError(f"{lex_id} entry fields do not match v2")
        concept_id = raw.get("concept_id")
        if (
            not isinstance(concept_id, str)
            or not _ID_RE.fullmatch(concept_id)
            or not concept_id.startswith(lex_id + ".")
        ):
            raise SemanticFabricError(f"invalid {lex_id} concept id: {concept_id!r}")
        for key in ("label", "kind", "completion"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise SemanticFabricError(f"{concept_id}.{key} must not be empty")
        if not _ID_RE.fullmatch(str(raw["kind"])) or not _ID_RE.fullmatch(str(raw["completion"])):
            raise SemanticFabricError(f"{concept_id} kind/completion must be stable identifiers")
        terms = _string_list(raw.get("terms"), f"{concept_id}.terms", allow_empty=False)
        normalized_terms = [_normalized_term(term, f"{concept_id}.terms") for term in terms]
        if len(set(normalized_terms)) != len(normalized_terms):
            raise SemanticFabricError(f"{concept_id}.terms collide after normalization")
        genres = _string_list(raw.get("genres"), f"{concept_id}.genres", allow_empty=False)
        if "*" in genres and genres != ("*",):
            raise SemanticFabricError(f"{concept_id}.genres cannot mix universal and explicit IDs")
        unknown_genres = set(genres) - set(self.genre_ids) - {"*"}
        if unknown_genres:
            raise SemanticFabricError(f"{concept_id} has unknown genre IDs: {sorted(unknown_genres)}")
        genre_terms_raw = raw.get("genre_terms")
        if not isinstance(genre_terms_raw, list):
            raise SemanticFabricError(f"{concept_id}.genre_terms must be a list")
        genre_terms: list[dict[str, Any]] = []
        seen_surfaces: set[tuple[str, tuple[str, ...]]] = set()
        for surface in genre_terms_raw:
            if not isinstance(surface, dict) or set(surface) != {
                "term",
                "genres",
                "source_ids",
                "baseline",
            }:
                raise SemanticFabricError(f"{concept_id}.genre_terms row is malformed")
            term = _normalized_term(surface.get("term"), f"{concept_id}.genre_terms.term")
            surface_genres = _string_list(
                surface.get("genres"),
                f"{concept_id}.{term}.genres",
                allow_empty=False,
            )
            if "*" in surface_genres or set(surface_genres) - set(self.genre_ids):
                raise SemanticFabricError(f"{concept_id}.{term} must use known explicit genre IDs")
            surface_sources = _string_list(
                surface.get("source_ids"),
                f"{concept_id}.{term}.source_ids",
                allow_empty=False,
            )
            unknown_surface_sources = set(surface_sources) - set(self.sources)
            if unknown_surface_sources:
                raise SemanticFabricError(
                    f"{concept_id}.{term} has unknown sources: {sorted(unknown_surface_sources)}"
                )
            baseline = surface.get("baseline")
            if baseline not in {"existing_corpus", "web_research", "genre_authored"}:
                raise SemanticFabricError(f"{concept_id}.{term} baseline is unsupported")
            surface_key = (term, tuple(sorted(surface_genres)))
            if surface_key in seen_surfaces:
                raise SemanticFabricError(f"{concept_id} repeats a genre surface: {term}")
            seen_surfaces.add(surface_key)
            genre_terms.append(
                {
                    "term": term,
                    "genres": list(surface_genres),
                    "source_ids": list(surface_sources),
                    "baseline": baseline,
                }
            )
        required_roles = _string_list(raw.get("required_roles"), f"{concept_id}.required_roles")
        optional_roles = _string_list(raw.get("optional_roles"), f"{concept_id}.optional_roles")
        if any(not _ROLE_RE.fullmatch(role) for role in (*required_roles, *optional_roles)):
            raise SemanticFabricError(f"{concept_id} has an invalid semantic role")
        if set(required_roles) & set(optional_roles):
            raise SemanticFabricError(f"{concept_id} repeats a required role as optional")
        features = raw.get("features")
        if not isinstance(features, dict):
            raise SemanticFabricError(f"{concept_id}.features must be an object")
        # Reject non-JSON feature values and detach the public corpus from runtime mutation.
        try:
            frozen_features = json.loads(json.dumps(features, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError) as exc:
            raise SemanticFabricError(f"{concept_id}.features must be JSON-compatible") from exc
        if lex_id == "action":
            _validate_action_features(concept_id, frozen_features, construction=False)
        elif lex_id == "claim":
            _validate_claim_features(concept_id, frozen_features)
        false_friends = _string_list(raw.get("false_friends"), f"{concept_id}.false_friends")
        source_ids = _string_list(raw.get("source_ids"), f"{concept_id}.source_ids", allow_empty=False)
        unknown_sources = set(source_ids) - set(self.sources)
        if unknown_sources:
            raise SemanticFabricError(f"{concept_id} has unknown source IDs: {sorted(unknown_sources)}")
        meaning_fingerprint = raw.get("meaning_fingerprint")
        if (
            not isinstance(meaning_fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", meaning_fingerprint) is None
            or meaning_fingerprint != semantic_entry_meaning_fingerprint(lex_id, raw)
        ):
            raise SemanticFabricError(f"{concept_id} meaning fingerprint mismatch")
        payload = {key: raw[key] for key in raw if key != "fingerprint"}
        fingerprint = raw.get("fingerprint")
        if fingerprint != content_fingerprint(payload):
            raise SemanticFabricError(f"{concept_id} fingerprint mismatch")
        return LexiconEntry(
            lex_id=lex_id,
            concept_id=concept_id,
            label=str(raw["label"]),
            kind=str(raw["kind"]),
            terms=terms,
            genres=genres,
            genre_terms=tuple(genre_terms),
            required_roles=required_roles,
            optional_roles=optional_roles,
            completion=str(raw["completion"]),
            features=frozen_features,
            false_friends=false_friends,
            source_ids=source_ids,
            meaning_fingerprint=meaning_fingerprint,
            fingerprint=str(fingerprint),
        )

    def _validate_construction(self, lex_id: str, raw: object) -> LexiconConstruction:
        fields = {
            "construction_id",
            "kind",
            "markers",
            "required_roles",
            "optional_roles",
            "completion",
            "features",
            "source_ids",
            "fingerprint",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise SemanticFabricError(f"{lex_id} construction fields do not match v1")
        construction_id = raw.get("construction_id")
        if (
            not isinstance(construction_id, str)
            or not _ID_RE.fullmatch(construction_id)
            or not construction_id.startswith(lex_id + ".")
        ):
            raise SemanticFabricError(f"invalid {lex_id} construction id: {construction_id!r}")
        for key in ("kind", "completion"):
            if not isinstance(raw.get(key), str) or not _ID_RE.fullmatch(raw[key]):
                raise SemanticFabricError(f"{construction_id}.{key} must be a stable identifier")
        markers = _string_list(raw.get("markers"), f"{construction_id}.markers")
        required_roles = _string_list(
            raw.get("required_roles"),
            f"{construction_id}.required_roles",
            allow_empty=False,
        )
        optional_roles = _string_list(
            raw.get("optional_roles"),
            f"{construction_id}.optional_roles",
        )
        if any(not _ROLE_RE.fullmatch(role) for role in (*required_roles, *optional_roles)):
            raise SemanticFabricError(f"{construction_id} has an invalid semantic role")
        if set(required_roles) & set(optional_roles):
            raise SemanticFabricError(f"{construction_id} repeats a required role as optional")
        features = raw.get("features")
        if not isinstance(features, dict):
            raise SemanticFabricError(f"{construction_id}.features must be an object")
        try:
            frozen_features = json.loads(json.dumps(features, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError) as exc:
            raise SemanticFabricError(f"{construction_id}.features must be JSON-compatible") from exc
        if lex_id == "action":
            _validate_action_features(construction_id, frozen_features, construction=True)
        elif lex_id == "claim":
            _validate_claim_features(construction_id, frozen_features)
        source_ids = _string_list(
            raw.get("source_ids"),
            f"{construction_id}.source_ids",
            allow_empty=False,
        )
        unknown_sources = set(source_ids) - set(self.sources)
        if unknown_sources:
            raise SemanticFabricError(f"{construction_id} has unknown source IDs: {sorted(unknown_sources)}")
        payload = {key: raw[key] for key in raw if key != "fingerprint"}
        fingerprint = raw.get("fingerprint")
        if fingerprint != content_fingerprint(payload):
            raise SemanticFabricError(f"{construction_id} fingerprint mismatch")
        return LexiconConstruction(
            lex_id=lex_id,
            construction_id=construction_id,
            kind=str(raw["kind"]),
            markers=markers,
            required_roles=required_roles,
            optional_roles=optional_roles,
            completion=str(raw["completion"]),
            features=frozen_features,
            source_ids=source_ids,
            fingerprint=str(fingerprint),
        )

    def entry(self, concept_id: str) -> LexiconEntry:
        try:
            return self.entries[concept_id]
        except KeyError as exc:
            raise SemanticFabricError(f"unknown semantic-fabric concept: {concept_id}") from exc

    def entries_for(self, lex_id: str, *, kind: str | None = None) -> tuple[LexiconEntry, ...]:
        if lex_id not in PACK_LEX_IDS:
            raise SemanticFabricError(f"Lex does not expose compiled pack entries: {lex_id}")
        return tuple(entry for entry in self._entries_by_lex[lex_id] if kind is None or entry.kind == kind)

    def constructions_for(
        self,
        lex_id: str,
        *,
        kind: str | None = None,
    ) -> tuple[LexiconConstruction, ...]:
        if lex_id not in PACK_LEX_IDS:
            raise SemanticFabricError(f"Lex does not expose compiled constructions: {lex_id}")
        return tuple(
            construction
            for construction in self._constructions_by_lex[lex_id]
            if kind is None or construction.kind == kind
        )

    def terms_for(
        self,
        lex_id: str,
        *,
        kind: str | None = None,
        feature: tuple[str, object] | None = None,
        genre_ids: Iterable[str] = (),
    ) -> tuple[str, ...]:
        requested_genres = tuple(dict.fromkeys(str(item) for item in genre_ids))
        unknown_genres = set(requested_genres) - set(self.genre_ids)
        if unknown_genres:
            raise SemanticFabricError(f"unknown semantic-fabric genre IDs: {sorted(unknown_genres)}")
        entries = self.entries_for(lex_id, kind=kind)
        if feature is not None:
            key, expected = feature
            entries = tuple(entry for entry in entries if entry.features.get(key) == expected)
        terms: list[str] = []
        for entry in entries:
            if entry.genres == ("*",) or set(entry.genres) & set(requested_genres):
                terms.extend(entry.terms)
            for surface in entry.genre_terms:
                surface_genres = tuple(str(item) for item in surface["genres"])
                if requested_genres and set(surface_genres) & set(requested_genres):
                    terms.append(str(surface["term"]))
        return tuple(dict.fromkeys(terms))

    def false_friend_spans(
        self,
        concept_id: str,
        text: object,
    ) -> tuple[tuple[int, int], ...]:
        """Return exact source spans suppressed by one sealed Lex entry.

        Runtime interpreters with a conservative legacy fallback must consume the same
        false-friend boundary as the translation memory. Otherwise a phrase ActionLex correctly
        rejects (for example, ``cut a deal``) can be resurrected as a physical action by a later
        keyword scan.
        """
        entry = self.entry(concept_id)
        source = str(text or "")
        spans = {
            (match.start(), match.end())
            for false_friend in entry.false_friends
            for match in _false_friend_pattern(
                entry.concept_id,
                false_friend,
            ).finditer(source)
        }
        return tuple(sorted(spans))

    def translate(
        self,
        text: object,
        *,
        genre_ids: Iterable[str] = (),
        lex_ids: Iterable[str] = LEX_IDS,
        limit: int = 128,
    ) -> CompiledMeaning:
        source = str(text or "")
        requested_genres = tuple(dict.fromkeys(str(item) for item in genre_ids))
        unknown_genres = set(requested_genres) - set(self.genre_ids)
        if unknown_genres:
            raise SemanticFabricError(f"unknown semantic-fabric genre IDs: {sorted(unknown_genres)}")
        requested_lexes = tuple(dict.fromkeys(str(item) for item in lex_ids))
        unknown_lexes = set(requested_lexes) - set(LEX_IDS)
        if unknown_lexes:
            raise SemanticFabricError(f"unknown semantic-fabric Lex IDs: {sorted(unknown_lexes)}")
        matches: list[SemanticLexMatch] = []
        truncated_lexes: list[str] = []
        for lex_id in requested_lexes:
            if lex_id == "capability":
                rows = self._translate_capabilities(source, requested_genres)
            else:
                rows = self._translate_pack(source, lex_id, requested_genres)
                if lex_id == "claim":
                    rows.extend(self._translate_claim_dialogue(source))
            rows.sort(
                key=lambda item: (
                    item.start,
                    -(item.end - item.start),
                    -item.score,
                    item.concept_id,
                )
            )
            if limit <= 0:
                selected: list[SemanticLexMatch] = []
            elif len(rows) <= limit:
                selected = rows
            else:
                selected = []
                grouped: dict[tuple[int, int, str], list[SemanticLexMatch]] = {}
                for match in rows:
                    key = (match.start, match.end, normalize_phrase(match.matched_phrase))
                    grouped.setdefault(key, []).append(match)
                for group in grouped.values():
                    if selected and len(selected) + len(group) > limit:
                        continue
                    selected.extend(group)
                if len(selected) != len(rows):
                    truncated_lexes.append(lex_id)
            matches.extend(selected)
        matches.sort(
            key=lambda item: (
                item.start,
                -(item.end - item.start),
                -item.score,
                item.lex_id,
                item.concept_id,
            )
        )
        unresolved = tuple(
            sorted(
                {concept_id for match in matches for concept_id in match.ambiguity}
                | {f"semantic_fabric.match_budget_exceeded.{lex_id}" for lex_id in truncated_lexes}
            )
        )
        return CompiledMeaning(
            source_fingerprint=content_fingerprint(source),
            fabric_fingerprint=self.fingerprint,
            genre_ids=requested_genres,
            matches=tuple(matches),
            unresolved=unresolved,
        )

    def _translate_claim_dialogue(self, source: str) -> list[SemanticLexMatch]:
        """Project productive named dialogue through the sealed direct-assertion meaning."""

        entry = self.entry("claim.assertion.direct")
        return [
            SemanticLexMatch(
                lex_id="claim",
                concept_id=entry.concept_id,
                kind=entry.kind,
                matched_phrase=source[row.governor_start:row.governor_end],
                start=row.governor_start,
                end=row.governor_end,
                score=85,
                genres=("*",),
                required_roles=entry.required_roles,
                optional_roles=entry.optional_roles,
                completion=entry.completion,
                features=entry.features,
                source_ids=entry.source_ids,
                entry_fingerprint=entry.fingerprint,
                surface_baseline="dialogue_construction",
            )
            for row in claim_dialogue_attributions(source)
        ]

    def _translate_pack(
        self,
        source: str,
        lex_id: str,
        requested_genres: tuple[str, ...],
    ) -> list[SemanticLexMatch]:
        out: list[SemanticLexMatch] = []
        for entry, term, pattern, surface_genres, surface_sources, baseline in self._patterns[lex_id]:
            if surface_genres != ("*",) and (
                not requested_genres or not set(surface_genres) & set(requested_genres)
            ):
                continue
            false_spans = [
                (hit.start(), hit.end())
                for false_friend in entry.false_friends
                for hit in _false_friend_pattern(
                    entry.concept_id,
                    false_friend,
                ).finditer(source)
            ]
            for hit in pattern.finditer(source):
                if any(start <= hit.start() and hit.end() <= end for start, end in false_spans):
                    continue
                normalized_term = _normalized_term(term, f"{entry.concept_id}.terms")
                score = 70 + min(20, 3 * len(normalized_term.split()))
                if normalize_phrase(source) == normalized_term:
                    score += 20
                if surface_genres != ("*",) and set(surface_genres) & set(requested_genres):
                    score += 10
                out.append(
                    SemanticLexMatch(
                        lex_id=lex_id,
                        concept_id=entry.concept_id,
                        kind=entry.kind,
                        matched_phrase=source[hit.start() : hit.end()],
                        start=hit.start(),
                        end=hit.end(),
                        score=score,
                        genres=surface_genres,
                        required_roles=entry.required_roles,
                        optional_roles=entry.optional_roles,
                        completion=entry.completion,
                        features=entry.features,
                        source_ids=surface_sources,
                        entry_fingerprint=entry.fingerprint,
                        surface_baseline=baseline,
                    )
                )
        for construction in self._constructions_by_lex[lex_id]:
            for marker in construction.markers:
                for hit in _marker_pattern(marker).finditer(source):
                    out.append(
                        SemanticLexMatch(
                            lex_id=lex_id,
                            concept_id=construction.construction_id,
                            kind=construction.kind,
                            matched_phrase=source[hit.start() : hit.end()],
                            start=hit.start(),
                            end=hit.end(),
                            score=45,
                            genres=("*",),
                            required_roles=construction.required_roles,
                            optional_roles=construction.optional_roles,
                            completion=construction.completion,
                            features=construction.features,
                            source_ids=construction.source_ids,
                            entry_fingerprint=construction.fingerprint,
                            surface_baseline="construction",
                        )
                    )
        unique: dict[tuple[str, int, int, str], SemanticLexMatch] = {}
        for match in out:
            key = (
                match.concept_id,
                match.start,
                match.end,
                normalize_phrase(match.matched_phrase),
            )
            prior = unique.get(key)
            if prior is None or (match.score, match.surface_baseline) > (
                prior.score,
                prior.surface_baseline,
            ):
                unique[key] = match
        out = list(unique.values())
        groups: dict[tuple[int, int, str], set[str]] = {}
        for match in out:
            key = (match.start, match.end, normalize_phrase(match.matched_phrase))
            groups.setdefault(key, set()).add(match.concept_id)
        return [
            replace(
                match,
                ambiguity=tuple(
                    sorted(groups[(match.start, match.end, normalize_phrase(match.matched_phrase))])
                ),
            )
            if len(groups[(match.start, match.end, normalize_phrase(match.matched_phrase))]) > 1
            else match
            for match in out
        ]

    def _translate_capabilities(
        self,
        source: str,
        requested_genres: tuple[str, ...],
    ) -> list[SemanticLexMatch]:
        out: list[SemanticLexMatch] = []
        for candidate in self.capability_glossary.translate(
            source,
            requested_genres,
            limit=256,
            all_surfaces=True,
        ):
            pattern = _term_pattern(
                _normalized_term(candidate.matched_phrase, "CapabilityLex matched phrase")
            )
            concept_fingerprint, source_ids = self.capability_glossary.concept_lineage(
                candidate.concept_id,
                candidate.genre_ids,
            )
            for hit in pattern.finditer(source):
                out.append(
                    SemanticLexMatch(
                        lex_id="capability",
                        concept_id=candidate.concept_id,
                        kind=candidate.concept_type,
                        matched_phrase=source[hit.start() : hit.end()],
                        start=hit.start(),
                        end=hit.end(),
                        score=candidate.score,
                        genres=candidate.genre_ids,
                        required_roles=("actor",),
                        optional_roles=("target", "object", "locus", "instrument"),
                        completion="context_binding_required",
                        features={
                            "categories": list(candidate.categories),
                            "meaning_facets": dict(candidate.meaning_facets),
                            "meaning_fingerprint": candidate.meaning_fingerprint,
                        },
                        source_ids=source_ids,
                        entry_fingerprint=concept_fingerprint,
                        surface_baseline=candidate.baseline,
                    )
                )
        groups: dict[tuple[int, int, str], set[str]] = {}
        for match in out:
            key = (match.start, match.end, normalize_phrase(match.matched_phrase))
            groups.setdefault(key, set()).add(match.concept_id)
        return [
            replace(
                match,
                ambiguity=tuple(
                    sorted(groups[(match.start, match.end, normalize_phrase(match.matched_phrase))])
                ),
            )
            if len(groups[(match.start, match.end, normalize_phrase(match.matched_phrase))]) > 1
            else match
            for match in out
        ]


def validate_compiled_meaning(value: object) -> dict[str, Any]:
    """Strictly validate a serialized recognition result without loading its corpus."""
    fields = {
        "schema",
        "source_fingerprint",
        "fabric_fingerprint",
        "genre_ids",
        "matches",
        "unresolved",
        "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != MEANING_SCHEMA:
        raise SemanticFabricError("compiled semantic meaning fields do not match v1")
    for key in ("source_fingerprint", "fabric_fingerprint", "fingerprint"):
        raw = value.get(key)
        if not isinstance(raw, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw):
            raise SemanticFabricError(f"compiled semantic meaning {key} is invalid")
    if (
        not isinstance(value.get("genre_ids"), list)
        or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in value["genre_ids"])
        or len(set(value["genre_ids"])) != len(value["genre_ids"])
    ):
        raise SemanticFabricError("compiled semantic meaning genre_ids are invalid")
    if (
        not isinstance(value.get("unresolved"), list)
        or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in value["unresolved"])
        or value["unresolved"] != sorted(set(value["unresolved"]))
    ):
        raise SemanticFabricError("compiled semantic meaning unresolved IDs are invalid")
    matches = value.get("matches")
    if not isinstance(matches, list):
        raise SemanticFabricError("compiled semantic meaning matches must be a list")
    match_fields = {
        "lex_id",
        "concept_id",
        "kind",
        "matched_phrase",
        "start",
        "end",
        "score",
        "genres",
        "required_roles",
        "optional_roles",
        "completion",
        "features",
        "source_ids",
        "entry_fingerprint",
        "ambiguity",
        "recognized",
        "authorized",
        "executable",
        "requires_context_binding",
        "surface_baseline",
    }
    seen_matches: set[tuple[object, ...]] = set()
    ordering: list[tuple[object, ...]] = []
    ambiguity_ids: set[str] = set()
    for match in matches:
        if not isinstance(match, dict) or set(match) != match_fields:
            raise SemanticFabricError("compiled semantic match fields do not match v1")
        if (
            match.get("lex_id") not in LEX_IDS
            or not isinstance(match.get("concept_id"), str)
            or not _ID_RE.fullmatch(match["concept_id"])
            or not isinstance(match.get("kind"), str)
            or not _ROLE_RE.fullmatch(match["kind"])
        ):
            raise SemanticFabricError("compiled semantic match identity is invalid")
        if (
            match.get("recognized") is not True
            or match.get("authorized") is not False
            or match.get("executable") is not False
            or match.get("requires_context_binding") is not True
        ):
            raise SemanticFabricError("compiled semantic match overclaims authority")
        start, end = match.get("start"), match.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise SemanticFabricError("compiled semantic match span is invalid")
        phrase = match.get("matched_phrase")
        score = match.get("score")
        if (
            not isinstance(phrase, str)
            or not phrase
            or len(phrase) != end - start
            or isinstance(score, bool)
            or not isinstance(score, int)
            or score < 0
            or score > 1000
        ):
            raise SemanticFabricError("compiled semantic match phrase or score is invalid")
        for key in ("genres", "required_roles", "optional_roles", "source_ids", "ambiguity"):
            if (
                not isinstance(match.get(key), list)
                or any(not isinstance(item, str) or not item for item in match[key])
                or len(set(match[key])) != len(match[key])
            ):
                raise SemanticFabricError(f"compiled semantic match {key} is invalid")
        if any(item != "*" and not _ID_RE.fullmatch(item) for item in match["genres"]):
            raise SemanticFabricError("compiled semantic match genres are invalid")
        if any(
            not _ROLE_RE.fullmatch(item) for item in (*match["required_roles"], *match["optional_roles"])
        ) or set(match["required_roles"]) & set(match["optional_roles"]):
            raise SemanticFabricError("compiled semantic match roles are invalid")
        if any(not _ID_RE.fullmatch(item) for item in (*match["source_ids"], *match["ambiguity"])):
            raise SemanticFabricError("compiled semantic match references are invalid")
        ambiguity = match["ambiguity"]
        if ambiguity and (
            ambiguity != sorted(ambiguity) or len(ambiguity) < 2 or match["concept_id"] not in ambiguity
        ):
            raise SemanticFabricError("compiled semantic match ambiguity is invalid")
        ambiguity_ids.update(ambiguity)
        for key in ("completion", "surface_baseline"):
            if not isinstance(match.get(key), str) or not match[key].strip():
                raise SemanticFabricError(f"compiled semantic match {key} is invalid")
        entry_fingerprint = match.get("entry_fingerprint")
        if (
            not isinstance(entry_fingerprint, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", entry_fingerprint) is None
        ):
            raise SemanticFabricError("compiled semantic match entry fingerprint is invalid")
        if not isinstance(match.get("features"), dict):
            raise SemanticFabricError("compiled semantic match features are invalid")
        identity = (
            match["lex_id"],
            match["concept_id"],
            start,
            end,
            phrase,
            entry_fingerprint,
            match["surface_baseline"],
        )
        if identity in seen_matches:
            raise SemanticFabricError("compiled semantic meaning contains a duplicate match")
        seen_matches.add(identity)
        ordering.append(
            (
                start,
                -(end - start),
                -score,
                match["lex_id"],
                match["concept_id"],
            )
        )
    if ordering != sorted(ordering):
        raise SemanticFabricError("compiled semantic matches are not in canonical order")
    allowed_unresolved = ambiguity_ids | {
        item
        for item in value["unresolved"]
        if item.startswith("semantic_fabric.match_budget_exceeded.") and item.rsplit(".", 1)[-1] in LEX_IDS
    }
    if set(value["unresolved"]) != allowed_unresolved:
        raise SemanticFabricError("compiled semantic unresolved IDs do not match its evidence")
    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value["fingerprint"] != content_fingerprint(payload):
        raise SemanticFabricError("compiled semantic meaning fingerprint mismatch")
    return json.loads(json.dumps(value, ensure_ascii=False))


def validate_compiled_meaning_receipt(value: object) -> dict[str, Any]:
    """Validate the content-free journal form of a compiled meaning."""
    fields = {
        "schema",
        "source_fingerprint",
        "fabric_fingerprint",
        "genre_ids",
        "matches",
        "unresolved",
        "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != MEANING_RECEIPT_SCHEMA:
        raise SemanticFabricError("compiled semantic receipt fields do not match v1")
    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value.get("fingerprint") != content_fingerprint(payload):
        raise SemanticFabricError("compiled semantic receipt fingerprint mismatch")
    rows = value.get("matches")
    if not isinstance(rows, list):
        raise SemanticFabricError("compiled semantic receipt matches must be a list")
    synthetic_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or "matched_phrase" in row:
            raise SemanticFabricError("compiled semantic receipt match is malformed")
        start, end = row.get("start"), row.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or end <= start
        ):
            raise SemanticFabricError("compiled semantic receipt match span is invalid")
        synthetic_rows.append({**row, "matched_phrase": "x" * (end - start)})
    synthetic = {
        **payload,
        "schema": MEANING_SCHEMA,
        "matches": synthetic_rows,
    }
    synthetic["fingerprint"] = content_fingerprint(synthetic)
    validate_compiled_meaning(synthetic)
    return json.loads(json.dumps(value, ensure_ascii=False))


@lru_cache(maxsize=4)
def _cached_default(root: str) -> SemanticFabric:
    fabric_root = Path(root)
    capability_root = fabric_root.parent / "capability-glossary"
    try:
        capability_glossary = CapabilityGlossary.load(capability_root)
    except (GlossaryError, OSError) as exc:
        raise SemanticFabricError("semantic fabric requires its sealed CapabilityLex corpus") from exc
    return SemanticFabric.load(fabric_root, capability_glossary=capability_glossary)


def load_default_semantic_fabric(project_root: Path | str | None = None) -> SemanticFabric:
    """Load and cache the repo-local or installed compact semantic-fabric corpus."""
    if project_root is not None:
        root = Path(project_root)
        if (root / "manifest.json").is_file():
            corpus = root
        else:
            corpus = root / "corpus" / "semantic-fabric"
        return _cached_default(str(corpus.resolve()))

    package_corpus = Path(__file__).resolve().parent / "corpus" / "semantic-fabric"
    if package_corpus.is_dir():
        return _cached_default(str(package_corpus.resolve()))
    repo_corpus = Path(__file__).resolve().parents[2] / "corpus" / "semantic-fabric"
    return _cached_default(str(repo_corpus.resolve()))
