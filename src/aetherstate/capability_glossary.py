"""Deterministic cross-genre capability translation and definition freezing.

The glossary is recognition evidence, never an authority grant.  It translates broad or
genre-specific language into stable concept ids; actor/world ownership and receipt admission remain
separate gates.  Corpus I/O belongs on cold paths (Creator, import, authoring, or tests), not on the
token-stream relay path.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlparse


GLOSSARY_SCHEMA = "aetherstate-capability-glossary/2"
TAXONOMY_SCHEMA = "aetherstate-semantic-atlas-taxonomy/1"
DEFINITION_SCHEMA = "capability-definition/1"
COMPILER_VERSION = "capability-compiler/1"

CAPABILITY_KINDS = frozenset(
    {
        "skill",
        "ability",
        "spell",
        "augment",
        "cyberware",
        "enemy_move",
    }
)

PREVIEW_CLASSES = frozenset(
    {
        "supported_and_frozen",
        "narration_boundary",
        "lore_only",
        "rejected_grounding",
    }
)

# Compiler v1 has no storage/assignment/runtime adapter.  Existing checks, ability dice shaping,
# and opposition HP remain real code paths, but a glossary definition cannot invoke them merely by
# naming them.  Future adapters must add an explicit versioned admission here and prove their full
# reducer envelope end to end.
RECEIPT_KIND_ADMISSION = MappingProxyType({})

_ENEMY_HP_REQUIRED_FIELDS = (
    "basis",
    "delivery",
    "semantic_primitive",
    "functional_family",
    "effect_channel",
    "target",
    "range",
    "area",
    "timing",
    "cadence",
)
_ENEMY_HP_TIMINGS = frozenset(
    {
        "broad wind-up",
        "charged wind-up",
        "committed",
        "committed sweep",
        "direct release",
        "fast",
        "measured",
        "precise casting",
        "slow aim",
        "slow wind-up",
        "spoken or gestured release",
        "steady aim",
        "sustained",
        "sweeping release",
        "tracking",
    }
)
_ENEMY_HP_CADENCES = frozenset({"ammo", "casting", "fuel", "recovery", "reliable", "reload", "setup"})
_SUPPORT_RECOGNITION = frozenset({"canonical"})
_SUPPORT_AUTHORIZATION = frozenset({"frozen_definition_required"})
_SUPPORT_NARRATION = frozenset({"meaning", "boundary"})

_REGISTRY_SOURCE_IDS = frozenset({"existing.registry_snapshot", "existing.registry_runtime"})
_LOCAL_PROVENANCE_IDS = frozenset(
    {
        "existing.enemy_capability_families",
        "existing.enemy_runtime_lexicon",
        "existing.registry_snapshot",
        "existing.registry_runtime",
        "aetherstate.cross_genre_normalization",
    }
)
_LOCAL_SOURCE_KINDS = MappingProxyType(
    {
        "existing.enemy_capability_families": "local_corpus",
        "existing.enemy_runtime_lexicon": "local_corpus",
        "existing.registry_snapshot": "local_corpus",
        "existing.registry_runtime": "local_code",
        "aetherstate.cross_genre_normalization": "project_research",
    }
)
_EXPECTED_CATEGORY_IDS = frozenset(
    {
        "offense",
        "defense_reaction",
        "buff_support",
        "status_condition",
        "movement_travel",
        "control_position_terrain",
        "summon_deploy_transform",
        "resource_cost",
        "equipment_technology_magic",
        "social_investigation",
        "crafting_survival_logistics",
        "world_scale_authority",
    }
)
_EXPECTED_CONCEPT_KIND_IDS = frozenset(
    {
        "ability",
        "ability_mechanic",
        "action",
        "basis",
        "condition",
        "functional_family",
        "identity",
        "relationship_state",
        "resource",
        "semantic_primitive",
        "skill",
        "status",
        "world_state",
    }
)
_EXPECTED_CLASSIFICATION_PLANE_IDS = frozenset(
    {"concept_facets", "scale_profile", "cube_coverage", "runtime_record"}
)
_EXPECTED_SCALE_AXIS_IDS = frozenset(
    {
        "power",
        "severity",
        "target_count",
        "area",
        "range",
        "duration",
        "world_scope",
        "propagation",
        "reversibility",
    }
)
_EXPECTED_AUTHORITY_STAGE_IDS = frozenset(
    {"recognized", "defined", "assigned", "eligible", "admitted", "settled", "replayed"}
)
_EXPECTED_CUBE_FACE_IDS = frozenset(
    {
        "recognition",
        "binding",
        "world_alignment",
        "admission",
        "complete_settlement",
        "narrator_transfer",
        "hud_visibility",
    }
)
_EXPECTED_COVERAGE_STATUSES = frozenset({"working", "partial", "blocked", "not_applicable", "unproven"})
_MEANING_FACET_SCHEMA = "capability-concept-meaning/1"
_MEANING_FACET_VALUES = MappingProxyType(
    {
        "semantic_role": frozenset(
            {
                "capability_identity",
                "operation",
                "state",
                "resource",
                "grounding_basis",
                "identity",
                "world_rule",
                "mechanic_modifier",
            }
        ),
        "target_cardinality": frozenset({"not_applicable", "unspecified", "single", "multiple"}),
        "spatial_extent": frozenset({"not_applicable", "unspecified", "entity", "area", "zone"}),
        "world_scope": frozenset(
            {
                "not_applicable",
                "unspecified",
                "personal",
                "local",
                "regional",
                "global",
                "cross_world",
            }
        ),
    }
)
_SEMANTIC_ROLE_BY_KIND = MappingProxyType(
    {
        "skill": "capability_identity",
        "ability": "capability_identity",
        "action": "operation",
        "functional_family": "operation",
        "semantic_primitive": "operation",
        "condition": "state",
        "status": "state",
        "relationship_state": "state",
        "resource": "resource",
        "basis": "grounding_basis",
        "identity": "identity",
        "world_state": "world_rule",
        "ability_mechanic": "mechanic_modifier",
    }
)
_NON_TARGET_ROLES = frozenset({"resource", "grounding_basis", "identity", "world_rule", "mechanic_modifier"})
_MEANING_FINGERPRINT_INCLUDES = (
    "concept_id",
    "concept_type",
    "categories",
    "definition",
    "meaning_facets",
)
_MEANING_FINGERPRINT_EXCLUDES = ("label", "aliases", "source_ids", "support")
_EXPECTED_WORLD_EVENT_FIELDS = frozenset(
    {
        "cause",
        "actor",
        "priority",
        "affected_domains",
        "scope",
        "propagation",
        "expiry",
        "reversibility",
        "supersession",
        "cause_visibility",
        "branch_replay_identity",
    }
)
_EXPECTED_GENRE_IDS = frozenset(
    {
        "high_fantasy_medieval",
        "dark_fantasy_gothic",
        "low_fantasy_historical",
        "urban_fantasy_modern",
        "mythological_ancient_world",
        "eastern_fantasy_wuxia_xianxia",
        "science_fantasy",
        "steampunk_victorian",
        "dieselpunk_world_war",
        "cyberpunk_dystopian",
        "space_opera_sci_fi",
        "space_western",
        "post_apoc_nuclear_zombies",
        "post_apoc_pandemic",
        "post_apoc_climate_collapse",
        "survival_horror",
        "supernatural_occult",
        "vampire_werewolf",
        "demon_angel",
        "crime_noir",
        "western_frontier",
        "pirate_nautical",
        "samurai_feudal_japan",
        "military_war",
        "superhero_modern",
        "alternate_history",
        "isekai_parallel_world",
        "virtual_world",
        "school_academy",
        "kingdom_empire",
        "dungeon_fantasy",
    }
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)


class GlossaryError(ValueError):
    """The local corpus or a proposed definition violates its versioned contract."""


def normalize_phrase(value: object) -> str:
    """Return a stable, conservative lookup form without stemming or fuzzy invention."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("'", " ").replace("’", " ").replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_fingerprint(value: object) -> str:
    """Hash canonical JSON so revisions are portable and replay-stable."""
    try:
        payload = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise GlossaryError("fingerprint payload must be finite JSON data") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def concept_meaning_fingerprint(concept: Mapping[str, Any]) -> str:
    """Hash only canonical meaning, excluding wording, provenance, and current support."""
    if not isinstance(concept, Mapping):
        raise GlossaryError("concept meaning fingerprint input must be an object")
    categories = concept.get("categories")
    facets = concept.get("meaning_facets")
    if (
        not isinstance(categories, (list, tuple))
        or any(not isinstance(item, str) or not item for item in categories)
        or not isinstance(facets, Mapping)
    ):
        raise GlossaryError("concept meaning fingerprint input is incomplete")
    payload = {
        "schema": _MEANING_FACET_SCHEMA,
        "concept_id": concept.get("id"),
        "concept_type": concept.get("concept_type"),
        "categories": sorted(set(categories)),
        "definition": concept.get("definition"),
        "meaning_facets": dict(facets),
    }
    return content_fingerprint(payload)


