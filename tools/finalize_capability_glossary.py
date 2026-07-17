"""Preflight, normalize provenance, and atomically seal the capability glossary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


SCHEMA = "aetherstate-capability-glossary/2"
TAXONOMY_SCHEMA = "aetherstate-semantic-atlas-taxonomy/1"
GENRE_SCHEMA = "aetherstate-genre-glossary-cluster/1"
EXPECTED_GENRES = {
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
EXPECTED_CATEGORIES = {
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
EXPECTED_CONCEPT_KINDS = {
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
EXPECTED_CLASSIFICATION_PLANES = {
    "concept_facets",
    "scale_profile",
    "cube_coverage",
    "runtime_record",
}
EXPECTED_SCALE_AXES = {
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
EXPECTED_AUTHORITY_STAGES = {
    "recognized",
    "defined",
    "assigned",
    "eligible",
    "admitted",
    "settled",
    "replayed",
}
EXPECTED_CUBE_FACES = {
    "recognition",
    "binding",
    "world_alignment",
    "admission",
    "complete_settlement",
    "narrator_transfer",
    "hud_visibility",
}
EXPECTED_COVERAGE_STATUSES = {
    "working",
    "partial",
    "blocked",
    "not_applicable",
    "unproven",
}
MEANING_FACET_SCHEMA = "capability-concept-meaning/1"
EXPECTED_MEANING_FACETS = {
    "semantic_role": {
        "capability_identity",
        "operation",
        "state",
        "resource",
        "grounding_basis",
        "identity",
        "world_rule",
        "mechanic_modifier",
    },
    "target_cardinality": {"not_applicable", "unspecified", "single", "multiple"},
    "spatial_extent": {"not_applicable", "unspecified", "entity", "area", "zone"},
    "world_scope": {
        "not_applicable",
        "unspecified",
        "personal",
        "local",
        "regional",
        "global",
        "cross_world",
    },
}
EXPECTED_ROLE_BY_KIND = {
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
NON_TARGET_ROLES = {
    "resource",
    "grounding_basis",
    "identity",
    "world_rule",
    "mechanic_modifier",
}
MEANING_FINGERPRINT_INCLUDES = [
    "concept_id",
    "concept_type",
    "categories",
    "definition",
    "meaning_facets",
]
MEANING_FINGERPRINT_EXCLUDES = ["label", "aliases", "source_ids", "support"]
EXPECTED_WORLD_EVENT_FIELDS = {
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
SENSITIVE_QUERY_KEYS = {
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

LOCAL_SOURCES = [
    {
        "id": "existing.enemy_capability_families",
        "kind": "local_corpus",
        "path": "../enemy-capabilities/capability-families.json",
        "label": "Preserved 18-family and 15-primitive capability corpus",
    },
    {
        "id": "existing.enemy_runtime_lexicon",
        "kind": "local_corpus",
        "path": "../enemy-capabilities/runtime-lexicon.json",
        "label": "Preserved enemy runtime recognition and negative-evidence lexicon",
    },
    {
        "id": "existing.registry_snapshot",
        "kind": "local_corpus",
        "path": "../enemy-capabilities/registry-snapshot.json",
        "label": "Preserved RPG skills, abilities, effects, items, and Phrasebook snapshot",
    },
    {
        "id": "existing.registry_runtime",
        "kind": "local_code",
        "path": "../../src/aetherstate/registry/__init__.py",
        "label": "Current frozen ability-mechanic registry and replay-safe normalizers",
    },
    {
        "id": "aetherstate.cross_genre_normalization",
        "kind": "project_research",
        "path": "research/2026-07-13-genre-gap-audit.md",
        "label": "AetherState 31-genre normalized gap audit",
    },
]


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _encoded(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _meaning_fingerprint(concept: dict) -> str:
    payload = {
        "schema": MEANING_FACET_SCHEMA,
        "concept_id": concept.get("id"),
        "concept_type": concept.get("concept_type"),
        "categories": sorted(set(concept.get("categories", []))),
        "definition": concept.get("definition"),
        "meaning_facets": concept.get("meaning_facets"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _validate_meaning_facets(concept: dict) -> None:
    concept_id = str(concept.get("id", ""))
    concept_type = str(concept.get("concept_type", ""))
    facets = concept.get("meaning_facets")
    if not isinstance(facets, dict) or set(facets) != set(EXPECTED_MEANING_FACETS):
        raise ValueError(f"{concept_id}.meaning facets are incomplete")
    for facet, expected in EXPECTED_MEANING_FACETS.items():
        if not isinstance(facets.get(facet), str) or facets[facet] not in expected:
            raise ValueError(f"{concept_id}.meaning facet {facet} is invalid")
    if facets["semantic_role"] != EXPECTED_ROLE_BY_KIND.get(concept_type):
        raise ValueError(f"{concept_id}.meaning facet semantic_role conflicts with concept kind")
    if facets["semantic_role"] in NON_TARGET_ROLES and (
        facets["target_cardinality"] != "not_applicable" or facets["spatial_extent"] != "not_applicable"
    ):
        raise ValueError(f"{concept_id}.meaning facets must mark target dimensions not_applicable")
    declared = concept.get("meaning_fingerprint")
    if (
        not isinstance(declared, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", declared)
        or declared != _meaning_fingerprint(concept)
    ):
        raise ValueError(f"{concept_id}.meaning fingerprint does not match canonical meaning")


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _source_id(url: str) -> str:
    return "web." + hashlib.sha256(url.encode("utf-8")).hexdigest()


def _validate_url(url: str, path: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid source URL in {path.name}: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError(f"credential-bearing source URL in {path.name}: {url!r}")
    sensitive = {
        key.casefold() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    } & SENSITIVE_QUERY_KEYS
    if sensitive:
        raise ValueError(f"sensitive signed query parameters in {path.name}: {sorted(sensitive)}")


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return list(value)


def _normalize_phrase(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("'", " ").replace("’", " ").replace("\u00e2\u20ac\u2122", " ")
    text = text.replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _expected_baseline(source_ids: set[str]) -> str:
    if source_ids & {"existing.registry_snapshot", "existing.registry_runtime"}:
        return "registry_corpus"
    if any(source_id.startswith("existing.") for source_id in source_ids):
        return "existing_corpus"
    return "gap_filled"


def _catalog_ids(
    value: object,
    *,
    label: str,
    expected: set[str],
    fields: set[str],
    list_fields: set[str] | None = None,
) -> set[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    list_fields = list_fields or set()
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"{label} rows have invalid fields")
        row_id = str(row.get("id", "")).strip()
        if not row_id or row_id in seen:
            raise ValueError(f"{label} has a missing or duplicate id")
        for field in fields - {"id"} - list_fields:
            if not isinstance(row.get(field), str) or not str(row[field]).strip():
                raise ValueError(f"{label}.{row_id}.{field} must be a non-empty string")
        for field in list_fields:
            values = _string_list(row.get(field), f"{label}.{row_id}.{field}")
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{label}.{row_id}.{field} must be non-empty and unique")
        seen.add(row_id)
    if seen != expected:
        raise ValueError(
            f"{label} mismatch; missing={sorted(expected - seen)}, extra={sorted(seen - expected)}"
        )
    return seen


def _preflight_taxonomy(categories: dict, concepts: dict, taxonomy: dict) -> None:
    if categories.get("schema") != "aetherstate-glossary-categories/2":
        raise ValueError("unsupported category catalog schema")
    if concepts.get("schema") != "aetherstate-glossary-concepts/2":
        raise ValueError("unsupported concept catalog schema")
    if taxonomy.get("schema") != TAXONOMY_SCHEMA:
        raise ValueError("unsupported Semantic Atlas taxonomy schema")
    expected_taxonomy_fields = {
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
    if set(taxonomy) != expected_taxonomy_fields:
        raise ValueError("Semantic Atlas taxonomy fields are incomplete")
    if taxonomy.get("name") != "Semantic Atlas" or taxonomy.get("authority") != "classification_only":
        raise ValueError("Semantic Atlas must retain classification_only authority")

    _catalog_ids(
        taxonomy.get("classification_planes"),
        label="classification planes",
        expected=EXPECTED_CLASSIFICATION_PLANES,
        fields={"id", "name", "stability", "owns", "does_not_own"},
        list_fields={"owns", "does_not_own"},
    )
    concept_kind_ids = _catalog_ids(
        taxonomy.get("concept_kinds"),
        label="concept kinds",
        expected=EXPECTED_CONCEPT_KINDS,
        fields={"id", "label", "description", "example", "counterexample"},
    )
    _catalog_ids(
        taxonomy.get("scale_axes"),
        label="scale axes",
        expected=EXPECTED_SCALE_AXES,
        fields={"id", "label", "description"},
    )
    _catalog_ids(
        taxonomy.get("authority_stages"),
        label="authority stages",
        expected=EXPECTED_AUTHORITY_STAGES,
        fields={"id", "label", "description"},
    )
    _catalog_ids(
        taxonomy.get("cube_faces"),
        label="Semantic Cube faces",
        expected=EXPECTED_CUBE_FACES,
        fields={"id", "label", "description"},
    )
    statuses = _string_list(taxonomy.get("coverage_statuses"), "coverage_statuses")
    if len(statuses) != len(set(statuses)) or set(statuses) != EXPECTED_COVERAGE_STATUSES:
        raise ValueError("coverage statuses do not match the closed catalog")

    facet_contract = taxonomy.get("meaning_facet_contract")
    if not isinstance(facet_contract, dict) or set(facet_contract) != {
        "schema",
        "closed_values",
        "fingerprint_includes",
        "fingerprint_excludes",
    }:
        raise ValueError("meaning facet contract fields are invalid")
    if facet_contract.get("schema") != MEANING_FACET_SCHEMA:
        raise ValueError("unsupported meaning facet contract schema")
    closed_values = facet_contract.get("closed_values")
    if not isinstance(closed_values, dict) or set(closed_values) != set(EXPECTED_MEANING_FACETS):
        raise ValueError("meaning facet closed values are incomplete")
    for facet, expected in EXPECTED_MEANING_FACETS.items():
        actual = _string_list(closed_values.get(facet), f"meaning facet {facet}")
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError(f"meaning facet {facet} does not match the closed contract")
    if facet_contract.get("fingerprint_includes") != MEANING_FINGERPRINT_INCLUDES:
        raise ValueError("meaning fingerprint includes do not match the closed contract")
    if facet_contract.get("fingerprint_excludes") != MEANING_FINGERPRINT_EXCLUDES:
        raise ValueError("meaning fingerprint excludes do not match the closed contract")

    event = taxonomy.get("world_event_record")
    if not isinstance(event, dict) or set(event) != {
        "name",
        "collection",
        "authority",
        "required_fields",
    }:
        raise ValueError("world_event_record fields are invalid")
    if (
        event.get("name") != "World Event Record"
        or event.get("collection") != "World Overlay Stack"
        or event.get("authority") != "ledger_owned_after_admitted_settlement"
    ):
        raise ValueError("world_event_record names or authority are invalid")
    event_fields = _string_list(event.get("required_fields"), "world_event_record.required_fields")
    if len(event_fields) != len(set(event_fields)) or set(event_fields) != EXPECTED_WORLD_EVENT_FIELDS:
        raise ValueError("world_event_record required fields do not match the closed contract")

    category_rows = categories.get("categories")
    if not isinstance(category_rows, list):
        raise ValueError("category catalog must be a list")
    category_ids: set[str] = set()
    category_fields = {
        "id",
        "label",
        "description",
        "includes",
        "excludes",
        "examples",
        "counterexamples",
    }
    for row in category_rows:
        if not isinstance(row, dict) or set(row) != category_fields:
            raise ValueError("category rows require includes, excludes, examples, and counterexamples")
        category_id = str(row.get("id", "")).strip()
        if not category_id or category_id in category_ids:
            raise ValueError("category catalog has a missing or duplicate id")
        for field in ("includes", "excludes", "examples", "counterexamples"):
            values = _string_list(row.get(field), f"{category_id}.{field}")
            if not values or len(values) != len(set(values)):
                raise ValueError(f"{category_id}.{field} must be non-empty and unique")
        category_ids.add(category_id)
    if category_ids != EXPECTED_CATEGORIES:
        raise ValueError("category catalog mismatch")

    concept_rows = concepts.get("concepts")
    if not isinstance(concept_rows, list) or not concept_rows:
        raise ValueError("concept catalog must be a non-empty list")
    used_kinds = {str(row.get("concept_type", "")) for row in concept_rows if isinstance(row, dict)}
    unknown = used_kinds - concept_kind_ids
    if unknown:
        raise ValueError(f"concept catalog references unknown concept kind: {sorted(unknown)}")
    if used_kinds != EXPECTED_CONCEPT_KINDS:
        raise ValueError("concept catalog does not exercise the complete concept kind catalog")
    expected_concept_fields = {
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
    for row in concept_rows:
        if not isinstance(row, dict) or set(row) != expected_concept_fields:
            raise ValueError("concept rows have invalid fields")
        categories = _string_list(row.get("categories"), f"{row.get('id')}.categories")
        if not categories or len(categories) != len(set(categories)):
            raise ValueError(f"{row.get('id')}.categories must be non-empty and unique")
        _validate_meaning_facets(row)


def _preflight_clusters(
    genre_files: list[Path],
    categories: dict,
    concepts: dict,
) -> list[tuple[Path, dict]]:
    if categories.get("schema") != "aetherstate-glossary-categories/2":
        raise ValueError("unsupported category catalog schema")
    if concepts.get("schema") != "aetherstate-glossary-concepts/2":
        raise ValueError("unsupported concept catalog schema")
    category_rows = categories.get("categories", [])
    category_ids = set()
    for row in category_rows:
        if not isinstance(row, dict):
            raise ValueError("category rows must be objects")
        category_id = str(row.get("id", "")).strip()
        if (
            not category_id
            or not str(row.get("label", "")).strip()
            or not str(row.get("description", "")).strip()
        ):
            raise ValueError("every category requires id, label, and description")
        category_ids.add(category_id)
    if len(category_rows) != 12 or category_ids != EXPECTED_CATEGORIES:
        raise ValueError(
            "category catalog mismatch; "
            f"missing={sorted(EXPECTED_CATEGORIES - category_ids)}, "
            f"extra={sorted(category_ids - EXPECTED_CATEGORIES)}"
        )
    concept_rows = concepts.get("concepts", [])
    concept_by_id = {
        str(row.get("id")): row
        for row in concept_rows
        if isinstance(row, dict) and str(row.get("id", "")).strip()
    }
    if not concept_rows or len(concept_by_id) != len(concept_rows):
        raise ValueError("concept catalog contains a missing or duplicate id")
    local_source_ids = {row["id"] for row in LOCAL_SOURCES}
    for concept_id, concept in concept_by_id.items():
        if not str(concept.get("label", "")).strip() or not str(concept.get("concept_type", "")).strip():
            raise ValueError(f"{concept_id} requires a label and concept_type")
        _string_list(concept.get("aliases"), f"{concept_id}.aliases")
        concept_categories = _string_list(concept.get("categories"), f"{concept_id}.categories")
        if not concept_categories or not set(concept_categories) <= category_ids:
            raise ValueError(f"{concept_id} references invalid categories")
        source_ids = _string_list(concept.get("source_ids"), f"{concept_id}.source_ids")
        if not source_ids or not set(source_ids) <= local_source_ids:
            raise ValueError(f"{concept_id} references invalid local provenance")
        support = concept.get("support")
        if not isinstance(support, dict):
            raise ValueError(f"{concept_id}.support must be an object")
        if (
            support.get("recognition") != "canonical"
            or support.get("authorization") != "frozen_definition_required"
        ):
            raise ValueError(f"{concept_id} has unsupported admission metadata")
        if support.get("narration") not in {"meaning", "boundary"}:
            raise ValueError(f"{concept_id} has unsupported narration metadata")
        receipts = support.get("receipt_ids")
        if not isinstance(receipts, list) or any(
            not isinstance(item, str) or not item.strip() for item in receipts
        ):
            raise ValueError(f"{concept_id}.support.receipt_ids must be strings")
        if receipts:
            raise ValueError(f"{concept_id} advertises a receipt without a compiler-v1 adapter")

    seen_genres: set[str] = set()
    seen_clusters: set[str] = set()
    prepared: list[tuple[Path, dict]] = []
    for path in genre_files:
        doc = _read(path)
        if doc.get("schema") != GENRE_SCHEMA:
            raise ValueError(f"unsupported cluster schema: {path}")
        cluster = str(doc.get("cluster", "")).strip()
        if not cluster or cluster in seen_clusters:
            raise ValueError(f"missing or duplicate cluster id in {path.name}: {cluster!r}")
        seen_clusters.add(cluster)
        requests = doc.get("concept_requests") or []
        if requests:
            ids = [str(row.get("id")) for row in requests if isinstance(row, dict)]
            raise ValueError(f"unresolved concept requests in {path.name}: {ids}")
        genres = doc.get("genres")
        if not isinstance(genres, list) or not genres:
            raise ValueError(f"cluster contains no genres: {path.name}")
        for genre in genres:
            if not isinstance(genre, dict):
                raise ValueError(f"genre rows must be objects: {path.name}")
            genre_id = str(genre.get("id", "")).strip()
            if not genre_id or genre_id in seen_genres:
                raise ValueError(f"missing or duplicate genre id: {genre_id!r}")
            seen_genres.add(genre_id)
            if not str(genre.get("label", "")).strip():
                raise ValueError(f"{genre_id} requires a label")
            for field in ("aliases", "facets", "false_friends", "source_urls"):
                if not _string_list(genre.get(field), f"{genre_id}.{field}"):
                    raise ValueError(f"{genre_id}.{field} cannot be empty")
            category_rows = genre.get("categories")
            if not isinstance(category_rows, dict) or set(category_rows) != category_ids:
                raise ValueError(f"{genre_id} does not cover the exact category catalog")
            phrase_targets: dict[str, str] = {}
            for category_id, entries in category_rows.items():
                if not isinstance(entries, list) or not entries:
                    raise ValueError(f"{genre_id}.{category_id} must contain entries")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError(f"{genre_id}.{category_id} entry must be an object")
                    concept_id = str(entry.get("concept_id", ""))
                    concept = concept_by_id.get(concept_id)
                    if concept is None:
                        raise ValueError(f"{genre_id} references unknown concept {concept_id}")
                    if category_id not in concept.get("categories", []):
                        raise ValueError(f"{genre_id}.{category_id} uses {concept_id} outside its categories")
                    baseline = entry.get("baseline")
                    if baseline not in {
                        "existing_corpus",
                        "registry_corpus",
                        "gap_filled",
                    }:
                        raise ValueError(f"invalid baseline for {genre_id}.{concept_id}")
                    expected_baseline = _expected_baseline(set(concept.get("source_ids", [])))
                    if baseline != expected_baseline:
                        raise ValueError(
                            f"baseline/provenance mismatch for {genre_id}.{concept_id}: "
                            f"declared {baseline}, expected {expected_baseline}"
                        )
                    if entry.get("priority") not in {"critical", "high", "medium"}:
                        raise ValueError(f"invalid priority for {genre_id}.{concept_id}")
                    terms = _string_list(entry.get("terms"), f"{genre_id}.{concept_id}.terms")
                    if not terms:
                        raise ValueError(f"{genre_id}.{concept_id}.terms cannot be empty")
                    for term in terms:
                        phrase = _normalize_phrase(term)
                        if not phrase:
                            raise ValueError(f"{genre_id}.{concept_id} contains an empty normalized term")
                        previous = phrase_targets.get(phrase)
                        if previous is not None and previous != concept_id:
                            raise ValueError(
                                f"same-genre phrase collision in {genre_id}: "
                                f"{term!r} maps to {previous} and {concept_id}"
                            )
                        phrase_targets[phrase] = concept_id
            for url in genre["source_urls"]:
                _validate_url(url, path)
        prepared.append((path, doc))
    if seen_genres != EXPECTED_GENRES:
        missing = sorted(EXPECTED_GENRES - seen_genres)
        extra = sorted(seen_genres - EXPECTED_GENRES)
        raise ValueError(f"genre catalog mismatch; missing={missing}, extra={extra}")
    return prepared


def finalize(root: Path) -> None:
    corpus = root / "corpus" / "capability-glossary"
    genre_files = sorted((corpus / "genres").glob("*.json"))
    if len(genre_files) != 3:
        raise ValueError(f"expected three genre cluster files, found {len(genre_files)}")

    categories = _read(corpus / "categories.json")
    concepts = _read(corpus / "concepts.json")
    taxonomy = _read(corpus / "taxonomy.json")
    _preflight_taxonomy(categories, concepts, taxonomy)
    prepared = _preflight_clusters(genre_files, categories, concepts)

    web: dict[str, dict] = {}
    genre_count = 0
    generated: dict[Path, bytes] = {}
    for path, doc in prepared:
        for genre in doc["genres"]:
            genre_count += 1
            urls = sorted(set(genre["source_urls"]))
            source_ids = []
            for url in urls:
                source_id = _source_id(url)
                source_ids.append(source_id)
                existing = web.get(source_id)
                if existing is not None and existing["url"] != url:
                    raise ValueError(f"web source id collision: {source_id}")
                parsed = urlparse(url)
                row = web.setdefault(
                    source_id,
                    {
                        "id": source_id,
                        "kind": "web_research",
                        "url": url,
                        "label": parsed.netloc + parsed.path,
                        "genre_ids": [],
                        "reuse": "research_and_normalization_only",
                    },
                )
                if genre["id"] not in row["genre_ids"]:
                    row["genre_ids"].append(genre["id"])
            genre["source_ids"] = source_ids
        generated[path.relative_to(corpus)] = _encoded(doc)

    for row in web.values():
        row["genre_ids"].sort()
    sources = {
        "schema": "aetherstate-glossary-sources/1",
        "sources": [*LOCAL_SOURCES, *(web[key] for key in sorted(web))],
    }
    generated[Path("sources.json")] = _encoded(sources)

    static_artifacts = [
        Path("README.md"),
        Path("categories.json"),
        Path("concepts.json"),
        Path("research/2026-07-13-genre-gap-audit.md"),
        Path("taxonomy.json"),
    ]
    artifacts: dict[str, dict] = {}
    for rel in [*static_artifacts, *sorted(generated)]:
        payload = generated.get(rel)
        if payload is None:
            payload = (corpus / rel).read_bytes()
        artifacts[rel.as_posix()] = {
            "bytes": len(payload),
            "fingerprint": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }

    manifest = {
        "schema": SCHEMA,
        "categories_file": "categories.json",
        "concepts_file": "concepts.json",
        "sources_file": "sources.json",
        "taxonomy_file": "taxonomy.json",
        "genre_files": [path.relative_to(corpus).as_posix() for path in genre_files],
        "counts": {
            "categories": len(categories.get("categories", [])),
            "classification_planes": len(taxonomy.get("classification_planes", [])),
            "concepts": len(concepts.get("concepts", [])),
            "concept_kinds": len(taxonomy.get("concept_kinds", [])),
            "cube_faces": len(taxonomy.get("cube_faces", [])),
            "genres": genre_count,
            "scale_axes": len(taxonomy.get("scale_axes", [])),
            "sources": len(sources["sources"]),
        },
        "authority_boundary": {
            "recognized": "A glossary match is candidate meaning only.",
            "authorized": "A world or actor must own an immutable frozen definition.",
            "executable": "Only an admitted receipt may commit a consequence.",
        },
        "artifacts": artifacts,
    }

    # All validation and byte construction has completed.  Replace generated artifacts first and
    # publish the manifest seal last, so readers never observe a new seal over old bytes.
    for rel in sorted(generated):
        _write_atomic(corpus / rel, generated[rel])
    _write_atomic(corpus / "manifest.json", _encoded(manifest))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    finalize(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
