"""Verified, Lex-qualified catalog view over AetherState's Semantic Atlas.

The Atlas is classification and discovery only.  It combines sealed CapabilityLex meanings with
the ReferentLex, SceneLex, and ActionLex concepts owned by Semantic Fabric without granting world
identity, assignment, authorization, execution, settlement, or ledger authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .capability_glossary import (
    CapabilityGlossary,
    GlossaryError,
    concept_meaning_fingerprint,
    content_fingerprint,
    normalize_phrase,
)
from .semantic_fabric import (
    LEX_IDS,
    PACK_LEX_IDS,
    LexiconEntry,
    SemanticFabric,
    SemanticFabricError,
    load_default_semantic_fabric,
    semantic_entry_meaning_fingerprint,
)


SEARCH_SCHEMA = "semantic-atlas-search/1"
CURSOR_SCHEMA = "semantic-atlas-cursor/1"
MAX_PAGE_SIZE = 100
MAX_CURSOR_LENGTH = 4096

_LEX_ORDER = {lex_id: index for index, lex_id in enumerate(LEX_IDS)}


class SemanticAtlasError(ValueError):
    """A Semantic Atlas lookup, filter, or cursor violates the bounded catalog contract."""


def _deep_plain(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SemanticAtlasError("Semantic Atlas snapshot keys must be strings")
        return {key: _deep_plain(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_plain(nested) for nested in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SemanticAtlasError("Semantic Atlas snapshot contains unsupported data")


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SemanticAtlasError("Semantic Atlas snapshot keys must be strings")
        return MappingProxyType({key: _deep_freeze(nested) for key, nested in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(nested) for nested in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SemanticAtlasError("Semantic Atlas snapshot contains unsupported data")


def _plain_identifier(value: object) -> str:
    return " ".join(str(value).replace("_", " ").replace(".", " ").split())


def _plain_semantic_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    if isinstance(value, str):
        return _plain_identifier(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_plain_semantic_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{_plain_identifier(key)}={_plain_semantic_value(value[key])}" for key in sorted(value)
            )
            + "}"
        )
    raise SemanticAtlasError("non-capability semantic descriptor contains unsupported data")


def _noncapability_definition(entry: Mapping[str, Any]) -> str:
    required = ", ".join(_plain_identifier(role) for role in entry["required_roles"]) or "none"
    optional = ", ".join(_plain_identifier(role) for role in entry["optional_roles"]) or "none"
    features = entry["features"]
    if not isinstance(features, Mapping):
        raise SemanticAtlasError("non-capability semantic features must be an object")
    features = (
        "; ".join(
            f"{_plain_identifier(key)}={_plain_semantic_value(features[key])}" for key in sorted(features)
        )
        or "none"
    )
    return (
        f"{_plain_identifier(entry['kind']).capitalize()} recognition. "
        f"Required roles: {required}. Optional roles: {optional}. "
        f"Completion: {_plain_identifier(entry['completion'])}. Semantic features: {features}."
    )


def _clone_row(row: Mapping[str, Any]) -> dict[str, Any]:
    plain = _deep_plain(row)
    if not isinstance(plain, dict):
        raise SemanticAtlasError("Semantic Atlas concept row snapshot is invalid")
    return plain


def _normalized_surfaces(values: Iterable[object]) -> tuple[str, ...]:
    surfaces: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = normalize_phrase(value)
        if normalized and normalized not in surfaces:
            surfaces.append(normalized)
    return tuple(surfaces)


def _capability_genre_terms(
    genres: Mapping[str, Any],
    concept_ids: Iterable[str],
) -> dict[str, list[str]]:
    terms: dict[str, list[str]] = {concept_id: [] for concept_id in concept_ids}
    for genre_id in sorted(genres):
        genre = genres[genre_id]
        if not isinstance(genre, Mapping):
            raise SemanticAtlasError(f"CapabilityLex genre is malformed: {genre_id}")
        categories = genre.get("categories")
        if not isinstance(categories, Mapping):
            raise SemanticAtlasError(f"CapabilityLex genre categories are malformed: {genre_id}")
        for category_id in sorted(categories):
            rows = categories[category_id]
            if not isinstance(rows, (list, tuple)):
                raise SemanticAtlasError(
                    f"CapabilityLex genre category rows are malformed: {genre_id}.{category_id}"
                )
            for row in rows:
                if not isinstance(row, Mapping) or row.get("concept_id") not in terms:
                    raise SemanticAtlasError(
                        f"CapabilityLex genre term references an unknown concept: {genre_id}"
                    )
                surfaces = row.get("terms")
                if not isinstance(surfaces, (list, tuple)) or any(
                    not isinstance(item, str) for item in surfaces
                ):
                    raise SemanticAtlasError(
                        f"CapabilityLex genre terms are malformed: {genre_id}.{category_id}"
                    )
                terms[str(row["concept_id"])].extend(surfaces)
    return terms


def _validated_capability_snapshots(
    glossary: CapabilityGlossary,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    try:
        verified = CapabilityGlossary.load(glossary.root)
        provided_concepts = _deep_plain(glossary.concepts)
        provided_genres = _deep_plain(glossary.genres)
        verified_genres = _deep_plain(verified.genres)
    except (GlossaryError, OSError) as exc:
        raise SemanticAtlasError("CapabilityLex catalog could not be revalidated") from exc
    if not isinstance(provided_concepts, dict) or not isinstance(provided_genres, dict):
        raise SemanticAtlasError("CapabilityLex catalog snapshots are malformed")
    if not isinstance(verified_genres, dict):
        raise SemanticAtlasError("verified CapabilityLex genre snapshot is malformed")
    if set(provided_concepts) != set(verified.concepts):
        raise SemanticAtlasError("CapabilityLex full fingerprint changed after verification")

    concepts: dict[str, Any] = {}
    classifications: dict[str, Any] = {}
    for concept_id in sorted(provided_concepts):
        concept = provided_concepts[concept_id]
        if not isinstance(concept, Mapping):
            raise SemanticAtlasError(f"CapabilityLex concept snapshot is malformed: {concept_id}")
        try:
            classification = verified.concept_classification(concept_id)
            calculated_meaning = concept_meaning_fingerprint(concept)
            sealed_full, _source_ids = verified.concept_lineage(concept_id)
            calculated_full = content_fingerprint(concept)
        except GlossaryError as exc:
            raise SemanticAtlasError(f"CapabilityLex concept could not be revalidated: {concept_id}") from exc
        if (
            concept.get("meaning_fingerprint") != calculated_meaning
            or calculated_meaning != classification["meaning_fingerprint"]
        ):
            raise SemanticAtlasError(
                f"CapabilityLex meaning fingerprint changed after verification: {concept_id}"
            )
        if calculated_full != sealed_full:
            raise SemanticAtlasError(
                f"CapabilityLex full fingerprint changed after verification: {concept_id}"
            )
        concepts[concept_id] = _deep_freeze(concept)
        classifications[concept_id] = _deep_freeze(classification)

    try:
        provided_genre_fingerprint = content_fingerprint(provided_genres)
        verified_genre_fingerprint = content_fingerprint(verified_genres)
    except GlossaryError as exc:
        raise SemanticAtlasError("CapabilityLex genre catalog could not be revalidated") from exc
    if provided_genre_fingerprint != verified_genre_fingerprint:
        raise SemanticAtlasError("CapabilityLex genre catalog fingerprint changed after verification")
    return (
        MappingProxyType(concepts),
        _deep_freeze(provided_genres),
        MappingProxyType(classifications),
    )


def _validated_entry_snapshot(entry: LexiconEntry) -> Mapping[str, Any]:
    lex_label = f"{entry.lex_id.capitalize()}Lex"
    try:
        snapshot = _deep_plain(entry.as_dict())
    except (TypeError, ValueError, SemanticAtlasError) as exc:
        raise SemanticAtlasError(f"{lex_label} entry snapshot is malformed: {entry.concept_id}") from exc
    if not isinstance(snapshot, dict):
        raise SemanticAtlasError(f"{lex_label} entry snapshot is malformed: {entry.concept_id}")
    try:
        calculated_meaning = semantic_entry_meaning_fingerprint(entry.lex_id, snapshot)
        full_payload = {key: snapshot[key] for key in snapshot if key not in {"lex_id", "fingerprint"}}
        calculated_full = content_fingerprint(full_payload)
    except (GlossaryError, SemanticFabricError) as exc:
        raise SemanticAtlasError(f"{lex_label} entry could not be revalidated: {entry.concept_id}") from exc
    if snapshot.get("meaning_fingerprint") != calculated_meaning:
        raise SemanticAtlasError(
            f"{lex_label} meaning fingerprint changed after verification: {entry.concept_id}"
        )
    if snapshot.get("fingerprint") != calculated_full:
        raise SemanticAtlasError(
            f"{lex_label} full fingerprint changed after verification: {entry.concept_id}"
        )
    frozen = _deep_freeze(snapshot)
    if not isinstance(frozen, Mapping):
        raise SemanticAtlasError(f"{lex_label} entry snapshot is malformed: {entry.concept_id}")
    return frozen


def _entry_catalog_snapshot(
    fabric: SemanticFabric,
    lex_id: str,
) -> Mapping[str, Mapping[str, Any]]:
    catalog: dict[str, Mapping[str, Any]] = {}
    for entry in fabric.entries_for(lex_id):
        snapshot = _validated_entry_snapshot(entry)
        concept_id = str(snapshot["concept_id"])
        if concept_id in catalog:
            raise SemanticAtlasError(
                f"duplicate {lex_id.capitalize()}Lex Semantic Atlas meaning: {concept_id}"
            )
        catalog[concept_id] = snapshot
    return MappingProxyType(catalog)


def _validated_noncapability_snapshots(
    fabric: SemanticFabric,
    capability_glossary: CapabilityGlossary,
) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    try:
        verified = SemanticFabric.load(
            fabric.root,
            capability_glossary=capability_glossary,
        )
    except (SemanticFabricError, OSError) as exc:
        raise SemanticAtlasError("Semantic Fabric catalog could not be revalidated") from exc
    if (
        fabric.fingerprint != verified.fingerprint
        or fabric.family_version != verified.family_version
        or fabric.capability_manifest_fingerprint != verified.capability_manifest_fingerprint
        or fabric.genre_ids != verified.genre_ids
    ):
        raise SemanticAtlasError("Semantic Fabric instance does not match sealed Semantic Fabric")

    catalogs: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for lex_id in PACK_LEX_IDS:
        provided = _entry_catalog_snapshot(fabric, lex_id)
        sealed = _entry_catalog_snapshot(verified, lex_id)
        if provided != sealed:
            raise SemanticAtlasError(
                f"{lex_id.capitalize()}Lex catalog does not match sealed Semantic Fabric"
            )
        catalogs[lex_id] = tuple(sealed[concept_id] for concept_id in sorted(sealed))
    return MappingProxyType(catalogs)


def _canonical_cursor_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class SemanticAtlas:
    """Deterministic catalog over exact ``(lex_id, concept_id)`` meanings."""

    def __init__(
        self,
        capability_glossary: CapabilityGlossary,
        semantic_fabric: SemanticFabric,
    ) -> None:
        if not isinstance(capability_glossary, CapabilityGlossary):
            raise SemanticAtlasError("Semantic Atlas requires a verified CapabilityGlossary")
        if not isinstance(semantic_fabric, SemanticFabric):
            raise SemanticAtlasError("Semantic Atlas requires a verified SemanticFabric")
        try:
            capability_manifest = (capability_glossary.root / "manifest.json").read_bytes()
        except OSError as exc:
            raise SemanticAtlasError("CapabilityLex manifest is unavailable for Atlas verification") from exc
        capability_fingerprint = "sha256:" + hashlib.sha256(capability_manifest).hexdigest()
        if capability_fingerprint != semantic_fabric.capability_manifest_fingerprint:
            raise SemanticAtlasError("CapabilityLex and Semantic Fabric Atlas bindings do not match")

        self.capability_glossary = capability_glossary
        self.semantic_fabric = semantic_fabric
        rows: dict[tuple[str, str], Mapping[str, Any]] = {}
        search_surfaces: dict[tuple[str, str], tuple[str, ...]] = {}

        capability_concepts, capability_genres, classifications = _validated_capability_snapshots(
            capability_glossary
        )
        noncapability_catalogs = _validated_noncapability_snapshots(
            semantic_fabric,
            capability_glossary,
        )
        capability_genre_terms = _capability_genre_terms(
            capability_genres,
            capability_concepts,
        )
        for concept_id in sorted(capability_concepts):
            concept = capability_concepts[concept_id]
            classification = classifications[concept_id]
            if not isinstance(concept, Mapping) or not isinstance(classification, Mapping):
                raise SemanticAtlasError(f"CapabilityLex snapshot is malformed: {concept_id}")
            identity = ("capability", concept_id)
            row = _deep_freeze(
                {
                    "lex_id": "capability",
                    "concept_id": concept_id,
                    "label": str(concept["label"]),
                    "definition": str(concept["definition"]),
                    "concept_kind": str(classification["concept_kind"]),
                    "domain_shelves": [str(item) for item in classification["domain_shelves"]],
                    "meaning_fingerprint": str(classification["meaning_fingerprint"]),
                }
            )
            if not isinstance(row, Mapping):
                raise SemanticAtlasError(f"CapabilityLex row snapshot is malformed: {concept_id}")
            rows[identity] = row
            aliases = concept.get("aliases")
            if not isinstance(aliases, (list, tuple)):
                raise SemanticAtlasError(f"CapabilityLex aliases are malformed: {concept_id}")
            search_surfaces[identity] = _normalized_surfaces(
                (
                    concept_id,
                    concept["label"],
                    concept["definition"],
                    *aliases,
                    *capability_genre_terms[concept_id],
                )
            )

        for lex_id in PACK_LEX_IDS:
            for entry_snapshot in noncapability_catalogs[lex_id]:
                concept_id = str(entry_snapshot["concept_id"])
                identity = (lex_id, concept_id)
                if identity in rows:
                    raise SemanticAtlasError(
                        f"duplicate Lex-qualified Semantic Atlas meaning: {lex_id}/{concept_id}"
                    )
                definition = _noncapability_definition(entry_snapshot)
                row = _deep_freeze(
                    {
                        "lex_id": lex_id,
                        "concept_id": concept_id,
                        "label": str(entry_snapshot["label"]),
                        "definition": definition,
                        "concept_kind": str(entry_snapshot["kind"]),
                        "domain_shelves": [],
                        "meaning_fingerprint": str(entry_snapshot["meaning_fingerprint"]),
                    }
                )
                if not isinstance(row, Mapping):
                    raise SemanticAtlasError(
                        f"{lex_id.capitalize()}Lex row snapshot is malformed: {concept_id}"
                    )
                rows[identity] = row
                genre_terms = entry_snapshot["genre_terms"]
                if not isinstance(genre_terms, (list, tuple)):
                    raise SemanticAtlasError(
                        f"{lex_id.capitalize()}Lex genre terms are malformed: {concept_id}"
                    )
                search_surfaces[identity] = _normalized_surfaces(
                    (
                        concept_id,
                        entry_snapshot["label"],
                        definition,
                        *entry_snapshot["terms"],
                        *(str(surface["term"]) for surface in genre_terms),
                    )
                )

        if set(rows) != set(search_surfaces):
            raise SemanticAtlasError("Semantic Atlas search index does not match its concept catalog")
        catalog_payload = [
            {
                "row": _clone_row(rows[identity]),
                "search_surfaces": list(search_surfaces[identity]),
            }
            for identity in sorted(
                rows,
                key=lambda item: (_LEX_ORDER[item[0]], item[1]),
            )
        ]
        self.fingerprint = content_fingerprint(catalog_payload)
        self._rows: Mapping[tuple[str, str], Mapping[str, Any]] = MappingProxyType(rows)
        self._search_surfaces: Mapping[tuple[str, str], tuple[str, ...]] = MappingProxyType(search_surfaces)

    @classmethod
    def from_fabric(cls, fabric: SemanticFabric) -> "SemanticAtlas":
        if not isinstance(fabric, SemanticFabric):
            raise SemanticAtlasError("Semantic Atlas requires a verified SemanticFabric")
        return cls(fabric.capability_glossary, fabric)

    def meaning(self, lex_id: str, concept_id: str) -> dict[str, Any]:
        """Return one exact Lex-qualified classification meaning."""
        if lex_id not in LEX_IDS:
            raise SemanticAtlasError(f"unknown Semantic Atlas Lex id: {lex_id!r}")
        if not isinstance(concept_id, str) or not concept_id:
            raise SemanticAtlasError("Semantic Atlas concept id must be a non-empty string")
        try:
            return _clone_row(self._rows[(lex_id, concept_id)])
        except KeyError as exc:
            raise SemanticAtlasError(f"unknown Semantic Atlas meaning: {lex_id}/{concept_id}") from exc

    def search(
        self,
        query: str = "",
        lex_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search deterministic catalog surfaces and return one bounded cursor page."""
        if not isinstance(query, str):
            raise SemanticAtlasError("Semantic Atlas search query must be a string")
        if lex_id is not None and lex_id not in LEX_IDS:
            raise SemanticAtlasError(f"unknown Semantic Atlas Lex id: {lex_id!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise SemanticAtlasError(f"Semantic Atlas search limit must be from 1 to {MAX_PAGE_SIZE}")
        normalized_query = normalize_phrase(query)
        if query.strip() and not normalized_query:
            if cursor is not None:
                raise SemanticAtlasError("Semantic Atlas cursor does not match an unmatchable nonempty query")
            return {
                "schema": SEARCH_SCHEMA,
                "concepts": [],
                "next_cursor": None,
            }
        offset = 0
        if cursor is not None:
            offset = self._decode_cursor(cursor, query=normalized_query, lex_id=lex_id)

        identities = [
            identity
            for identity, surfaces in self._search_surfaces.items()
            if (lex_id is None or identity[0] == lex_id)
            and (not normalized_query or any(normalized_query in surface for surface in surfaces))
        ]
        identities.sort(key=lambda identity: self._search_key(identity, normalized_query))
        if cursor is not None and offset >= len(identities):
            raise SemanticAtlasError("Semantic Atlas cursor position is outside its result set")

        selected = identities[offset : offset + limit]
        next_offset = offset + len(selected)
        next_cursor = (
            self._encode_cursor(next_offset, query=normalized_query, lex_id=lex_id)
            if next_offset < len(identities)
            else None
        )
        return {
            "schema": SEARCH_SCHEMA,
            "concepts": [_clone_row(self._rows[identity]) for identity in selected],
            "next_cursor": next_cursor,
        }

    def _search_key(self, identity: tuple[str, str], query: str) -> tuple[int, int, str]:
        if not query:
            rank = 0
        else:
            row = self._rows[identity]
            concept_id = normalize_phrase(row["concept_id"])
            label = normalize_phrase(row["label"])
            surfaces = self._search_surfaces[identity]
            if query == concept_id:
                rank = 0
            elif query == label:
                rank = 1
            elif any(query == surface for surface in surfaces):
                rank = 2
            elif concept_id.startswith(query):
                rank = 3
            elif label.startswith(query):
                rank = 4
            elif any(surface.startswith(query) for surface in surfaces):
                rank = 5
            else:
                rank = 6
        return rank, _LEX_ORDER[identity[0]], identity[1]

    def _encode_cursor(self, offset: int, *, query: str, lex_id: str | None) -> str:
        payload = {
            "schema": CURSOR_SCHEMA,
            "atlas_fingerprint": self.fingerprint,
            "query": query,
            "lex_id": lex_id,
            "offset": offset,
        }
        document = {**payload, "fingerprint": content_fingerprint(payload)}
        return base64.urlsafe_b64encode(_canonical_cursor_bytes(document)).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: object, *, query: str, lex_id: str | None) -> int:
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise SemanticAtlasError("Semantic Atlas cursor is invalid")
        try:
            encoded = cursor.encode("ascii")
            padded = encoded + (b"=" * (-len(encoded) % 4))
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise SemanticAtlasError("Semantic Atlas cursor is invalid") from exc
        try:
            canonical_token = (
                base64.urlsafe_b64encode(_canonical_cursor_bytes(document)).decode("ascii").rstrip("=")
            )
        except (TypeError, ValueError) as exc:
            raise SemanticAtlasError("Semantic Atlas cursor is invalid") from exc
        if canonical_token != cursor:
            raise SemanticAtlasError("Semantic Atlas cursor is invalid")
        fields = {
            "schema",
            "atlas_fingerprint",
            "query",
            "lex_id",
            "offset",
            "fingerprint",
        }
        if (
            not isinstance(document, dict)
            or set(document) != fields
            or document.get("schema") != CURSOR_SCHEMA
        ):
            raise SemanticAtlasError("Semantic Atlas cursor fields are invalid")
        payload = {key: document[key] for key in document if key != "fingerprint"}
        try:
            expected_fingerprint = content_fingerprint(payload)
        except GlossaryError as exc:
            raise SemanticAtlasError("Semantic Atlas cursor fingerprint is invalid") from exc
        if document.get("fingerprint") != expected_fingerprint:
            raise SemanticAtlasError("Semantic Atlas cursor fingerprint is invalid")
        if document.get("atlas_fingerprint") != self.fingerprint:
            raise SemanticAtlasError("Semantic Atlas cursor belongs to a different catalog")
        if document.get("query") != query or document.get("lex_id") != lex_id:
            raise SemanticAtlasError("Semantic Atlas cursor does not match query or Lex filter")
        offset = document.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise SemanticAtlasError("Semantic Atlas cursor offset is invalid")
        return offset


def load_default_semantic_atlas(project_root: Path | str | None = None) -> SemanticAtlas:
    """Load the verified repo-local or installed Semantic Atlas catalog."""
    return SemanticAtlas.from_fabric(load_default_semantic_fabric(project_root))