def raw_fingerprint(value: bytes) -> str:
    """Hash an artifact's exact bytes for manifest integrity checks."""
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_json_bytes(payload: bytes, label: Path) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GlossaryError(f"cannot load glossary artifact {label}: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise GlossaryError(f"glossary artifact must be an object: {label}")
    return data


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GlossaryError(f"cannot load glossary artifact {path}: {type(exc).__name__}") from exc
    return _read_json_bytes(payload, path)


def _resolve_artifact(root: Path, relative: object) -> Path:
    """Resolve one manifest path without permitting absolute or parent traversal."""
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
    ):
        raise GlossaryError("manifest artifact paths must be non-empty forward-slash strings")
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute()
        or posix.as_posix() != relative
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise GlossaryError(f"unsafe glossary artifact path: {relative!r}")
    rel = Path(*posix.parts)
    resolved_root = root.resolve()
    resolved = (resolved_root / rel).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise GlossaryError(f"glossary artifact escapes corpus root: {relative!r}")
    return resolved


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise GlossaryError(f"{field} must be a list of non-empty strings")
    return list(value)


def _definition_snapshot(draft: Mapping[str, Any]) -> dict[str, Any]:
    """Take one finite, deep JSON snapshot so validation and freezing see identical data."""
    if not isinstance(draft, Mapping):
        raise GlossaryError("definition draft must be an object")
    try:
        snapshot = json.loads(_canonical_json(dict(draft)))
    except (TypeError, ValueError, RuntimeError) as exc:
        raise GlossaryError("definition draft must be finite JSON data") from exc
    if not isinstance(snapshot, dict):
        raise GlossaryError("definition draft must be an object")
    return snapshot


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_plain(item) for item in value]
    return value


def _baseline_for_sources(source_ids: Iterable[str]) -> str:
    sources = set(source_ids)
    if sources & _REGISTRY_SOURCE_IDS:
        return "registry_corpus"
    if any(source_id.startswith("existing.") for source_id in sources):
        return "existing_corpus"
    return "gap_filled"


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise GlossaryError(f"{label} field mismatch; missing={missing}, extra={extra}")


