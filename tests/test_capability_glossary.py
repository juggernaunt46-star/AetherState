from __future__ import annotations

import json
import hashlib
import shutil
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import pytest

from aetherstate.capability_glossary import (
    CAPABILITY_KINDS,
    CapabilityGlossary,
    GlossaryError,
    concept_meaning_fingerprint,
    content_fingerprint,
    load_default_glossary,
    normalize_phrase,
    raw_fingerprint,
)
from aetherstate.capability_glossary import _resolve_artifact
from tools.build_capability_glossary import build as build_capability_glossary
from tools.finalize_capability_glossary import _preflight_clusters, _preflight_taxonomy


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "capability-glossary"

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

EXPECTED_ROLE_COUNTS = {
    "capability_identity": 36,
    "operation": 74,
    "state": 69,
    "resource": 41,
    "grounding_basis": 29,
    "identity": 3,
    "world_rule": 6,
    "mechanic_modifier": 7,
}


def _valid_enemy_draft(**changes):
    draft = {
        "kind": "enemy_move",
        "name": "Measured Bolt",
        "definition_id": "measured_bolt",
        "world_id": "blackglass",
        "genre_ids": ["dark_fantasy_gothic"],
        "concept_ids": ["family.direct_pressure"],
        "grounding_evidence": ["functional crossbow"],
        "receipt_id": "enemy-opposition-hp/1",
        "receipt_concept_ids": ["family.direct_pressure"],
        "basis": "projectile",
        "delivery": "crossbow bolt",
        "semantic_primitive": "strike",
        "functional_family": "direct_pressure",
        "effect_channel": "hp",
        "target": "single_player",
        "range": "near",
        "area": "single_target",
        "timing": "committed",
        "cadence": "reliable",
        "interpretation_mode": "explicit_only",
    }
    draft.update(changes)
    return draft


@pytest.fixture(scope="module")
def glossary() -> CapabilityGlossary:
    return CapabilityGlossary.load(CORPUS)


def test_normalization_and_fingerprint_are_stable():
    assert normalize_phrase("  Wuxia—Qi's  Path & Oath ") == "wuxia qi s path and oath"
    left = {"b": [2, 3], "a": {"x": 1}}
    right = {"a": {"x": 1}, "b": [2, 3]}
    assert content_fingerprint(left) == content_fingerprint(right)


def test_glossary_has_every_requested_genre_and_category(glossary: CapabilityGlossary):
    assert set(glossary.genres) == EXPECTED_GENRES
    assert set(glossary.categories) == EXPECTED_CATEGORIES
    for genre_id in EXPECTED_GENRES:
        genre = glossary.genre_coverage(genre_id)
        assert set(genre["categories"]) == EXPECTED_CATEGORIES
        assert all(genre["categories"][category] for category in EXPECTED_CATEGORIES)
        baselines = {entry["baseline"] for entries in genre["categories"].values() for entry in entries}
        assert baselines & {"existing_corpus", "registry_corpus"}
        assert "gap_filled" in baselines


def test_semantic_atlas_defines_complete_non_authoritative_classification_planes(
    glossary: CapabilityGlossary,
):
    taxonomy = glossary.taxonomy
    assert taxonomy["schema"] == "aetherstate-semantic-atlas-taxonomy/1"
    assert taxonomy["name"] == "Semantic Atlas"
    assert taxonomy["authority"] == "classification_only"
    assert {row["id"] for row in taxonomy["classification_planes"]} == (EXPECTED_CLASSIFICATION_PLANES)
    assert {row["id"] for row in taxonomy["concept_kinds"]} == EXPECTED_CONCEPT_KINDS
    assert {row["id"] for row in taxonomy["scale_axes"]} == EXPECTED_SCALE_AXES
    assert {row["id"] for row in taxonomy["authority_stages"]} == EXPECTED_AUTHORITY_STAGES
    assert {row["id"] for row in taxonomy["cube_faces"]} == EXPECTED_CUBE_FACES
    assert set(taxonomy["coverage_statuses"]) == EXPECTED_COVERAGE_STATUSES
    meaning_contract = taxonomy["meaning_facet_contract"]
    assert meaning_contract["schema"] == "capability-concept-meaning/1"
    assert {
        key: set(values) for key, values in meaning_contract["closed_values"].items()
    } == EXPECTED_MEANING_FACETS
    assert meaning_contract["fingerprint_includes"] == [
        "concept_id",
        "concept_type",
        "categories",
        "definition",
        "meaning_facets",
    ]
    assert meaning_contract["fingerprint_excludes"] == [
        "label",
        "aliases",
        "source_ids",
        "support",
    ]

    world_event_fields = set(taxonomy["world_event_record"]["required_fields"])
    assert {
        "cause",
        "actor",
        "priority",
        "affected_domains",
        "scope",
        "propagation",
        "expiry",
        "reversibility",
        "supersession",
        "branch_replay_identity",
    } <= world_event_fields


def test_every_domain_shelf_has_plain_scope_contrasts_and_examples(
    glossary: CapabilityGlossary,
):
    expected_keys = {
        "id",
        "label",
        "description",
        "includes",
        "excludes",
        "examples",
        "counterexamples",
    }
    for category_id, row in glossary.categories.items():
        assert set(row) == expected_keys, category_id
        for field in ("includes", "excludes", "examples", "counterexamples"):
            assert isinstance(row[field], list)
            assert row[field]
            assert len(row[field]) == len(set(row[field]))


