from __future__ import annotations

from pathlib import Path

import pytest

from aetherstate.capability_glossary import CapabilityGlossary, content_fingerprint
from aetherstate.semantic_atlas import (
    MAX_CURSOR_LENGTH,
    SEARCH_SCHEMA,
    SemanticAtlas,
    SemanticAtlasError,
    load_default_semantic_atlas,
)
from aetherstate.semantic_fabric import (
    LexiconEntry,
    SemanticFabric,
    semantic_entry_meaning_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_CORPUS = ROOT / "corpus" / "capability-glossary"
FABRIC_CORPUS = ROOT / "corpus" / "semantic-fabric"
ROW_FIELDS = {
    "lex_id",
    "concept_id",
    "label",
    "definition",
    "concept_kind",
    "domain_shelves",
    "meaning_fingerprint",
}


@pytest.fixture(scope="module")
def capability_glossary() -> CapabilityGlossary:
    return CapabilityGlossary.load(CAPABILITY_CORPUS)


@pytest.fixture(scope="module")
def fabric(capability_glossary: CapabilityGlossary) -> SemanticFabric:
    return SemanticFabric.load(FABRIC_CORPUS, capability_glossary=capability_glossary)


@pytest.fixture(scope="module")
def atlas(capability_glossary: CapabilityGlossary, fabric: SemanticFabric) -> SemanticAtlas:
    return SemanticAtlas(capability_glossary, fabric)


def _fresh_atlas_inputs() -> tuple[CapabilityGlossary, SemanticFabric]:
    glossary = CapabilityGlossary.load(CAPABILITY_CORPUS)
    semantic_fabric = SemanticFabric.load(FABRIC_CORPUS, capability_glossary=glossary)
    return glossary, semantic_fabric


def _reseal_forged_entry(entry: LexiconEntry) -> None:
    entry.features["atlas_forgery"] = entry.lex_id
    snapshot = entry.as_dict()
    meaning_fingerprint = semantic_entry_meaning_fingerprint(entry.lex_id, snapshot)
    object.__setattr__(entry, "meaning_fingerprint", meaning_fingerprint)
    snapshot["meaning_fingerprint"] = meaning_fingerprint
    full_payload = {key: snapshot[key] for key in snapshot if key not in {"lex_id", "fingerprint"}}
    object.__setattr__(entry, "fingerprint", content_fingerprint(full_payload))


def test_catalog_pages_every_lex_qualified_concept_without_clipping(atlas: SemanticAtlas) -> None:
    concepts: list[dict[str, object]] = []
    cursor = None
    while True:
        page = atlas.search(limit=100, cursor=cursor)
        assert set(page) == {"schema", "concepts", "next_cursor"}
        assert page["schema"] == SEARCH_SCHEMA
        assert len(page["concepts"]) <= 100
        assert all(set(row) == ROW_FIELDS for row in page["concepts"])
        concepts.extend(page["concepts"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    identities = [(row["lex_id"], row["concept_id"]) for row in concepts]
    assert len(concepts) == 311
    assert len(set(identities)) == 311
    assert {lex_id for lex_id, _concept_id in identities} == {
        "capability",
        "referent",
        "scene",
        "action",
    }


def test_exact_lookup_is_lex_qualified_for_duplicate_concept_ids(atlas: SemanticAtlas) -> None:
    capability = atlas.meaning("capability", "action.repair")
    action = atlas.meaning("action", "action.repair")

    assert capability["concept_id"] == action["concept_id"] == "action.repair"
    assert capability["lex_id"] == "capability"
    assert action["lex_id"] == "action"
    assert capability["definition"] == "Canonical recognition frame for repair."
    assert action["definition"]
    assert capability["meaning_fingerprint"] != action["meaning_fingerprint"]
    assert set(capability) == set(action) == ROW_FIELDS

    collisions = atlas.search("action.repair")["concepts"]
    assert {(row["lex_id"], row["concept_id"]) for row in collisions} == {
        ("capability", "action.repair"),
        ("action", "action.repair"),
    }


def test_search_covers_capability_aliases_and_cross_lex_terms(atlas: SemanticAtlas) -> None:
    capability_alias = atlas.search("restore machine", lex_id="capability")["concepts"]
    assert ("capability", "action.repair") in {(row["lex_id"], row["concept_id"]) for row in capability_alias}

    capability_genre_term = atlas.search("fireball blast", lex_id="capability")["concepts"]
    assert ("capability", "family.direct_pressure") in {
        (row["lex_id"], row["concept_id"]) for row in capability_genre_term
    }

    action_term = atlas.search("stabilized", lex_id="action")["concepts"]
    assert [(row["lex_id"], row["concept_id"]) for row in action_term] == [("action", "action.repair")]


def test_noncapability_definition_is_a_stable_plain_semantic_descriptor(
    atlas: SemanticAtlas,
) -> None:
    row = atlas.meaning("action", "action.repair")
    assert row["definition"] == atlas.meaning("action", "action.repair")["definition"]
    assert "change action" in row["definition"].casefold()
    assert "actor" in row["definition"]
    assert "patient" in row["definition"]
    assert row["domain_shelves"] == []


def test_valid_noncapability_catalog_is_detached_from_its_source_fabric() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    detached_atlas = SemanticAtlas(glossary, semantic_fabric)
    expected = detached_atlas.meaning("scene", "scene.cognition.belief")

    entry = semantic_fabric.entry("scene.cognition.belief")
    entry.features["post_construction_mutation"] = True
    object.__setattr__(entry, "label", "Mutated after Atlas construction")

    assert detached_atlas.meaning("scene", "scene.cognition.belief") == expected


def test_cursor_is_deterministic_validated_and_bound_to_query_and_filter(
    atlas: SemanticAtlas,
) -> None:
    first = atlas.search("action", lex_id="capability", limit=3)
    repeated = atlas.search("action", lex_id="capability", limit=3)
    assert first == repeated
    assert isinstance(first["next_cursor"], str)

    second = atlas.search(
        "action",
        lex_id="capability",
        limit=3,
        cursor=first["next_cursor"],
    )
    assert second["concepts"]
    assert second["concepts"] != first["concepts"]

    with pytest.raises(SemanticAtlasError, match="query or Lex filter"):
        atlas.search("spell", lex_id="capability", limit=3, cursor=first["next_cursor"])
    with pytest.raises(SemanticAtlasError, match="query or Lex filter"):
        atlas.search("action", lex_id="action", limit=3, cursor=first["next_cursor"])
    with pytest.raises(SemanticAtlasError, match="cursor"):
        atlas.search("action", lex_id="capability", limit=3, cursor=first["next_cursor"] + "x")
    assert len(first["next_cursor"]) <= MAX_CURSOR_LENGTH
    with pytest.raises(SemanticAtlasError, match="cursor"):
        atlas.search(cursor="x" * (MAX_CURSOR_LENGTH + 1))


def test_nonempty_unmatchable_query_never_becomes_an_empty_catalog_query(
    atlas: SemanticAtlas,
) -> None:
    for query in ("???", "修理"):
        assert atlas.search(query) == {
            "schema": SEARCH_SCHEMA,
            "concepts": [],
            "next_cursor": None,
        }

    empty_cursor = atlas.search(limit=3)["next_cursor"]
    assert isinstance(empty_cursor, str)
    with pytest.raises(SemanticAtlasError, match="cursor"):
        atlas.search("???", limit=3, cursor=empty_cursor)


def test_atlas_rejects_mutated_capability_meaning_before_projection() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    glossary.concepts["action.repair"]["definition"] = "Forged display meaning."

    with pytest.raises(SemanticAtlasError, match="CapabilityLex meaning fingerprint"):
        SemanticAtlas(glossary, semantic_fabric)


def test_atlas_rejects_mutated_capability_full_entry_before_projection() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    glossary.concepts["action.repair"]["aliases"].append("forged search alias")

    with pytest.raises(SemanticAtlasError, match="CapabilityLex full fingerprint"):
        SemanticAtlas(glossary, semantic_fabric)


def test_atlas_rejects_mutated_capability_genre_search_data_before_projection() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    genre = glossary.genres[sorted(glossary.genres)[0]]
    category = genre["categories"][sorted(genre["categories"])[0]]
    category[0]["terms"].append("forged genre term")

    with pytest.raises(SemanticAtlasError, match="CapabilityLex genre catalog fingerprint"):
        SemanticAtlas(glossary, semantic_fabric)


def test_atlas_rejects_mutated_noncapability_meaning_before_projection() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    semantic_fabric.entry("action.repair").features["action_class"] = "forged_semantics"

    with pytest.raises(SemanticAtlasError, match="ActionLex meaning fingerprint"):
        SemanticAtlas(glossary, semantic_fabric)


def test_atlas_rejects_mutated_noncapability_full_entry_before_projection() -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    object.__setattr__(semantic_fabric.entry("action.repair"), "label", "Forged display label")

    with pytest.raises(SemanticAtlasError, match="ActionLex full fingerprint"):
        SemanticAtlas(glossary, semantic_fabric)


@pytest.mark.parametrize(
    ("lex_id", "concept_id"),
    (
        ("referent", "referent.body_part.face"),
        ("scene", "scene.cognition.belief"),
        ("action", "action.communicate"),
    ),
)
def test_atlas_rejects_resealed_noncapability_entry_not_present_in_sealed_corpus(
    lex_id: str,
    concept_id: str,
) -> None:
    glossary, semantic_fabric = _fresh_atlas_inputs()
    original_fabric_fingerprint = semantic_fabric.fingerprint
    entry = semantic_fabric.entry(concept_id)
    _reseal_forged_entry(entry)

    assert entry.meaning_fingerprint == semantic_entry_meaning_fingerprint(
        lex_id,
        entry.as_dict(),
    )
    assert semantic_fabric.fingerprint == original_fabric_fingerprint
    with pytest.raises(SemanticAtlasError, match="sealed Semantic Fabric"):
        SemanticAtlas(glossary, semantic_fabric)


@pytest.mark.parametrize("limit", (0, -1, 101, True, 1.5))
def test_search_rejects_invalid_page_limits(atlas: SemanticAtlas, limit: object) -> None:
    with pytest.raises(SemanticAtlasError, match="limit"):
        atlas.search(limit=limit)  # type: ignore[arg-type]


def test_default_loader_reuses_the_verified_fabric_binding(atlas: SemanticAtlas) -> None:
    loaded = load_default_semantic_atlas(ROOT)
    assert loaded.meaning("action", "action.repair") == atlas.meaning("action", "action.repair")
    assert loaded.semantic_fabric.capability_glossary is loaded.capability_glossary