def _validate_meaning_facets(
    value: object,
    *,
    concept_id: str,
    concept_type: str,
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GlossaryError(f"{concept_id}.meaning_facets must be an object")
    _exact_keys(value, frozenset(_MEANING_FACET_VALUES), f"{concept_id}.meaning_facets")
    facets: dict[str, str] = {}
    for facet, allowed in _MEANING_FACET_VALUES.items():
        selected = value.get(facet)
        if not isinstance(selected, str) or selected not in allowed:
            raise GlossaryError(f"{concept_id}.meaning facet {facet} is invalid")
        facets[facet] = selected
    expected_role = _SEMANTIC_ROLE_BY_KIND.get(concept_type)
    if facets["semantic_role"] != expected_role:
        raise GlossaryError(f"{concept_id}.meaning facet semantic_role conflicts with concept kind")
    if facets["semantic_role"] in _NON_TARGET_ROLES and (
        facets["target_cardinality"] != "not_applicable" or facets["spatial_extent"] != "not_applicable"
    ):
        raise GlossaryError(f"{concept_id}.meaning facets must mark non-target dimensions not_applicable")
    return facets


def _plain_catalog_rows(
    value: object,
    *,
    label: str,
    expected_ids: frozenset[str],
    row_keys: frozenset[str],
    list_fields: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GlossaryError(f"{label} must be a non-empty list")
    rows: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise GlossaryError(f"{label}[{index}] must be an object")
        _exact_keys(raw, row_keys, f"{label}[{index}]")
        row_id = str(raw.get("id", ""))
        if not normalize_phrase(row_id) or row_id in rows:
            raise GlossaryError(f"{label} has a missing or duplicate id: {row_id!r}")
        for field in row_keys - {"id"} - list_fields:
            if not isinstance(raw.get(field), str) or not str(raw[field]).strip():
                raise GlossaryError(f"{label}.{row_id}.{field} must be a non-empty string")
        for field in list_fields:
            values = _string_list(raw.get(field), f"{label}.{row_id}.{field}")
            if not values or len(values) != len(set(values)):
                raise GlossaryError(f"{label}.{row_id}.{field} must be non-empty and unique")
        rows[row_id] = dict(raw)
    if set(rows) != expected_ids:
        missing = sorted(expected_ids - set(rows))
        extra = sorted(set(rows) - expected_ids)
        raise GlossaryError(f"{label} catalog mismatch; missing={missing}, extra={extra}")
    return rows


def _validate_taxonomy_doc(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GlossaryError("Semantic Atlas taxonomy must be an object")
    _exact_keys(
        value,
        frozenset(
            {
                "schema",
                "name",
                "authority",
                "classification_planes",
                "concept_kinds",
                "scale_axes",
                "authority_stages",
                "cube_faces",
                "coverage_statuses",
                "meaning_facet_contract",
                "world_event_record",
            }
        ),
        "Semantic Atlas taxonomy",
    )
    if value.get("schema") != TAXONOMY_SCHEMA:
        raise GlossaryError("unsupported Semantic Atlas taxonomy schema")
    if value.get("name") != "Semantic Atlas" or value.get("authority") != "classification_only":
        raise GlossaryError("Semantic Atlas must declare classification_only authority")

    _plain_catalog_rows(
        value.get("classification_planes"),
        label="classification planes",
        expected_ids=_EXPECTED_CLASSIFICATION_PLANE_IDS,
        row_keys=frozenset({"id", "name", "stability", "owns", "does_not_own"}),
        list_fields=frozenset({"owns", "does_not_own"}),
    )
    _plain_catalog_rows(
        value.get("concept_kinds"),
        label="concept kinds",
        expected_ids=_EXPECTED_CONCEPT_KIND_IDS,
        row_keys=frozenset({"id", "label", "description", "example", "counterexample"}),
    )
    _plain_catalog_rows(
        value.get("scale_axes"),
        label="scale axes",
        expected_ids=_EXPECTED_SCALE_AXIS_IDS,
        row_keys=frozenset({"id", "label", "description"}),
    )
    _plain_catalog_rows(
        value.get("authority_stages"),
        label="authority stages",
        expected_ids=_EXPECTED_AUTHORITY_STAGE_IDS,
        row_keys=frozenset({"id", "label", "description"}),
    )
    _plain_catalog_rows(
        value.get("cube_faces"),
        label="Semantic Cube faces",
        expected_ids=_EXPECTED_CUBE_FACE_IDS,
        row_keys=frozenset({"id", "label", "description"}),
    )
    coverage = _string_list(value.get("coverage_statuses"), "coverage_statuses")
    if len(coverage) != len(set(coverage)) or set(coverage) != _EXPECTED_COVERAGE_STATUSES:
        raise GlossaryError("Semantic Atlas coverage statuses do not match the closed catalog")

    facet_contract = value.get("meaning_facet_contract")
    if not isinstance(facet_contract, dict):
        raise GlossaryError("Semantic Atlas meaning facet contract must be an object")
    _exact_keys(
        facet_contract,
        frozenset({"schema", "closed_values", "fingerprint_includes", "fingerprint_excludes"}),
        "meaning facet contract",
    )
    if facet_contract.get("schema") != _MEANING_FACET_SCHEMA:
        raise GlossaryError("unsupported meaning facet contract schema")
    closed_values = facet_contract.get("closed_values")
    if not isinstance(closed_values, dict) or set(closed_values) != set(_MEANING_FACET_VALUES):
        raise GlossaryError("meaning facet closed values are incomplete")
    for facet, expected in _MEANING_FACET_VALUES.items():
        actual = _string_list(closed_values.get(facet), f"meaning facet {facet}")
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise GlossaryError(f"meaning facet {facet} does not match the closed contract")
    includes = _string_list(facet_contract.get("fingerprint_includes"), "meaning fingerprint includes")
    excludes = _string_list(facet_contract.get("fingerprint_excludes"), "meaning fingerprint excludes")
    if tuple(includes) != _MEANING_FINGERPRINT_INCLUDES:
        raise GlossaryError("meaning fingerprint includes do not match the closed contract")
    if tuple(excludes) != _MEANING_FINGERPRINT_EXCLUDES:
        raise GlossaryError("meaning fingerprint excludes do not match the closed contract")

    event = value.get("world_event_record")
    if not isinstance(event, dict):
        raise GlossaryError("world_event_record must be an object")
    _exact_keys(
        event,
        frozenset({"name", "collection", "authority", "required_fields"}),
        "world_event_record",
    )
    if (
        event.get("name") != "World Event Record"
        or event.get("collection") != "World Overlay Stack"
        or event.get("authority") != "ledger_owned_after_admitted_settlement"
    ):
        raise GlossaryError("world_event_record names or authority are invalid")
    event_fields = _string_list(event.get("required_fields"), "world_event_record.required_fields")
    if len(event_fields) != len(set(event_fields)) or set(event_fields) != _EXPECTED_WORLD_EVENT_FIELDS:
        raise GlossaryError("world_event_record required fields do not match the closed contract")
    return dict(value)


def _validate_source_url(url: str, label: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GlossaryError(f"invalid source URL in {label}")
    if parsed.username or parsed.password:
        raise GlossaryError(f"credential-bearing source URL in {label}")
    sensitive = {
        key.casefold() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    } & _SENSITIVE_QUERY_KEYS
    if sensitive:
        raise GlossaryError(f"sensitive source URL query in {label}: {sorted(sensitive)}")


@dataclass(frozen=True)
class GlossaryMatch:
    """One lexical/genre candidate.  It is recognized, not owned or admitted."""

    concept_id: str
    label: str
    categories: tuple[str, ...]
    concept_type: str
    meaning_facets: Mapping[str, str]
    meaning_fingerprint: str
    matched_phrase: str
    score: int
    genre_ids: tuple[str, ...]
    baseline: str
    receipt_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "label": self.label,
            "categories": list(self.categories),
            "concept_type": self.concept_type,
            "meaning_facets": dict(self.meaning_facets),
            "meaning_fingerprint": self.meaning_fingerprint,
            "matched_phrase": self.matched_phrase,
            "score": self.score,
            "genre_ids": list(self.genre_ids),
            "baseline": self.baseline,
            "recognized": True,
            "authorized": False,
            "executable": False,
            "requires_context_binding": True,
            "receipt_ids": list(self.receipt_ids),
        }


@dataclass(frozen=True)
class _PhraseBinding:
    phrase: str
    concept_id: str
    genre_id: Optional[str]
    baseline: str
    source: str


class CapabilityGlossary:
    """Validated in-memory index over the project-owned glossary corpus."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        manifest = _read_json(self.root / "manifest.json")
        if manifest.get("schema") != GLOSSARY_SCHEMA:
            raise GlossaryError(f"unsupported glossary manifest schema: {manifest.get('schema')!r}")

        verified = self._verify_artifacts(manifest)

        categories_ref = manifest.get("categories_file")
        concepts_ref = manifest.get("concepts_file")
        sources_ref = manifest.get("sources_file")
        taxonomy_ref = manifest.get("taxonomy_file")
        categories_doc = _read_json_bytes(
            verified[categories_ref], _resolve_artifact(self.root, categories_ref)
        )
        concepts_doc = _read_json_bytes(verified[concepts_ref], _resolve_artifact(self.root, concepts_ref))
        sources_doc = _read_json_bytes(verified[sources_ref], _resolve_artifact(self.root, sources_ref))
        taxonomy_doc = _read_json_bytes(verified[taxonomy_ref], _resolve_artifact(self.root, taxonomy_ref))

        if categories_doc.get("schema") != "aetherstate-glossary-categories/2":
            raise GlossaryError("unsupported glossary category schema")
        if concepts_doc.get("schema") != "aetherstate-glossary-concepts/2":
            raise GlossaryError("unsupported glossary concept schema")
        if sources_doc.get("schema") != "aetherstate-glossary-sources/1":
            raise GlossaryError("unsupported glossary source schema")
        self.taxonomy = _validate_taxonomy_doc(taxonomy_doc)
        self._sealed_taxonomy: Mapping[str, Any] = _deep_freeze(self.taxonomy)

        category_rows = categories_doc.get("categories")
        if not isinstance(category_rows, list) or not category_rows:
            raise GlossaryError("glossary category catalog must be a non-empty list")
        self.categories: dict[str, dict[str, Any]] = {}
        for row in category_rows:
            if not isinstance(row, dict):
                raise GlossaryError("category rows must be objects")
            _exact_keys(
                row,
                frozenset(
                    {
                        "id",
                        "label",
                        "description",
                        "includes",
                        "excludes",
                        "examples",
                        "counterexamples",
                    }
                ),
                "category row",
            )
            if (
                not normalize_phrase(row.get("id"))
                or not str(row.get("label", "")).strip()
                or not str(row.get("description", "")).strip()
            ):
                raise GlossaryError("every category requires id, label, and description")
            category_id = str(row["id"])
            if category_id in self.categories:
                raise GlossaryError(f"duplicate category id: {category_id}")
            for field in ("includes", "excludes", "examples", "counterexamples"):
                values = _string_list(row.get(field), f"{category_id}.{field}")
                if not values or len(values) != len(set(values)):
                    raise GlossaryError(f"{category_id}.{field} must be non-empty and unique")
            self.categories[category_id] = dict(row)
        if set(self.categories) != _EXPECTED_CATEGORY_IDS:
            raise GlossaryError("glossary category catalog does not match the versioned v1 IDs")

        source_rows = sources_doc.get("sources")
        if not isinstance(source_rows, list) or not source_rows:
            raise GlossaryError("glossary source catalog must be a non-empty list")
        self.sources: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            if not isinstance(row, dict) or not normalize_phrase(row.get("id")):
                raise GlossaryError("every source requires an id")
            source_id = str(row["id"])
            if source_id in self.sources:
                raise GlossaryError(f"duplicate source id: {source_id}")
            self.sources[source_id] = dict(row)

        concept_rows = concepts_doc.get("concepts")
        if not isinstance(concept_rows, list) or not concept_rows:
            raise GlossaryError("glossary concept catalog must be a non-empty list")
        self.concepts: dict[str, dict[str, Any]] = {}
        for row in concept_rows:
            self._add_concept(row)
        # Public corpus data remains convenient to inspect, but every authority decision reads this
        # private immutable snapshot so callers cannot mutate recognition metadata into permission.
        self._sealed_concepts: Mapping[str, Mapping[str, Any]] = _deep_freeze(self.concepts)

        self.genres: dict[str, dict[str, Any]] = {}
        self._cluster_ids: set[str] = set()
        self._bindings: list[_PhraseBinding] = []
        for concept_id, concept in self.concepts.items():
            terms = [concept["label"], *_string_list(concept.get("aliases"), f"{concept_id}.aliases")]
            for term in terms:
                phrase = normalize_phrase(term)
                if phrase:
                    self._bindings.append(_PhraseBinding(phrase, concept_id, None, "canonical", "concept"))

        genre_files = manifest.get("genre_files")
        if not isinstance(genre_files, list) or not genre_files:
            raise GlossaryError("manifest.genre_files must be a non-empty list")
        for rel in genre_files:
            if not isinstance(rel, str):
                raise GlossaryError("manifest.genre_files entries must be strings")
            self._load_genre_file(_resolve_artifact(self.root, rel), verified[rel])
        if set(self.genres) != _EXPECTED_GENRE_IDS:
            raise GlossaryError("glossary genre catalog does not match the versioned v1 IDs")
        self._sealed_genres: Mapping[str, Mapping[str, Any]] = _deep_freeze(self.genres)
        self._validate_source_catalog()
        self._sealed_genre_ids = frozenset(self.genres)

        expected = manifest.get("counts")
        count_keys = {
            "categories",
            "classification_planes",
            "concepts",
            "concept_kinds",
            "cube_faces",
            "genres",
            "scale_axes",
            "sources",
        }
        if not isinstance(expected, dict) or set(expected) != count_keys:
            raise GlossaryError(
                "manifest.counts must contain exactly categories, concepts, genres, and sources"
            )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in expected.values()
        ):
            raise GlossaryError("manifest counts must be non-negative integers")
        actual = {
            "categories": len(self.categories),
            "classification_planes": len(self._sealed_taxonomy["classification_planes"]),
            "concepts": len(self.concepts),
            "concept_kinds": len(self._sealed_taxonomy["concept_kinds"]),
            "cube_faces": len(self._sealed_taxonomy["cube_faces"]),
            "genres": len(self.genres),
            "scale_axes": len(self._sealed_taxonomy["scale_axes"]),
            "sources": len(self.sources),
        }
        for key, count in actual.items():
            if expected[key] != count:
                raise GlossaryError(
                    f"manifest count mismatch for {key}: declared {expected[key]}, found {count}"
                )

        # Longest phrases win ties and deterministic ordering never depends on filesystem order.
        self._bindings.sort(
            key=lambda item: (-len(item.phrase.split()), item.phrase, item.concept_id, item.genre_id or "")
        )

    def _verify_artifacts(self, manifest: Mapping[str, Any]) -> dict[str, bytes]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise GlossaryError("manifest.artifacts must be a non-empty object")
        referenced_values = [
            manifest.get("categories_file"),
            manifest.get("concepts_file"),
            manifest.get("sources_file"),
            manifest.get("taxonomy_file"),
        ]
        genre_files = manifest.get("genre_files")
        if not isinstance(genre_files, list) or not genre_files:
            raise GlossaryError("manifest.genre_files must be a non-empty list")
        referenced_values.extend(genre_files)
        if any(not isinstance(rel, str) for rel in referenced_values):
            raise GlossaryError("manifest artifact references must be strings")
        referenced = set(referenced_values)
        if len(referenced_values) != len(referenced):
            raise GlossaryError("manifest artifact references must be unique")
        required_artifacts = referenced | {
            "README.md",
            "research/2026-07-13-genre-gap-audit.md",
        }
        if set(artifacts) != required_artifacts:
            missing = sorted(str(rel) for rel in required_artifacts - set(artifacts))
            extra = sorted(str(rel) for rel in set(artifacts) - required_artifacts)
            raise GlossaryError(f"manifest artifact catalog mismatch; missing={missing}, extra={extra}")
        verified: dict[str, bytes] = {}
        for rel, declaration in artifacts.items():
            path = _resolve_artifact(self.root, rel)
            if not isinstance(declaration, dict):
                raise GlossaryError(f"manifest artifact declaration must be an object: {rel}")
            expected_bytes = declaration.get("bytes")
            if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int):
                raise GlossaryError(f"manifest artifact bytes must be an integer: {rel}")
            if expected_bytes < 0:
                raise GlossaryError(f"manifest artifact bytes cannot be negative: {rel}")
            expected_fingerprint = declaration.get("fingerprint")
            if not isinstance(expected_fingerprint, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", expected_fingerprint
            ):
                raise GlossaryError(f"invalid artifact fingerprint in manifest: {rel}")
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise GlossaryError(f"cannot read declared glossary artifact: {rel}") from exc
            if len(payload) != expected_bytes or raw_fingerprint(payload) != expected_fingerprint:
                raise GlossaryError(f"glossary artifact integrity mismatch: {rel}")
            verified[rel] = payload
        return verified

    @classmethod
    def load(cls, root: Path | str) -> "CapabilityGlossary":
        return cls(Path(root))

    def _validate_source_catalog(self) -> None:
        expected_web: dict[str, dict[str, Any]] = {}
        for genre_id, genre in self.genres.items():
            for url in genre["source_urls"]:
                source_id = "web." + hashlib.sha256(url.encode("utf-8")).hexdigest()
                row = expected_web.setdefault(source_id, {"url": url, "genre_ids": set()})
                if row["url"] != url:
                    raise GlossaryError(f"web source hash collision: {source_id}")
                row["genre_ids"].add(genre_id)

        expected_ids = set(_LOCAL_SOURCE_KINDS) | set(expected_web)
        if set(self.sources) != expected_ids:
            missing = sorted(expected_ids - set(self.sources))
            extra = sorted(set(self.sources) - expected_ids)
            raise GlossaryError(f"source catalog mismatch; missing={missing}, extra={extra}")

        for source_id, kind in _LOCAL_SOURCE_KINDS.items():
            if self.sources[source_id].get("kind") != kind:
                raise GlossaryError(f"local source has invalid kind: {source_id}")
        for source_id, expected in expected_web.items():
            source = self.sources[source_id]
            genre_ids = _string_list(source.get("genre_ids"), f"{source_id}.genre_ids")
            if (
                source.get("kind") != "web_research"
                or source.get("url") != expected["url"]
                or set(genre_ids) != expected["genre_ids"]
                or source_id
                != "web." + hashlib.sha256(str(source.get("url", "")).encode("utf-8")).hexdigest()
            ):
                raise GlossaryError(f"inconsistent web source catalog row: {source_id}")

    def _add_concept(self, row: object) -> None:
        if not isinstance(row, dict):
            raise GlossaryError("concept rows must be objects")
        _exact_keys(
            row,
            frozenset(
                {
                    "id",
                    "label",
                    "concept_type",
                    "categories",
                    "aliases",
                    "definition",
                    "meaning_facets",
                    "meaning_fingerprint",
                    "source_ids",
                    "support",
                }
            ),
            "concept row",
        )
        concept_id = str(row.get("id", ""))
        label = str(row.get("label", "")).strip()
        concept_type = str(row.get("concept_type", "")).strip()
        categories = _string_list(row.get("categories"), f"{concept_id}.categories")
        definition = str(row.get("definition", "")).strip()
        if (
            not normalize_phrase(concept_id)
            or not label
            or not concept_type
            or not categories
            or not definition
        ):
            raise GlossaryError("every concept requires id, label, concept_type, categories, and definition")
        if concept_type not in _EXPECTED_CONCEPT_KIND_IDS:
            raise GlossaryError(f"{concept_id} references unknown concept kind: {concept_type!r}")
        if concept_id in self.concepts:
            raise GlossaryError(f"duplicate concept id: {concept_id}")
        unknown = set(categories) - set(self.categories)
        if unknown:
            raise GlossaryError(f"{concept_id} references unknown categories: {sorted(unknown)}")
        if len(categories) != len(set(categories)):
            raise GlossaryError(f"{concept_id}.categories must be unique")
        _string_list(row.get("aliases"), f"{concept_id}.aliases")
        facets = _validate_meaning_facets(
            row.get("meaning_facets"),
            concept_id=concept_id,
            concept_type=concept_type,
        )
        declared_meaning_fingerprint = row.get("meaning_fingerprint")
        if (
            not isinstance(declared_meaning_fingerprint, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", declared_meaning_fingerprint)
            or declared_meaning_fingerprint != concept_meaning_fingerprint({**row, "meaning_facets": facets})
        ):
            raise GlossaryError(f"{concept_id}.meaning fingerprint does not match canonical meaning")
        source_ids = _string_list(row.get("source_ids"), f"{concept_id}.source_ids")
        if not source_ids:
            raise GlossaryError(f"{concept_id} requires at least one provenance source")
        if not set(source_ids) <= _LOCAL_PROVENANCE_IDS:
            raise GlossaryError(f"{concept_id} uses non-local concept provenance")
        missing_sources = set(source_ids) - set(self.sources)
        if missing_sources:
            raise GlossaryError(f"{concept_id} references unknown sources: {sorted(missing_sources)}")
        support = row.get("support") or {}
        if not isinstance(support, dict):
            raise GlossaryError(f"{concept_id}.support must be an object")
        receipts = support.get("receipt_ids", [])
        _string_list(receipts, f"{concept_id}.support.receipt_ids")
        if receipts:
            raise GlossaryError(f"{concept_id} advertises a receipt without a compiler-v1 adapter")
        if support.get("recognition") not in _SUPPORT_RECOGNITION:
            raise GlossaryError(f"{concept_id}.support.recognition is unsupported")
        if support.get("authorization") not in _SUPPORT_AUTHORIZATION:
            raise GlossaryError(f"{concept_id}.support.authorization is unsupported")
        if support.get("narration") not in _SUPPORT_NARRATION:
            raise GlossaryError(f"{concept_id}.support.narration is unsupported")
        self.concepts[concept_id] = dict(row)

    def _load_genre_file(self, path: Path, payload: bytes) -> None:
        doc = _read_json_bytes(payload, path)
        if doc.get("schema") != "aetherstate-genre-glossary-cluster/1":
            raise GlossaryError(f"unsupported genre cluster schema: {path}")
        cluster = str(doc.get("cluster", "")).strip()
        if not cluster:
            raise GlossaryError(f"genre cluster requires a non-empty cluster id: {path}")
        if cluster in self._cluster_ids:
            raise GlossaryError(f"duplicate genre cluster id: {cluster}")
        self._cluster_ids.add(cluster)
        requests = doc.get("concept_requests") or []
        if requests:
            raise GlossaryError(f"unresolved concept requests in genre cluster: {path}")
        genres = doc.get("genres")
        if not isinstance(genres, list) or not genres:
            raise GlossaryError(f"genre cluster must contain genres: {path}")
        for genre in genres:
            if not isinstance(genre, dict):
                raise GlossaryError(f"genre rows must be objects: {path}")
            genre_id = str(genre.get("id", ""))
            if not normalize_phrase(genre_id) or genre_id in self.genres:
                raise GlossaryError(f"missing or duplicate genre id: {genre_id!r}")
            if not str(genre.get("label", "")).strip():
                raise GlossaryError(f"{genre_id} requires a label")
            for field in ("aliases", "facets", "false_friends", "source_urls"):
                values = _string_list(genre.get(field), f"{genre_id}.{field}")
                if not values:
                    raise GlossaryError(f"{genre_id}.{field} cannot be empty")
            for url in genre["source_urls"]:
                _validate_source_url(url, genre_id)
            category_rows = genre.get("categories")
            if not isinstance(category_rows, dict):
                raise GlossaryError(f"{genre_id}.categories must be an object")
            if set(category_rows) != set(self.categories):
                missing = sorted(set(self.categories) - set(category_rows))
                extra = sorted(set(category_rows) - set(self.categories))
                raise GlossaryError(
                    f"{genre_id} category coverage mismatch; missing={missing}, extra={extra}"
                )
            normalized_categories: dict[str, list[dict[str, Any]]] = {}
            phrase_targets: dict[str, str] = {}
            for category_id, entries in category_rows.items():
                if not isinstance(entries, list) or not entries:
                    raise GlossaryError(f"{genre_id}.{category_id} must contain at least one entry")
                normalized_categories[category_id] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise GlossaryError(f"{genre_id}.{category_id} entries must be objects")
                    concept_id = str(entry.get("concept_id", ""))
                    if concept_id not in self.concepts:
                        raise GlossaryError(f"{genre_id} references unknown concept: {concept_id}")
                    if category_id not in self.concepts[concept_id]["categories"]:
                        raise GlossaryError(
                            f"{genre_id}.{category_id} uses {concept_id} outside its declared categories"
                        )
                    baseline = str(entry.get("baseline", ""))
                    if baseline not in {"existing_corpus", "registry_corpus", "gap_filled"}:
                        raise GlossaryError(f"{genre_id}.{concept_id} has invalid baseline: {baseline!r}")
                    expected_baseline = _baseline_for_sources(
                        self._sealed_concepts[concept_id].get("source_ids", ())
                    )
                    if baseline != expected_baseline:
                        raise GlossaryError(
                            f"baseline/provenance mismatch for {genre_id}.{concept_id}: "
                            f"declared {baseline}, expected {expected_baseline}"
                        )
                    priority = str(entry.get("priority", ""))
                    if priority not in {"critical", "high", "medium"}:
                        raise GlossaryError(f"{genre_id}.{concept_id} has invalid priority: {priority!r}")
                    terms = _string_list(entry.get("terms"), f"{genre_id}.{concept_id}.terms")
                    if not terms or any(not item.strip() for item in terms):
                        raise GlossaryError(f"{genre_id}.{concept_id} requires at least one translation term")
                    clean = dict(entry)
                    clean["terms"] = terms
                    normalized_categories[category_id].append(clean)
                    for term in terms:
                        phrase = normalize_phrase(term)
                        if not phrase:
                            raise GlossaryError(f"{genre_id}.{concept_id} contains an empty normalized term")
                        previous = phrase_targets.get(phrase)
                        if previous is not None and previous != concept_id:
                            raise GlossaryError(
                                f"same-genre phrase collision in {genre_id}: "
                                f"{term!r} maps to {previous} and {concept_id}"
                            )
                        phrase_targets[phrase] = concept_id
                        self._bindings.append(
                            _PhraseBinding(phrase, concept_id, genre_id, baseline, str(path.name))
                        )
            source_ids = _string_list(genre.get("source_ids"), f"{genre_id}.source_ids")
            missing_sources = set(source_ids) - set(self.sources)
            if missing_sources:
                raise GlossaryError(f"{genre_id} references unknown sources: {sorted(missing_sources)}")
            expected_source_ids = {
                "web." + hashlib.sha256(url.encode("utf-8")).hexdigest() for url in genre["source_urls"]
            }
            if set(source_ids) != expected_source_ids:
                raise GlossaryError(f"{genre_id} source IDs do not match its normalized source URLs")
            for source_id in source_ids:
                source = self.sources[source_id]
                if (
                    source.get("kind") != "web_research"
                    or source.get("url") not in genre["source_urls"]
                    or genre_id not in source.get("genre_ids", [])
                ):
                    raise GlossaryError(f"{genre_id} has inconsistent source reverse mapping")
            stored = dict(genre)
            stored["categories"] = normalized_categories
            self.genres[genre_id] = stored

    def concept_lineage(
        self,
        concept_id: str,
        genre_ids: Iterable[str] = (),
    ) -> tuple[str, tuple[str, ...]]:
        """Return immutable concept identity and provenance, independent of match context."""
        concept = self._sealed_concepts.get(concept_id)
        if concept is None:
            raise GlossaryError(f"unknown capability concept: {concept_id}")
        requested = tuple(dict.fromkeys(str(item) for item in genre_ids))
        unknown = set(requested) - self._sealed_genre_ids
        if unknown:
            raise GlossaryError(f"unknown genre ids: {sorted(unknown)}")
        snapshot = _deep_plain(concept)
        source_ids = list(str(item) for item in concept.get("source_ids", ()))
        for genre_id in requested:
            for source_id in self._sealed_genres[genre_id].get("source_ids", ()):
                if str(source_id) not in source_ids:
                    source_ids.append(str(source_id))
        return content_fingerprint(snapshot), tuple(source_ids)

    def translate(
        self,
        text: object,
        genre_ids: Iterable[str] = (),
        *,
        limit: int = 12,
        all_surfaces: bool = False,
    ) -> list[GlossaryMatch]:
        """Return deterministic recognition candidates; never authorize or execute them."""
        normalized = normalize_phrase(text)
        if not normalized or limit <= 0:
            return []
        requested = tuple(dict.fromkeys(str(item) for item in genre_ids))
        unknown = set(requested) - self._sealed_genre_ids
        if unknown:
            raise GlossaryError(f"unknown genre ids: {sorted(unknown)}")

        padded = f" {normalized} "
        best: dict[object, GlossaryMatch] = {}
        for binding in self._bindings:
            if f" {binding.phrase} " not in padded:
                continue
            concept = self._sealed_concepts[binding.concept_id]
            score = 100 if binding.phrase == normalized else 50 + min(20, len(binding.phrase.split()) * 3)
            if binding.genre_id:
                score += 8
                if binding.genre_id in requested:
                    score += 25
                elif requested:
                    score -= 12
            else:
                score += 4
            support = concept.get("support") or {}
            match = GlossaryMatch(
                concept_id=binding.concept_id,
                label=str(concept["label"]),
                categories=tuple(concept["categories"]),
                concept_type=str(concept["concept_type"]),
                meaning_facets=MappingProxyType(dict(concept["meaning_facets"])),
                meaning_fingerprint=str(concept["meaning_fingerprint"]),
                matched_phrase=binding.phrase,
                score=score,
                genre_ids=(binding.genre_id,) if binding.genre_id else (),
                baseline=binding.baseline,
                receipt_ids=tuple(str(item) for item in support.get("receipt_ids", ())),
            )
            key: object = (binding.concept_id, binding.phrase) if all_surfaces else binding.concept_id
            previous = best.get(key)
            if previous is None or (match.score, match.matched_phrase) > (
                previous.score,
                previous.matched_phrase,
            ):
                best[key] = match
        return sorted(
            best.values(),
            key=lambda item: (-item.score, item.concept_id, item.matched_phrase),
        )[:limit]

    def genre_coverage(self, genre_id: str) -> dict[str, Any]:
        if genre_id not in self.genres:
            raise GlossaryError(f"unknown genre id: {genre_id}")
        return json.loads(json.dumps(self.genres[genre_id], ensure_ascii=False))

    def concept_classification(self, concept_id: str) -> dict[str, Any]:
        """Return stable Concept Facets without mutable support or runtime authority."""
        concept = self._sealed_concepts.get(concept_id)
        if concept is None:
            raise GlossaryError(f"unknown concept id: {concept_id}")
        return {
            "schema": "aetherstate-concept-facets/1",
            "concept_id": concept_id,
            "concept_kind": str(concept["concept_type"]),
            "domain_shelves": [str(item) for item in concept["categories"]],
            "meaning_facets": _deep_plain(concept["meaning_facets"]),
            "meaning_fingerprint": str(concept["meaning_fingerprint"]),
        }

    def _receipt_validation(
        self,
        draft: Mapping[str, Any],
        kind: str,
        concept_ids: list[str],
        receipt_concept_ids: list[str],
        requested_receipt: str,
    ) -> dict[str, Any]:
        """Validate explicit receipt coverage against the real reducer envelope."""
        reasons: list[str] = []
        if not requested_receipt:
            if receipt_concept_ids:
                reasons.append("receipt_concept_ids requires a requested receipt_id")
            return {
                "requested_receipt_id": None,
                "receipt_concept_ids": receipt_concept_ids,
                "admitted": False,
                "reasons": reasons or ["No receipt was requested."],
            }
        if requested_receipt not in RECEIPT_KIND_ADMISSION:
            reasons.append("The requested receipt is not admitted by this compiler version.")
        elif kind not in RECEIPT_KIND_ADMISSION[requested_receipt]:
            reasons.append(f"The requested receipt cannot settle capability kind {kind}.")
        if not receipt_concept_ids:
            reasons.append("Receipt coverage must be explicit in receipt_concept_ids.")
        outside = sorted(set(receipt_concept_ids) - set(concept_ids))
        if outside:
            reasons.append(f"Receipt coverage is outside the definition concepts: {outside}.")
        for concept_id in receipt_concept_ids:
            if concept_id not in self._sealed_concepts:
                continue
            support = self._sealed_concepts[concept_id].get("support") or {}
            advertised = tuple(str(item) for item in support.get("receipt_ids", ()))
            if requested_receipt not in advertised:
                reasons.append(f"{concept_id} does not advertise {requested_receipt}.")

        if requested_receipt == "enemy-opposition-hp/1":
            missing = [
                field
                for field in _ENEMY_HP_REQUIRED_FIELDS
                if not isinstance(draft.get(field), str) or not str(draft.get(field)).strip()
            ]
            if missing:
                reasons.append(f"Enemy HP receipt is missing required fields: {missing}.")
            basis = str(draft.get("basis", "")).strip()
            primitive = str(draft.get("semantic_primitive", "")).strip()
            family = str(draft.get("functional_family", "")).strip()
            timing = str(draft.get("timing", "")).strip()
            cadence = str(draft.get("cadence", "")).strip()
            if basis and f"basis.{basis}" not in self._sealed_concepts:
                reasons.append(f"Unknown grounded basis: {basis}.")
            if primitive and f"primitive.{primitive}" not in self._sealed_concepts:
                reasons.append(f"Unknown semantic primitive: {primitive}.")
            if primitive and primitive != "strike":
                reasons.append("Enemy HP receipt currently admits only the strike primitive.")
            if family and family not in {"direct_pressure", "committed_strike"}:
                reasons.append("Enemy HP receipt currently admits only direct pressure or committed strike.")
            if family and f"family.{family}" not in receipt_concept_ids:
                reasons.append("The functional family must be explicitly covered by the receipt.")
            if family and set(receipt_concept_ids) != {f"family.{family}"}:
                reasons.append("Enemy HP receipt covers exactly its one functional family.")
            if str(draft.get("effect_channel", "")).strip() not in {"", "hp"}:
                reasons.append("Enemy HP receipt requires effect_channel=hp.")
            if str(draft.get("target", "")).strip() not in {"", "player", "single_player"}:
                reasons.append("Enemy HP receipt requires one Player target.")
            if str(draft.get("range", "")).strip() not in {"", "close", "near", "far"}:
                reasons.append("Enemy HP receipt range must be close, near, or far.")
            if str(draft.get("area", "")).strip() not in {"", "single_target"}:
                reasons.append("Enemy HP receipt does not admit an area or extra target.")
            if timing and timing not in _ENEMY_HP_TIMINGS:
                reasons.append("Enemy HP receipt timing is outside the frozen enemy move vocabulary.")
            if cadence and cadence not in _ENEMY_HP_CADENCES:
                reasons.append("Enemy HP receipt cadence is outside the frozen enemy move vocabulary.")
        elif requested_receipt == "check/1" and receipt_concept_ids:
            if len(receipt_concept_ids) != 1:
                reasons.append("check/1 covers exactly one canonical skill concept.")
            if any(
                self._sealed_concepts[item].get("concept_type") != "skill" for item in receipt_concept_ids
            ):
                reasons.append("check/1 coverage must contain only canonical skill concepts.")
            if any(
                "existing.registry_snapshot" not in self._sealed_concepts[item].get("source_ids", ())
                for item in receipt_concept_ids
            ):
                reasons.append("check/1 currently admits only preserved registry skills.")
        elif requested_receipt in {"ability-check-shape/1", "ability-basis/1"} and receipt_concept_ids:
            if len(receipt_concept_ids) != 1:
                reasons.append("An ability receipt covers exactly one canonical ability concept.")
            if any(
                self._sealed_concepts[item].get("concept_type") not in {"ability", "ability_mechanic"}
                for item in receipt_concept_ids
            ):
                reasons.append("Ability receipt coverage must contain only registry ability concepts.")
            if any(
                not {
                    "existing.registry_snapshot",
                    "existing.registry_runtime",
                }
                & set(self._sealed_concepts[item].get("source_ids", ()))
                for item in receipt_concept_ids
            ):
                reasons.append("Ability receipts currently admit only preserved registry abilities.")

        return {
            "requested_receipt_id": requested_receipt,
            "receipt_concept_ids": receipt_concept_ids,
            "admitted": not reasons,
            "reasons": reasons or ["Receipt coverage and reducer envelope are valid."],
        }

    def _preview_snapshot(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Normalize one immutable plain-data snapshot and classify its recognized concepts."""
        kind = normalize_phrase(draft.get("kind")).replace(" ", "_")
        name = str(draft.get("name", "")).strip()
        world_id = str(draft.get("world_id", "")).strip()
        if kind not in CAPABILITY_KINDS:
            raise GlossaryError(f"unsupported capability kind: {kind!r}")
        if not name or not world_id:
            raise GlossaryError("definition draft requires name and world_id")
        owner_scope = str(draft.get("owner_scope", "world")).strip()
        if owner_scope not in {"world", "actor", "enemy_blueprint"}:
            raise GlossaryError("owner_scope must be world, actor, or enemy_blueprint")
        owner_id = str(draft.get("owner_id", "")).strip()
        if owner_scope == "world":
            owner_id = owner_id or world_id
        elif not owner_id:
            raise GlossaryError(f"owner_id is required for owner_scope={owner_scope}")

        genre_ids = sorted(set(_string_list(draft.get("genre_ids"), "genre_ids")))
        unknown_genres = set(genre_ids) - self._sealed_genre_ids
        if unknown_genres:
            raise GlossaryError(f"unknown genre ids: {sorted(unknown_genres)}")
        requested_concepts = _string_list(draft.get("concept_ids"), "concept_ids")
        unknown_concepts = set(requested_concepts) - set(self._sealed_concepts)
        if unknown_concepts:
            raise GlossaryError(f"unknown concept ids: {sorted(unknown_concepts)}")

        source_text = " ".join(str(draft.get(key, "")) for key in ("name", "source_wording", "description"))
        inferred = [match.concept_id for match in self.translate(source_text, genre_ids, limit=64)]
        concept_ids = sorted(set([*requested_concepts, *inferred]))
        grounding = _string_list(draft.get("grounding_evidence"), "grounding_evidence")
        requested_receipt = str(draft.get("receipt_id", "")).strip()
        receipt_concept_ids = sorted(
            set(_string_list(draft.get("receipt_concept_ids"), "receipt_concept_ids"))
        )
        unknown_receipt_concepts = set(receipt_concept_ids) - set(self._sealed_concepts)
        if unknown_receipt_concepts:
            raise GlossaryError(f"unknown receipt concept ids: {sorted(unknown_receipt_concepts)}")
        receipt_validation = self._receipt_validation(
            draft,
            kind,
            concept_ids,
            receipt_concept_ids,
            requested_receipt,
        )

        classifications: list[dict[str, str]] = []
        if not concept_ids:
            classifications.append(
                {
                    "concept_id": "",
                    "classification": "rejected_grounding",
                    "reason": "No canonical glossary concept matched; keep the draft editable.",
                }
            )
        for concept_id in concept_ids:
            concept = self._sealed_concepts[concept_id]
            support = concept.get("support") or {}
            if not grounding:
                classification = "rejected_grounding"
                reason = "No world, actor, equipment, training, or authoring evidence was supplied."
            elif concept_id in receipt_concept_ids and receipt_validation["admitted"]:
                classification = "supported_and_frozen"
                reason = "Grounded concept is explicitly covered by a validated admitted receipt."
            elif str(support.get("narration", "")) == "boundary":
                classification = "narration_boundary"
                reason = "Meaning is recognized, but only its narration boundary is currently supported."
            else:
                classification = "lore_only"
                reason = "Meaning is preserved, but no admitted receipt was supplied for this definition."
            classifications.append(
                {"concept_id": concept_id, "classification": classification, "reason": reason}
            )

        return {
            "schema": "capability-definition-preview/1",
            "compiler_version": COMPILER_VERSION,
            "kind": kind,
            "name": name,
            "world_id": world_id,
            "owner_scope": owner_scope,
            "owner_id": owner_id,
            "genre_ids": genre_ids,
            "concept_ids": concept_ids,
            "classifications": classifications,
            "receipt_validation": receipt_validation,
        }

    def preview_definition(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Snapshot, normalize, and visibly classify every recognized concept before freezing."""
        return self._preview_snapshot(_definition_snapshot(draft))

    def freeze_definition(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze an immutable, content-addressed revision after deterministic preview."""
        draft = _definition_snapshot(draft)
        preview = self._preview_snapshot(draft)
        if any(row["classification"] == "rejected_grounding" for row in preview["classifications"]):
            raise GlossaryError("cannot freeze a definition with rejected grounding")

        aliases = sorted(set(_string_list(draft.get("aliases"), "aliases")))
        genre_ids = list(preview["genre_ids"])
        revision = draft.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise GlossaryError("revision must be an integer")
        if revision < 1:
            raise GlossaryError("revision must be at least 1")
        parent_fingerprint = str(draft.get("parent_fingerprint", "")).strip() or None
        if revision > 1 and not parent_fingerprint:
            raise GlossaryError("revisions after 1 require parent_fingerprint")
        if parent_fingerprint and not re.fullmatch(r"sha256:[0-9a-f]{64}", parent_fingerprint):
            raise GlossaryError("parent_fingerprint must be a sha256 content fingerprint")

        definition_id = str(
            draft.get("definition_id") or normalize_phrase(preview["name"]).replace(" ", "_")
        ).strip()
        if not normalize_phrase(definition_id):
            raise GlossaryError("definition_id cannot be empty")

        record: dict[str, Any] = {
            "schema": DEFINITION_SCHEMA,
            "compiler_version": COMPILER_VERSION,
            "definition_id": definition_id,
            "revision": revision,
            "parent_fingerprint": parent_fingerprint,
            "kind": preview["kind"],
            "name": preview["name"],
            "aliases": aliases,
            "source_wording": str(draft.get("source_wording", "")),
            "description": str(draft.get("description", "")),
            "world_id": preview["world_id"],
            "owner_scope": preview["owner_scope"],
            "owner_id": preview["owner_id"],
            "authoring_source": str(draft.get("authoring_source", "creator")),
            "genre_ids": genre_ids,
            "concept_ids": list(preview["concept_ids"]),
            "basis": str(draft.get("basis") or "").strip(),
            "delivery": str(draft.get("delivery") or "").strip(),
            "semantic_primitive": str(draft.get("semantic_primitive") or "").strip(),
            "functional_family": str(draft.get("functional_family") or "").strip(),
            "effect_channel": str(draft.get("effect_channel") or "").strip(),
            "target": draft.get("target").strip()
            if isinstance(draft.get("target"), str)
            else draft.get("target"),
            "range": draft.get("range").strip()
            if isinstance(draft.get("range"), str)
            else draft.get("range"),
            "area": draft.get("area").strip() if isinstance(draft.get("area"), str) else draft.get("area"),
            "timing": draft.get("timing").strip()
            if isinstance(draft.get("timing"), str)
            else draft.get("timing"),
            "cadence": draft.get("cadence").strip()
            if isinstance(draft.get("cadence"), str)
            else draft.get("cadence"),
            "duration": draft.get("duration"),
            "availability": draft.get("availability"),
            "cost": draft.get("cost") if isinstance(draft.get("cost"), Mapping) else {},
            "power_ceiling": draft.get("power_ceiling"),
            "mastery": draft.get("mastery"),
            "prerequisites": draft.get("prerequisites")
            if isinstance(draft.get("prerequisites"), Mapping)
            else {},
            "side_effects": _string_list(draft.get("side_effects"), "side_effects"),
            "counterplay": _string_list(draft.get("counterplay"), "counterplay"),
            "risk": _string_list(draft.get("risk"), "risk"),
            "world_scale_potential": str(draft.get("world_scale_potential", "personal")),
            "grounding_evidence": _string_list(draft.get("grounding_evidence"), "grounding_evidence"),
            "support_classification": list(preview["classifications"]),
            "receipt_concept_ids": list(preview["receipt_validation"]["receipt_concept_ids"]),
            "receipt_validation": dict(preview["receipt_validation"]),
            "receipt_id": None,
            "interpretation_mode": str(draft.get("interpretation_mode", "automatic")),
        }
        if record["interpretation_mode"] not in {"automatic", "explicit_only", "disabled"}:
            raise GlossaryError("interpretation_mode must be automatic, explicit_only, or disabled")
        requested_receipt = str(draft.get("receipt_id", "")).strip() or None
        record["requested_receipt_id"] = requested_receipt
        record["receipt_id"] = requested_receipt if preview["receipt_validation"]["admitted"] else None
        fingerprint_payload = dict(record)
        record["fingerprint"] = content_fingerprint(fingerprint_payload)
        return record


def load_default_glossary(project_root: Path | str | None = None) -> CapabilityGlossary:
    """Load the sealed project or installed-package corpus on an explicit cold path.

    A caller may pass the repository root or the glossary directory itself.  The function raises a
    bounded ``GlossaryError`` rather than guessing a network or user-data location.
    """
    if project_root is not None:
        root = Path(project_root)
        if (root / "manifest.json").is_file():
            return CapabilityGlossary.load(root)
        return CapabilityGlossary.load(root / "corpus" / "capability-glossary")

    package_corpus = Path(__file__).resolve().parent / "corpus" / "capability-glossary"
    if package_corpus.is_dir():
        return CapabilityGlossary.load(package_corpus)
    repo_corpus = Path(__file__).resolve().parents[2] / "corpus" / "capability-glossary"
    return CapabilityGlossary.load(repo_corpus)