def test_concept_classification_returns_stable_facets_without_support_authority(
    glossary: CapabilityGlossary,
):
    classification = glossary.concept_classification("action.planar_travel")
    assert classification == {
        "schema": "aetherstate-concept-facets/1",
        "concept_id": "action.planar_travel",
        "concept_kind": "action",
        "domain_shelves": ["movement_travel", "world_scale_authority"],
        "meaning_facets": {
            "semantic_role": "operation",
            "target_cardinality": "unspecified",
            "spatial_extent": "unspecified",
            "world_scope": "unspecified",
        },
        "meaning_fingerprint": glossary.concepts["action.planar_travel"]["meaning_fingerprint"],
    }
    assert "support" not in classification
    assert "authority_stage" not in classification
    assert "cube_coverage" not in classification

    classification["domain_shelves"].append("offense")
    assert glossary.concept_classification("action.planar_travel")["domain_shelves"] == [
        "movement_travel",
        "world_scale_authority",
    ]


def test_every_concept_has_conservative_closed_meaning_facets_and_stable_fingerprint(
    glossary: CapabilityGlossary,
):
    role_counts = Counter()
    for concept_id, concept in glossary.concepts.items():
        facets = concept["meaning_facets"]
        assert set(facets) == set(EXPECTED_MEANING_FACETS), concept_id
        for facet, value in facets.items():
            assert value in EXPECTED_MEANING_FACETS[facet], concept_id
        assert facets["semantic_role"] == EXPECTED_ROLE_BY_KIND[concept["concept_type"]]
        assert concept["meaning_fingerprint"] == concept_meaning_fingerprint(concept)
        role_counts[facets["semantic_role"]] += 1

    assert dict(role_counts) == EXPECTED_ROLE_COUNTS
    assert glossary.concepts["family.direct_pressure"]["meaning_facets"] == {
        "semantic_role": "operation",
        "target_cardinality": "single",
        "spatial_extent": "entity",
        "world_scope": "unspecified",
    }
    assert glossary.concepts["family.sweep_burst"]["meaning_facets"]["target_cardinality"] == "multiple"
    assert glossary.concepts["family.zone_denial"]["meaning_facets"]["spatial_extent"] == "zone"
    assert glossary.concepts["primitive.zone"]["meaning_facets"]["spatial_extent"] == "zone"

    non_target_roles = {
        "resource",
        "grounding_basis",
        "identity",
        "world_rule",
        "mechanic_modifier",
    }
    for concept_id, concept in glossary.concepts.items():
        facets = concept["meaning_facets"]
        if facets["semantic_role"] in non_target_roles:
            assert facets["target_cardinality"] == "not_applicable", concept_id
            assert facets["spatial_extent"] == "not_applicable", concept_id


def test_meaning_fingerprint_tracks_canonical_meaning_not_wording_provenance_or_support(
    glossary: CapabilityGlossary,
):
    original = json.loads(json.dumps(glossary.concepts["family.direct_pressure"]))
    expected = concept_meaning_fingerprint(original)

    for field, replacement in (
        ("label", "Renamed Direct Pressure"),
        ("aliases", ["new authoring phrase"]),
        ("source_ids", ["aetherstate.cross_genre_normalization"]),
        (
            "support",
            {
                "recognition": "canonical",
                "authorization": "frozen_definition_required",
                "receipt_ids": ["future-receipt/9"],
                "narration": "meaning",
            },
        ),
    ):
        changed = json.loads(json.dumps(original))
        changed[field] = replacement
        assert concept_meaning_fingerprint(changed) == expected

    meaning_changes = []
    changed = json.loads(json.dumps(original))
    changed["definition"] += " Changed meaning."
    meaning_changes.append(changed)
    changed = json.loads(json.dumps(original))
    changed["categories"].append("status_condition")
    meaning_changes.append(changed)
    changed = json.loads(json.dumps(original))
    changed["concept_type"] = "action"
    meaning_changes.append(changed)
    changed = json.loads(json.dumps(original))
    changed["meaning_facets"]["target_cardinality"] = "multiple"
    meaning_changes.append(changed)
    assert all(concept_meaning_fingerprint(changed) != expected for changed in meaning_changes)


def test_every_concept_kind_is_defined_by_the_semantic_atlas(glossary: CapabilityGlossary):
    assert {row["concept_type"] for row in glossary.concepts.values()} == EXPECTED_CONCEPT_KINDS
    for concept_id in glossary.concepts:
        classification = glossary.concept_classification(concept_id)
        assert classification["concept_kind"] in EXPECTED_CONCEPT_KINDS
        assert set(classification["domain_shelves"]) <= EXPECTED_CATEGORIES


def test_existing_corpus_is_not_dropped(glossary: CapabilityGlossary):
    required = {
        "family.direct_pressure",
        "family.committed_strike",
        "family.guard_barrier",
        "family.zone_denial",
        "primitive.strike",
        "primitive.information",
        "skill.stealth",
        "skill.spellcraft",
        "ability.second_wind",
        "status.bleeding",
        "status.pregnant",
        "basis.magic",
        "basis.technology",
    }
    assert required <= set(glossary.concepts)

    enemy = json.loads(
        (ROOT / "corpus" / "enemy-capabilities" / "capability-families.json").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (ROOT / "corpus" / "enemy-capabilities" / "registry-snapshot.json").read_text(encoding="utf-8")
    )["registries"]
    assert {f"family.{row['id']}" for row in enemy["families"]} <= set(glossary.concepts)
    assert {f"primitive.{key}" for key in enemy["semantic_primitives"]} <= set(glossary.concepts)
    assert {f"skill.{key}" for key in registry["skills"]} <= set(glossary.concepts)
    assert {f"ability.{key}" for key in registry["abilities"]} <= set(glossary.concepts)
    assert {f"status.{key}" for key in registry["effects"]} <= set(glossary.concepts)


def test_concept_provenance_does_not_overclaim_old_corpus(glossary: CapabilityGlossary):
    assert glossary.concepts["basis.magic"]["source_ids"] == [
        "existing.enemy_capability_families",
        "aetherstate.cross_genre_normalization",
    ]
    assert glossary.concepts["basis.occult"]["source_ids"] == ["aetherstate.cross_genre_normalization"]
    assert glossary.concepts["resource.mana"]["source_ids"] == [
        "existing.registry_snapshot",
        "aetherstate.cross_genre_normalization",
    ]
    assert glossary.concepts["resource.blood_hunger"]["source_ids"] == [
        "aetherstate.cross_genre_normalization"
    ]


def test_each_genre_phrase_has_one_canonical_target(glossary: CapabilityGlossary):
    for genre_id, genre in glossary.genres.items():
        targets: dict[str, set[str]] = {}
        for entries in genre["categories"].values():
            for entry in entries:
                for term in entry["terms"]:
                    targets.setdefault(normalize_phrase(term), set()).add(entry["concept_id"])
        collisions = {term: ids for term, ids in targets.items() if len(ids) > 1}
        assert collisions == {}, (genre_id, collisions)


@pytest.mark.parametrize(
    ("text", "genre_id", "concept_id"),
    [
        ("She raises a radiant ward", "high_fantasy_medieval", "family.guard_barrier"),
        ("The gun team lays down covering fire", "military_war", "family.zone_denial"),
        ("I trigger a neural intrusion", "cyberpunk_dystopian", "family.disrupt"),
        ("His blood frenzy takes hold", "vampire_werewolf", "condition.frenzy"),
        ("Qi deviation rattles her meridians", "eastern_fantasy_wuxia_xianxia", "status.backlash"),
        ("Plot a jump route around the blockade", "space_opera_sci_fi", "skill.navigation"),
    ],
)
def test_genre_language_translates_to_canonical_concepts(
    glossary: CapabilityGlossary,
    text: str,
    genre_id: str,
    concept_id: str,
):
    matches = glossary.translate(text, [genre_id])
    assert concept_id in {match.concept_id for match in matches}
    chosen = next(match for match in matches if match.concept_id == concept_id)
    assert chosen.as_dict()["recognized"] is True
    assert chosen.as_dict()["authorized"] is False
    assert chosen.as_dict()["executable"] is False


def test_genre_hint_ranks_its_manifestation_above_cross_genre_collisions(
    glossary: CapabilityGlossary,
):
    matches = glossary.translate("ward", ["high_fantasy_medieval"])
    assert matches[0].concept_id == "family.guard_barrier"
    assert matches[0].genre_ids == ("high_fantasy_medieval",)


@pytest.mark.parametrize(
    ("text", "genre_id", "concept_id"),
    [
        ("blood hunger", "vampire_werewolf", "resource.blood_hunger"),
        ("vacuum exposure", "space_opera_sci_fi", "condition.vacuum_exposure"),
        ("isolate the infected", "post_apoc_pandemic", "action.isolation"),
        ("acute panic", "survival_horror", "condition.panic"),
        ("avatar desynced", "virtual_world", "condition.avatar_desync"),
        ("aggro radius", "virtual_world", "world.aggro_rule"),
        ("system trace heat", "cyberpunk_dystopian", "resource.law_heat"),
        ("reincarnated body", "isekai_parallel_world", "action.reincarnation"),
        ("possessed computer", "science_fantasy", "condition.possession"),
        ("pact obligation", "demon_angel", "resource.debt_obligation"),
        ("boiler pressure", "steampunk_victorian", "resource.boiler_pressure"),
        ("point of divergence", "alternate_history", "world.timeline_divergence"),
        ("heat from the law", "crime_noir", "resource.law_heat"),
        ("silver weakness", "vampire_werewolf", "world.weakness_rule"),
    ],
)
def test_false_friends_rank_as_distinct_canonical_meanings(
    glossary: CapabilityGlossary,
    text: str,
    genre_id: str,
    concept_id: str,
):
    matches = glossary.translate(text, [genre_id])
    assert matches and matches[0].concept_id == concept_id


def test_broad_false_friend_wording_abstains(glossary: CapabilityGlossary):
    assert glossary.translate("turning the wheel", ["vampire_werewolf"]) == []


def test_unknown_genre_never_falls_back_to_a_guess(glossary: CapabilityGlossary):
    with pytest.raises(GlossaryError, match="unknown genre"):
        glossary.translate("ward", ["not_a_genre"])


def test_one_definition_path_freezes_every_capability_kind(glossary: CapabilityGlossary):
    concept_by_kind = {
        "skill": "skill.stealth",
        "ability": "ability.second_wind",
        "spell": "family.direct_pressure",
        "augment": "ability.edge",
        "cyberware": "basis.technology",
        "enemy_move": "family.committed_strike",
    }
    fingerprints = set()
    for kind in sorted(CAPABILITY_KINDS):
        draft = {
            "kind": kind,
            "name": f"Test {kind}",
            "world_id": "test_world",
            "genre_ids": ["science_fantasy"],
            "concept_ids": [concept_by_kind[kind]],
            "description": "A bounded authored definition.",
            "grounding_evidence": ["creator-authored test fixture"],
            "interpretation_mode": "automatic",
            "power_ceiling": "personal",
        }
        frozen = glossary.freeze_definition(draft)
        assert frozen["schema"] == "capability-definition/1"
        assert frozen["kind"] == kind
        assert frozen["owner_scope"] == "world"
        assert frozen["owner_id"] == "test_world"
        assert frozen["support_classification"]
        assert frozen["fingerprint"].startswith("sha256:")
        fingerprints.add(frozen["fingerprint"])
    assert len(fingerprints) == len(CAPABILITY_KINDS)


def test_freeze_is_deterministic_and_revisioned(glossary: CapabilityGlossary):
    draft = {
        "kind": "enemy_move",
        "name": "Measured Bolt",
        "definition_id": "measured_bolt",
        "world_id": "blackglass",
        "genre_ids": ["dark_fantasy_gothic"],
        "concept_ids": ["family.direct_pressure"],
        "grounding_evidence": ["functional crossbow"],
        "receipt_id": "enemy-opposition-hp/1",
        "receipt_concept_ids": ["family.direct_pressure"],
        "basis": "projectile",
        "delivery": "crossbow bolt",
        "semantic_primitive": "strike",
        "functional_family": "direct_pressure",
        "effect_channel": "hp",
        "target": "single_player",
        "range": "near",
        "area": "single_target",
        "timing": "committed",
        "cadence": "reliable",
        "interpretation_mode": "explicit_only",
    }
    left = glossary.freeze_definition(draft)
    right = glossary.freeze_definition(dict(reversed(list(draft.items()))))
    assert left == right
    assert left["requested_receipt_id"] == "enemy-opposition-hp/1"
    assert left["receipt_id"] is None
    assert left["support_classification"][0]["classification"] == "narration_boundary"

    revised = dict(draft, revision=2, parent_fingerprint=left["fingerprint"], range="far")
    child = glossary.freeze_definition(revised)
    assert child["parent_fingerprint"] == left["fingerprint"]
    assert child["fingerprint"] != left["fingerprint"]


def test_enemy_receipt_rejects_underspecified_and_implicit_coverage(
    glossary: CapabilityGlossary,
):
    underspecified = glossary.freeze_definition(
        {
            "kind": "enemy_move",
            "name": "Unspecified Harm",
            "world_id": "blackglass",
            "concept_ids": ["family.direct_pressure"],
            "grounding_evidence": ["Creator-authored enemy move"],
            "receipt_id": "enemy-opposition-hp/1",
            "receipt_concept_ids": ["family.direct_pressure"],
        }
    )
    assert underspecified["receipt_id"] is None
    assert underspecified["receipt_validation"]["admitted"] is False
    assert any(
        "missing required fields" in reason for reason in underspecified["receipt_validation"]["reasons"]
    )

    complete_but_implicit = glossary.freeze_definition(
        {
            "kind": "enemy_move",
            "name": "Implicit Bolt",
            "world_id": "blackglass",
            "concept_ids": ["family.direct_pressure"],
            "grounding_evidence": ["functional crossbow"],
            "receipt_id": "enemy-opposition-hp/1",
            "basis": "projectile",
            "delivery": "crossbow bolt",
            "semantic_primitive": "strike",
            "functional_family": "direct_pressure",
            "effect_channel": "hp",
            "target": "single_player",
            "range": "near",
            "area": "single_target",
            "timing": "fast",
            "cadence": "reliable",
        }
    )
    assert complete_but_implicit["receipt_id"] is None
    assert complete_but_implicit["receipt_concept_ids"] == []


def test_receipt_coverage_prevents_lore_concept_hitchhiking(
    glossary: CapabilityGlossary,
):
    draft = {
        "kind": "enemy_move",
        "name": "Planar Bolt",
        "world_id": "blackglass",
        "concept_ids": ["family.direct_pressure", "action.planar_travel"],
        "grounding_evidence": ["functional crossbow and a world-authored planar visual"],
        "receipt_id": "enemy-opposition-hp/1",
        "basis": "projectile",
        "delivery": "crossbow bolt",
        "semantic_primitive": "strike",
        "functional_family": "direct_pressure",
        "effect_channel": "hp",
        "target": "single_player",
        "range": "near",
        "area": "single_target",
        "timing": "fast",
        "cadence": "reliable",
    }
    implicit = glossary.freeze_definition(draft)
    assert implicit["receipt_id"] is None

    explicit = glossary.freeze_definition(dict(draft, receipt_concept_ids=["family.direct_pressure"]))
    assert explicit["receipt_id"] is None
    classifications = {row["concept_id"]: row["classification"] for row in explicit["support_classification"]}
    assert classifications["family.direct_pressure"] == "narration_boundary"
    assert classifications["action.planar_travel"] == "lore_only"


def test_registry_receipts_do_not_promote_new_recognition_domains(
    glossary: CapabilityGlossary,
):
    registry_skill = glossary.freeze_definition(
        {
            "kind": "skill",
            "name": "Shadow Practice",
            "world_id": "blackglass",
            "concept_ids": ["skill.stealth"],
            "grounding_evidence": ["Owned preserved Stealth skill"],
            "receipt_id": "check/1",
            "receipt_concept_ids": ["skill.stealth"],
        }
    )
    assert registry_skill["requested_receipt_id"] == "check/1"
    assert registry_skill["receipt_id"] is None
    assert any(
        "not admitted by this compiler version" in reason
        for reason in registry_skill["receipt_validation"]["reasons"]
    )

    recognition_only = glossary.freeze_definition(
        {
            "kind": "skill",
            "name": "Astrogation",
            "world_id": "blackglass",
            "concept_ids": ["skill.navigation"],
            "grounding_evidence": ["Creator-authored navigation training"],
            "receipt_id": "check/1",
            "receipt_concept_ids": ["skill.navigation"],
        }
    )
    assert recognition_only["requested_receipt_id"] == "check/1"
    assert recognition_only["receipt_id"] is None
    assert any(
        "not admitted by this compiler version" in reason
        for reason in recognition_only["receipt_validation"]["reasons"]
    )


def test_compiler_v1_advertises_and_admits_no_runtime_receipts(glossary: CapabilityGlossary):
    assert all(not concept["support"]["receipt_ids"] for concept in glossary.concepts.values())

    requested = [
        _valid_enemy_draft(),
        {
            "kind": "skill",
            "name": "Shadow Practice",
            "world_id": "blackglass",
            "concept_ids": ["skill.stealth"],
            "grounding_evidence": ["Owned preserved Stealth skill"],
            "receipt_id": "check/1",
            "receipt_concept_ids": ["skill.stealth"],
        },
        {
            "kind": "ability",
            "name": "Second Wind",
            "world_id": "blackglass",
            "concept_ids": ["ability.second_wind"],
            "grounding_evidence": ["Owned preserved ability"],
            "receipt_id": "ability-check-shape/1",
            "receipt_concept_ids": ["ability.second_wind"],
        },
    ]
    for draft in requested:
        frozen = glossary.freeze_definition(draft)
        assert frozen["requested_receipt_id"] == draft["receipt_id"]
        assert frozen["receipt_id"] is None
        assert frozen["receipt_validation"]["admitted"] is False


def test_set_like_definition_fields_have_order_independent_fingerprints(
    glossary: CapabilityGlossary,
):
    left = {
        "kind": "skill",
        "name": "Courtly Scout",
        "world_id": "blackglass",
        "genre_ids": ["crime_noir", "high_fantasy_medieval"],
        "concept_ids": ["skill.stealth", "skill.perception"],
        "aliases": ["Quiet Eye", "Hidden Witness"],
        "grounding_evidence": ["Creator-authored training"],
    }
    right = dict(
        left,
        genre_ids=list(reversed(left["genre_ids"])),
        concept_ids=list(reversed(left["concept_ids"])),
        aliases=list(reversed(left["aliases"])),
    )
    assert glossary.freeze_definition(left) == glossary.freeze_definition(right)


@pytest.mark.parametrize("revision", [True, 1.9, "1"])
def test_revision_rejects_coercible_non_integers(glossary: CapabilityGlossary, revision):
    with pytest.raises(GlossaryError, match="revision must be an integer"):
        glossary.freeze_definition(
            {
                "kind": "skill",
                "name": "Exact Revision",
                "world_id": "blackglass",
                "concept_ids": ["skill.stealth"],
                "grounding_evidence": ["Creator-authored training"],
                "revision": revision,
            }
        )


def test_receipt_envelope_whitespace_normalizes_before_freezing(glossary: CapabilityGlossary):
    clean = _valid_enemy_draft()
    padded = dict(clean)
    for field in (
        "kind",
        "name",
        "world_id",
        "receipt_id",
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
    ):
        padded[field] = f"  {padded[field]}  "
    assert glossary.freeze_definition(clean) == glossary.freeze_definition(padded)


def test_definition_uses_one_deep_snapshot_and_does_not_alias_caller_data(
    glossary: CapabilityGlossary,
):
    class ShiftingMapping(Mapping):
        def __init__(self, values):
            self.values = values
            self.effect_reads = 0

        def __iter__(self):
            return iter(self.values)

        def __len__(self):
            return len(self.values)

        def __getitem__(self, key):
            if key == "effect_channel":
                self.effect_reads += 1
                return "hp" if self.effect_reads == 1 else "world_delete"
            return self.values[key]

    shifted = glossary.freeze_definition(ShiftingMapping(_valid_enemy_draft()))
    assert shifted["effect_channel"] == "hp"
    assert shifted["receipt_id"] is None

    cost = {"mana": {"amount": 1}}
    draft = {
        "kind": "spell",
        "name": "Bound Cost",
        "world_id": "blackglass",
        "concept_ids": ["action.planar_travel"],
        "grounding_evidence": ["Creator-authored world law"],
        "cost": cost,
    }
    frozen = glossary.freeze_definition(draft)
    cost["mana"]["amount"] = 999
    assert frozen["cost"] == {"mana": {"amount": 1}}
    assert frozen["fingerprint"] == content_fingerprint(
        {key: value for key, value in frozen.items() if key != "fingerprint"}
    )


def test_public_corpus_mutation_cannot_create_authority_or_fake_genres():
    glossary = CapabilityGlossary.load(CORPUS)
    glossary.concepts["skill.navigation"]["source_ids"].append("existing.registry_snapshot")
    glossary.concepts["skill.navigation"]["support"]["receipt_ids"].append("check/1")
    glossary.genres["forged_genre"] = {}

    frozen = glossary.freeze_definition(
        {
            "kind": "skill",
            "name": "Forged Navigation",
            "world_id": "blackglass",
            "concept_ids": ["skill.navigation"],
            "grounding_evidence": ["Creator-authored training"],
            "receipt_id": "check/1",
            "receipt_concept_ids": ["skill.navigation"],
        }
    )
    assert frozen["receipt_id"] is None
    with pytest.raises(GlossaryError, match="unknown genre"):
        glossary.freeze_definition(
            {
                "kind": "skill",
                "name": "Forged Genre",
                "world_id": "blackglass",
                "genre_ids": ["forged_genre"],
                "concept_ids": ["skill.navigation"],
                "grounding_evidence": ["Creator-authored training"],
            }
        )


def test_unsupported_meaning_is_preserved_without_inventing_a_receipt(
    glossary: CapabilityGlossary,
):
    frozen = glossary.freeze_definition(
        {
            "kind": "spell",
            "name": "Gate of Returning",
            "world_id": "test_world",
            "genre_ids": ["isekai_parallel_world"],
            "concept_ids": ["action.planar_travel"],
            "grounding_evidence": ["Creator-authored world law"],
            "description": "Opens a path between worlds.",
        }
    )
    assert frozen["receipt_id"] is None
    assert frozen["support_classification"] == [
        {
            "concept_id": "action.planar_travel",
            "classification": "lore_only",
            "reason": "Meaning is preserved, but no admitted receipt was supplied for this definition.",
        }
    ]

    wrong_receipt = glossary.freeze_definition(
        {
            "kind": "spell",
            "name": "Gate of Returning",
            "world_id": "test_world",
            "genre_ids": ["isekai_parallel_world"],
            "concept_ids": ["action.planar_travel"],
            "grounding_evidence": ["Creator-authored world law"],
            "receipt_id": "enemy-opposition-hp/1",
        }
    )
    assert wrong_receipt["requested_receipt_id"] == "enemy-opposition-hp/1"
    assert wrong_receipt["receipt_id"] is None

    wrong_kind = glossary.freeze_definition(
        {
            "kind": "spell",
            "name": "Arc Bolt",
            "world_id": "test_world",
            "genre_ids": ["high_fantasy_medieval"],
            "concept_ids": ["family.direct_pressure"],
            "grounding_evidence": ["Creator-authored spell"],
            "receipt_id": "enemy-opposition-hp/1",
        }
    )
    assert wrong_kind["receipt_id"] is None


def test_definition_without_grounding_stays_editable(glossary: CapabilityGlossary):
    with pytest.raises(GlossaryError, match="rejected grounding"):
        glossary.freeze_definition(
            {
                "kind": "ability",
                "name": "Unowned Miracle",
                "world_id": "test_world",
                "genre_ids": ["mythological_ancient_world"],
                "concept_ids": ["basis.divine"],
            }
        )


def test_manifest_declares_real_artifact_hashes_and_counts(glossary: CapabilityGlossary):
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"] == {
        "categories": len(glossary.categories),
        "classification_planes": len(glossary.taxonomy["classification_planes"]),
        "concepts": len(glossary.concepts),
        "concept_kinds": len(glossary.taxonomy["concept_kinds"]),
        "cube_faces": len(glossary.taxonomy["cube_faces"]),
        "genres": len(glossary.genres),
        "scale_axes": len(glossary.taxonomy["scale_axes"]),
        "sources": len(glossary.sources),
    }
    assert manifest["taxonomy_file"] == "taxonomy.json"
    for rel, expected in manifest["artifacts"].items():
        path = CORPUS / rel
        assert path.is_file()
        assert raw_fingerprint(path.read_bytes()) == expected["fingerprint"]
        assert path.stat().st_size == expected["bytes"]

    for genre in glossary.genres.values():
        cited_urls = {
            glossary.sources[source_id]["url"]
            for source_id in genre["source_ids"]
            if glossary.sources[source_id]["kind"] == "web_research"
        }
        assert cited_urls == set(genre["source_urls"])


def test_loader_enforces_manifest_artifact_seal(tmp_path: Path):
    copied = tmp_path / "capability-glossary"
    shutil.copytree(CORPUS, copied)
    categories = copied / "categories.json"
    categories.write_bytes(categories.read_bytes() + b" ")
    with pytest.raises(GlossaryError, match="artifact integrity mismatch"):
        CapabilityGlossary.load(copied)


def test_manifest_contract_rejects_missing_counts_noninteger_bytes_and_omitted_provenance(
    tmp_path: Path,
):
    for fault in ("count", "bytes", "provenance"):
        copied = tmp_path / fault
        shutil.copytree(CORPUS, copied)
        manifest_path = copied / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if fault == "count":
            manifest["counts"].pop("sources")
        elif fault == "bytes":
            manifest["artifacts"]["README.md"]["bytes"] = str(manifest["artifacts"]["README.md"]["bytes"])
        else:
            manifest["artifacts"].pop("research/2026-07-13-genre-gap-audit.md")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(GlossaryError):
            CapabilityGlossary.load(copied)


@pytest.mark.parametrize(
    "fault",
    [
        "receipt",
        "category_id",
        "genre_id",
        "unresolved_request",
        "source_url",
        "concept_source",
        "concept_web_source",
        "cluster_id",
        "genre_empty_sources",
        "genre_wrong_sources",
        "source_swapped_urls",
        "source_extra_genre",
        "source_unused_web",
        "source_local_kind",
        "taxonomy_authority",
        "taxonomy_kind",
        "taxonomy_facet_value",
        "concept_type",
        "concept_facet",
        "meaning_fingerprint",
        "category_contract",
    ],
)
def test_loader_rejects_semantically_invalid_but_resealed_v1_corpus(
    tmp_path: Path,
    fault: str,
):
    copied = tmp_path / fault
    shutil.copytree(CORPUS, copied)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if fault in {
        "source_swapped_urls",
        "source_extra_genre",
        "source_unused_web",
        "source_local_kind",
    }:
        rel = "sources.json"
        path = copied / rel
        doc = json.loads(path.read_text(encoding="utf-8"))
        web_rows = [row for row in doc["sources"] if row["kind"] == "web_research"]
        if fault == "source_swapped_urls":
            pair = next(
                (left, right)
                for left in web_rows
                for right in web_rows
                if left is not right and set(left["genre_ids"]) & set(right["genre_ids"])
            )
            pair[0]["url"], pair[1]["url"] = pair[1]["url"], pair[0]["url"]
        elif fault == "source_extra_genre":
            row = web_rows[0]
            extra = next(genre for genre in EXPECTED_GENRES if genre not in row["genre_ids"])
            row["genre_ids"].append(extra)
        elif fault == "source_unused_web":
            url = "https://example.com/unused-glossary-source"
            doc["sources"].append(
                {
                    "id": "web." + hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    "kind": "web_research",
                    "url": url,
                    "label": "example.com/unused-glossary-source",
                    "genre_ids": [],
                    "reuse": "research_and_normalization_only",
                }
            )
            manifest["counts"]["sources"] += 1
        else:
            local = next(row for row in doc["sources"] if row["kind"] != "web_research")
            local["kind"] = "web_research"
    elif fault in {"taxonomy_authority", "taxonomy_kind", "taxonomy_facet_value"}:
        rel = "taxonomy.json"
        path = copied / rel
        doc = json.loads(path.read_text(encoding="utf-8"))
        if fault == "taxonomy_authority":
            doc["authority"] = "runtime_authority"
        elif fault == "taxonomy_facet_value":
            doc["meaning_facet_contract"]["closed_values"]["spatial_extent"].append("planet")
        else:
            doc["concept_kinds"][0]["id"] = "banana"
    elif fault in {
        "receipt",
        "concept_source",
        "concept_web_source",
        "concept_type",
        "concept_facet",
        "meaning_fingerprint",
    }:
        rel = "concepts.json"
        path = copied / rel
        doc = json.loads(path.read_text(encoding="utf-8"))
        if fault == "receipt":
            doc["concepts"][0]["support"]["receipt_ids"] = ["invented/1"]
        elif fault == "concept_source":
            doc["concepts"][0]["source_ids"] = []
        elif fault == "concept_type":
            doc["concepts"][0]["concept_type"] = "banana"
        elif fault == "concept_facet":
            doc["concepts"][0]["meaning_facets"]["world_scope"] = "planet"
        elif fault == "meaning_fingerprint":
            doc["concepts"][0]["meaning_fingerprint"] = "sha256:" + ("0" * 64)
        else:
            sources = json.loads((copied / "sources.json").read_text(encoding="utf-8"))["sources"]
            web_source = next(row["id"] for row in sources if row["kind"] == "web_research")
            doc["concepts"][0]["source_ids"] = [web_source]
    elif fault in {"category_id", "category_contract"}:
        rel = "categories.json"
        path = copied / rel
        doc = json.loads(path.read_text(encoding="utf-8"))
        if fault == "category_id":
            doc["categories"][0]["id"] = "banana"
        else:
            doc["categories"][0]["counterexamples"] = []
    else:
        genre_paths = sorted((copied / "genres").glob("*.json"))
        path = genre_paths[1] if fault == "cluster_id" else genre_paths[0]
        rel = path.relative_to(copied).as_posix()
        doc = json.loads(path.read_text(encoding="utf-8"))
        if fault == "genre_id":
            doc["genres"][0]["id"] = "renamed_but_counted_genre"
        elif fault == "unresolved_request":
            doc["concept_requests"] = [{"id": "unresolved.concept"}]
        elif fault == "source_url":
            doc["genres"][0]["source_urls"][0] = "file:///private/source"
        elif fault == "genre_empty_sources":
            doc["genres"][0]["source_ids"] = []
        elif fault == "genre_wrong_sources":
            doc["genres"][0]["source_ids"] = list(doc["genres"][1]["source_ids"])
        else:
            first_doc = json.loads(genre_paths[0].read_text(encoding="utf-8"))
            doc["cluster"] = first_doc["cluster"]
    payload = (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(payload)
    manifest["artifacts"][rel] = {
        "bytes": len(payload),
        "fingerprint": raw_fingerprint(payload),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(GlossaryError):
        CapabilityGlossary.load(copied)


def test_artifact_paths_reject_windows_alternate_streams(tmp_path: Path):
    with pytest.raises(GlossaryError, match="forward-slash"):
        _resolve_artifact(tmp_path, "categories.json:hidden")


def test_loader_parses_the_exact_bytes_it_verified(tmp_path: Path, monkeypatch):
    copied = tmp_path / "capability-glossary"
    shutil.copytree(CORPUS, copied)
    concepts_path = (copied / "concepts.json").resolve()
    original_read_bytes = Path.read_bytes
    changed = False

    def racing_read_bytes(path):
        nonlocal changed
        payload = original_read_bytes(path)
        if path.resolve() == concepts_path and not changed:
            changed = True
            doc = json.loads(payload.decode("utf-8"))
            concept = next(row for row in doc["concepts"] if row["id"] == "action.planar_travel")
            concept["aliases"].append("unsealed race alias")
            path.write_text(json.dumps(doc), encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    loaded = CapabilityGlossary.load(copied)
    assert changed is True
    assert loaded.translate("unsealed race alias") == []


def test_genre_baselines_match_concept_provenance(glossary: CapabilityGlossary):
    registry_sources = {"existing.registry_snapshot", "existing.registry_runtime"}
    for genre in glossary.genres.values():
        for entries in genre["categories"].values():
            for entry in entries:
                sources = set(glossary.concepts[entry["concept_id"]]["source_ids"])
                expected = (
                    "registry_corpus"
                    if sources & registry_sources
                    else "existing_corpus"
                    if any(source.startswith("existing.") for source in sources)
                    else "gap_filled"
                )
                assert entry["baseline"] == expected
    assert glossary.concepts["ability.reroll"]["source_ids"] == ["existing.registry_runtime"]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("blank_label", "requires a label"),
        ("empty_aliases", "cannot be empty"),
        ("empty_terms", "cannot be empty"),
        ("baseline", "baseline/provenance mismatch"),
        ("collision", "phrase collision"),
        ("category_id", "category catalog mismatch"),
    ],
)
def test_finalizer_preflight_rejects_data_the_loader_would_reject(
    tmp_path: Path,
    fault: str,
    message: str,
):
    genre_root = tmp_path / "genres"
    shutil.copytree(CORPUS / "genres", genre_root)
    categories = json.loads((CORPUS / "categories.json").read_text(encoding="utf-8"))
    concepts = json.loads((CORPUS / "concepts.json").read_text(encoding="utf-8"))
    files = sorted(genre_root.glob("*.json"))
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    genre = doc["genres"][0]
    first_category = next(iter(genre["categories"]))
    first_entry = genre["categories"][first_category][0]
    if fault == "blank_label":
        genre["label"] = " "
    elif fault == "empty_aliases":
        genre["aliases"] = []
    elif fault == "empty_terms":
        first_entry["terms"] = []
    elif fault == "baseline":
        first_entry["baseline"] = (
            "gap_filled" if first_entry["baseline"] != "gap_filled" else "existing_corpus"
        )
    elif fault == "collision":
        entries = [entry for rows in genre["categories"].values() for entry in rows]
        second_entry = next(entry for entry in entries if entry["concept_id"] != first_entry["concept_id"])
        second_entry["terms"][0] = first_entry["terms"][0]
    else:
        old_id = categories["categories"][0]["id"]
        categories["categories"][0]["id"] = "banana"
        for concept in concepts["concepts"]:
            concept["categories"] = ["banana" if item == old_id else item for item in concept["categories"]]
        for path in files:
            cluster = doc if path == files[0] else json.loads(path.read_text(encoding="utf-8"))
            for row in cluster["genres"]:
                row["categories"]["banana"] = row["categories"].pop(old_id)
            path.write_text(json.dumps(cluster), encoding="utf-8")
    if fault != "category_id":
        files[0].write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        _preflight_clusters(files, categories, concepts)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("authority", "classification_only"),
        ("concept_kind", "concept kind"),
        ("concept_reference", "unknown concept kind"),
        ("scale_axis", "scale axes"),
        ("cube_face", "Cube faces"),
        ("facet_contract", "meaning facet"),
        ("concept_facet", "meaning facet"),
        ("meaning_fingerprint", "meaning fingerprint"),
        ("category_contract", "counterexamples"),
    ],
)
def test_finalizer_taxonomy_preflight_rejects_incomplete_or_authority_claiming_classification(
    fault: str,
    message: str,
):
    categories = json.loads((CORPUS / "categories.json").read_text(encoding="utf-8"))
    concepts = json.loads((CORPUS / "concepts.json").read_text(encoding="utf-8"))
    taxonomy = json.loads((CORPUS / "taxonomy.json").read_text(encoding="utf-8"))

    if fault == "authority":
        taxonomy["authority"] = "runtime_authority"
    elif fault == "concept_kind":
        taxonomy["concept_kinds"][0]["id"] = "banana"
    elif fault == "concept_reference":
        concepts["concepts"][0]["concept_type"] = "banana"
    elif fault == "scale_axis":
        taxonomy["scale_axes"].pop()
    elif fault == "cube_face":
        taxonomy["cube_faces"].pop()
    elif fault == "facet_contract":
        taxonomy["meaning_facet_contract"]["closed_values"]["world_scope"].pop()
    elif fault == "concept_facet":
        concepts["concepts"][0]["meaning_facets"]["semantic_role"] = "banana"
    elif fault == "meaning_fingerprint":
        concepts["concepts"][0]["meaning_fingerprint"] = "sha256:" + ("0" * 64)
    else:
        categories["categories"][0]["counterexamples"] = []

    with pytest.raises(ValueError, match=message):
        _preflight_taxonomy(categories, concepts, taxonomy)


def test_builder_is_independent_of_manifestation_map_order(tmp_path: Path):
    enemy_root = tmp_path / "corpus" / "enemy-capabilities"
    enemy_root.mkdir(parents=True)
    for name in ("capability-families.json", "registry-snapshot.json"):
        shutil.copy2(ROOT / "corpus" / "enemy-capabilities" / name, enemy_root / name)
    build_capability_glossary(tmp_path)
    first = (tmp_path / "corpus" / "capability-glossary" / "concepts.json").read_bytes()
    categories = (tmp_path / "corpus" / "capability-glossary" / "categories.json").read_bytes()
    taxonomy = (tmp_path / "corpus" / "capability-glossary" / "taxonomy.json").read_bytes()
    assert b"\r" not in first
    assert b"\r" not in categories
    assert b"\r" not in taxonomy

    families_path = enemy_root / "capability-families.json"
    families = json.loads(families_path.read_text(encoding="utf-8"))
    manifestations = families["families"][0]["genre_manifestations"]
    families["families"][0]["genre_manifestations"] = dict(reversed(list(manifestations.items())))
    families_path.write_text(json.dumps(families), encoding="utf-8")
    build_capability_glossary(tmp_path)
    assert (tmp_path / "corpus" / "capability-glossary" / "concepts.json").read_bytes() == first
    assert (tmp_path / "corpus" / "capability-glossary" / "taxonomy.json").read_bytes() == taxonomy


def test_default_loader_uses_repo_local_corpus():
    assert set(load_default_glossary(ROOT).genres) == EXPECTED_GENRES
