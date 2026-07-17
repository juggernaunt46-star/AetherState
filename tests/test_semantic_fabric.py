from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from aetherstate.capability_glossary import CapabilityGlossary, content_fingerprint
from aetherstate.semantic_fabric import (
    LEX_IDS,
    MEANING_RECEIPT_SCHEMA,
    MEANING_SCHEMA,
    CompiledMeaning,
    SemanticFabric,
    SemanticFabricError,
    load_default_semantic_fabric,
    semantic_entry_meaning_fingerprint,
    validate_compiled_meaning,
    validate_compiled_meaning_receipt,
)
from tools.build_semantic_fabric import build as build_semantic_fabric


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "semantic-fabric"
CAPABILITY_CORPUS = ROOT / "corpus" / "capability-glossary"


@pytest.fixture(scope="module")
def capability_lex() -> CapabilityGlossary:
    return CapabilityGlossary.load(CAPABILITY_CORPUS)


@pytest.fixture(scope="module")
def fabric(capability_lex: CapabilityGlossary) -> SemanticFabric:
    return SemanticFabric.load(CORPUS, capability_glossary=capability_lex)


def _rewrite_manifest(corpus: Path, relative: str) -> None:
    manifest_path = corpus / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw = (corpus / relative).read_bytes()
    artifact = next(row for row in manifest["artifacts"] if row["path"] == relative)
    artifact["bytes"] = len(raw)
    artifact["sha256"] = hashlib.sha256(raw).hexdigest()
    payload = {key: manifest[key] for key in manifest if key != "fingerprint"}
    manifest["fingerprint"] = content_fingerprint(payload)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_sealed_corpora(tmp_path: Path) -> Path:
    copied_root = tmp_path / "corpus"
    copied_root.mkdir()
    copied = copied_root / "semantic-fabric"
    shutil.copytree(CORPUS, copied)
    shutil.copytree(CAPABILITY_CORPUS, copied_root / "capability-glossary")
    return copied


def test_default_family_has_five_lexes_and_every_existing_genre(
    fabric: SemanticFabric,
    capability_lex: CapabilityGlossary,
) -> None:
    assert LEX_IDS == ("capability", "referent", "scene", "action", "claim")
    assert fabric.capability_glossary is capability_lex
    assert len(fabric.genre_ids) == 31
    assert {entry.lex_id for entry in fabric.entries.values()} == {
        "referent",
        "scene",
        "action",
        "claim",
    }
    assert all(
        entry.meaning_fingerprint == semantic_entry_meaning_fingerprint(entry.lex_id, entry.as_dict())
        for entry in fabric.entries.values()
    )
    assert len(fabric.entries_for("referent")) == 13
    assert len(fabric.entries_for("scene")) == 18
    assert len(fabric.entries_for("action")) == 15
    assert len(fabric.entries_for("claim")) == 16
    assert all(
        fabric.constructions_for(lex_id)
        for lex_id in ("referent", "scene", "action", "claim")
    )
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "aetherstate-semantic-fabric/1"
    assert manifest["family_version"] == 3
    assert manifest["capability_lex"]["schema"] == "aetherstate-capability-glossary/2"
    for lex_id in ("referent", "scene", "action", "claim"):
        pack = json.loads((CORPUS / f"{lex_id}-lex.json").read_text(encoding="utf-8"))
        assert pack["schema"] == "semantic-translation-memory/2"
        assert pack["version"] == 2
    assert capability_lex.taxonomy["name"] == "Semantic Atlas"

    default_meaning = fabric.translate("I use swordplay to attack his shield.")
    assert {match.lex_id for match in default_meaning.matches} == {
        "capability",
        "referent",
        "scene",
        "action",
    }

    claim_meaning = fabric.translate("Mara reported that Vosk denied the gate was open.")
    assert "claim" in {match.lex_id for match in claim_meaning.matches}


@pytest.mark.parametrize(
    "text",
    (
        'Selene says, "The East Gate is shut."',
        '"The East Gate is shut," Selene says.',
    ),
)
def test_named_dialogue_reuses_sealed_direct_assertion_without_new_atlas_meaning(
    fabric: SemanticFabric,
    text: str,
) -> None:
    meaning = fabric.translate(text)
    rows = [
        match for match in meaning.matches
        if match.lex_id == "claim" and match.surface_baseline == "dialogue_construction"
    ]

    assert len(rows) == 1
    row = rows[0]
    assertion = fabric.entry("claim.assertion.direct")
    assert row.concept_id == assertion.concept_id
    assert row.entry_fingerprint == assertion.fingerprint
    assert row.matched_phrase == "says"
    assert text[row.start:row.end] == row.matched_phrase
    assert len(fabric.entries_for("claim")) == 16
    assert validate_compiled_meaning(meaning.as_dict()) == meaning.as_dict()


def test_entry_meaning_fingerprint_excludes_wording_provenance_and_support(
    fabric: SemanticFabric,
) -> None:
    original = fabric.entry("action.repair").as_dict()
    expected = original["meaning_fingerprint"]

    for key, value in {
        "label": "Entirely different display label",
        "terms": ["entirely different surface"],
        "genres": ["cyberpunk"],
        "genre_terms": [
            {
                "term": "different genre surface",
                "genres": ["cyberpunk"],
                "source_ids": ["web.verbnet"],
                "baseline": "genre_authored",
            }
        ],
        "false_friends": ["different false friend"],
        "source_ids": ["web.verbnet"],
        "provenance": {"reviewed": False},
        "support": {"recognition": "different"},
    }.items():
        changed = json.loads(json.dumps(original))
        changed[key] = value
        assert semantic_entry_meaning_fingerprint("action", changed) == expected

    for key, value in {
        "concept_id": "action.different",
        "kind": "different_kind",
        "required_roles": ["actor"],
        "optional_roles": ["target"],
        "completion": "different_completion",
        "features": {"action_class": "different"},
    }.items():
        changed = json.loads(json.dumps(original))
        changed[key] = value
        assert semantic_entry_meaning_fingerprint("action", changed) != expected
    assert semantic_entry_meaning_fingerprint("scene", original) != expected
    assert original["fingerprint"] != expected


def test_loader_rejects_resealed_noncanonical_entry_meaning_fingerprint(tmp_path: Path) -> None:
    copied = _copy_sealed_corpora(tmp_path)
    relative = "action-lex.json"
    pack_path = copied / relative
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    entry = next(row for row in pack["entries"] if row["concept_id"] == "action.repair")
    entry["meaning_fingerprint"] = "sha256:" + ("0" * 64)
    entry_payload = {key: entry[key] for key in entry if key != "fingerprint"}
    entry["fingerprint"] = content_fingerprint(entry_payload)
    pack_payload = {key: pack[key] for key in pack if key != "fingerprint"}
    pack["fingerprint"] = content_fingerprint(pack_payload)
    pack_path.write_text(
        json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _rewrite_manifest(copied, relative)

    with pytest.raises(SemanticFabricError, match="meaning fingerprint mismatch"):
        SemanticFabric.load(copied)


def test_loader_explicitly_rejects_old_v1_translation_memory_pack(tmp_path: Path) -> None:
    copied = _copy_sealed_corpora(tmp_path)
    relative = "action-lex.json"
    pack_path = copied / relative
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["schema"] = "semantic-translation-memory/1"
    pack["version"] = 1
    payload = {key: pack[key] for key in pack if key != "fingerprint"}
    pack["fingerprint"] = content_fingerprint(payload)
    pack_path.write_text(
        json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _rewrite_manifest(copied, relative)

    with pytest.raises(SemanticFabricError, match="translation-memory schema v1"):
        SemanticFabric.load(copied)


def test_semantic_fabric_requires_its_sibling_capability_lex(tmp_path: Path) -> None:
    isolated_root = tmp_path / "isolated-corpus"
    isolated_root.mkdir()
    copied = isolated_root / "semantic-fabric"
    shutil.copytree(CORPUS, copied)

    with pytest.raises(SemanticFabricError, match="requires its sealed CapabilityLex corpus"):
        SemanticFabric.load(copied)


def test_live_compound_possessive_vocabulary_compiles_once(fabric: SemanticFabric) -> None:
    text = (
        "I use Sunspoke Chain-Reversal to whip the starwire around Vosk's mirror shield rim, "
        "tear it aside, and drive the weighted sun-hook into his chest."
    )
    meaning = fabric.translate(text)
    concepts = set(meaning.concepts())
    assert {
        "referent.object.equipment_head",
        "referent.object_part.component",
        "referent.pronoun.personal",
        "referent.pronoun.possessive",
        "referent.body_part.torso",
        "action.use_capability",
        "action.weapon_attack",
    } <= concepts
    for match in meaning.matches:
        assert text[match.start : match.end] == match.matched_phrase
        payload = match.as_dict()
        assert payload["recognized"] is True
        assert payload["authorized"] is False
        assert payload["executable"] is False
        assert payload["requires_context_binding"] is True


def test_possessive_construction_never_claims_ledger_ownership(fabric: SemanticFabric) -> None:
    construction = next(
        row
        for row in fabric.constructions_for("referent")
        if row.construction_id == "referent.possession.genitive"
    )
    assert construction.required_roles == ("possessor", "possessed_object")
    assert construction.optional_roles == ("possessed_object_part",)
    assert construction.completion == "linguistic_owner_relation_only"
    assert construction.features["proves_ledger_ownership"] is False


def test_declared_polysemy_remains_unresolved(fabric: SemanticFabric) -> None:
    meaning = fabric.translate("I turn the head aside.", lex_ids=("referent",))
    candidates = {match.concept_id for match in meaning.matches if match.matched_phrase.casefold() == "head"}
    assert candidates == {"referent.body_part.head", "referent.object_part.component"}
    assert set(meaning.unresolved) == candidates


def test_capability_lex_matches_repeated_and_alternate_surfaces_with_stable_lineage(
    fabric: SemanticFabric,
    capability_lex: CapabilityGlossary,
) -> None:
    text = "Brawl, attack, then brawl."
    meaning = fabric.translate(text, lex_ids=("capability",))
    matches = [match for match in meaning.for_lex("capability") if match.concept_id == "skill.brawl"]

    assert [match.matched_phrase.casefold() for match in matches] == [
        "brawl",
        "attack",
        "brawl",
    ]
    assert all(text[match.start : match.end] == match.matched_phrase for match in matches)
    concept_fingerprint, source_ids = capability_lex.concept_lineage("skill.brawl")
    assert {match.entry_fingerprint for match in matches} == {concept_fingerprint}
    assert {match.source_ids for match in matches} == {source_ids}
    expected = capability_lex.concept_classification("skill.brawl")
    for match in matches:
        assert match.features == {
            "categories": expected["domain_shelves"],
            "meaning_facets": expected["meaning_facets"],
            "meaning_fingerprint": expected["meaning_fingerprint"],
        }
        assert "support" not in match.features
        assert "authority_stage" not in match.features


def test_capability_same_span_polysemy_is_atomic_and_unresolved(fabric: SemanticFabric) -> None:
    meaning = fabric.translate("I use stealth.", lex_ids=("capability",))
    matches = meaning.for_lex("capability")
    candidates = ("family.conceal_ambush", "skill.stealth")

    assert tuple(match.concept_id for match in matches) == candidates
    assert {(match.start, match.end, match.matched_phrase) for match in matches} == {
        (6, 13, "stealth"),
    }
    assert {match.ambiguity for match in matches} == {candidates}
    assert meaning.unresolved == candidates


def test_duplicate_genitive_markers_compile_to_one_match(fabric: SemanticFabric) -> None:
    meaning = fabric.translate("Vosk's shield", lex_ids=("referent",))
    genitives = [
        match for match in meaning.for_lex("referent") if match.concept_id == "referent.possession.genitive"
    ]

    assert len(genitives) == 1
    assert (genitives[0].matched_phrase, genitives[0].start, genitives[0].end) == ("'s", 4, 6)


def test_match_budget_marks_only_truncated_lex_and_preserves_other_lexes(
    fabric: SemanticFabric,
) -> None:
    text = " ".join(["in"] * 129 + ["I", "attack", "Vosk."])
    meaning = fabric.translate(text)

    assert len(meaning.for_lex("scene")) == 128
    assert meaning.unresolved == ("semantic_fabric.match_budget_exceeded.scene",)
    assert "action.weapon_attack" in meaning.concepts("action")
    assert "skill.brawl" in meaning.concepts("capability")


def test_false_friend_suppresses_only_the_guarded_sense(fabric: SemanticFabric) -> None:
    meaning = fabric.translate("I strike a bargain with the captain.")
    assert "action.weapon_attack" not in meaning.concepts("action")
    assert "referent.title.role" in meaning.concepts("referent")


@pytest.mark.parametrize(
    "text",
    (
        "I cut a deal with the captain.",
        "I cut the deal with the captain.",
        "I cut another dangerous deal with the captain.",
        "I attack the problem from another angle.",
        "I attack this difficult problem from another angle.",
        "I hit the open road with the captain.",
    ),
)
def test_action_false_friends_do_not_become_weapon_attacks(
    fabric: SemanticFabric,
    text: str,
) -> None:
    meaning = fabric.translate(text, lex_ids=("action",))
    assert "action.weapon_attack" not in meaning.concepts("action")


def test_false_friend_does_not_suppress_a_literal_weapon_attack(
    fabric: SemanticFabric,
) -> None:
    meaning = fabric.translate("I attack the bandit with my sword.")
    assert "action.weapon_attack" in meaning.concepts("action")


def test_compiled_meaning_is_deterministic_and_strict(fabric: SemanticFabric) -> None:
    left = fabric.translate("I inspect the shield rim.").as_dict()
    right = fabric.translate("I inspect the shield rim.").as_dict()
    assert left == right
    assert left["schema"] == MEANING_SCHEMA
    assert validate_compiled_meaning(left) == left

    forged = json.loads(json.dumps(left))
    forged["matches"][0]["authorized"] = True
    payload = {key: forged[key] for key in forged if key != "fingerprint"}
    forged["fingerprint"] = content_fingerprint(payload)
    with pytest.raises(SemanticFabricError, match="overclaims authority"):
        validate_compiled_meaning(forged)


def test_content_free_meaning_receipt_validates_and_rejects_forgery(
    fabric: SemanticFabric,
) -> None:
    meaning = fabric.translate("I inspect the shield rim.")
    receipt = meaning.receipt_dict()

    assert receipt["schema"] == MEANING_RECEIPT_SCHEMA
    assert receipt["source_fingerprint"] == meaning.source_fingerprint
    assert receipt["fabric_fingerprint"] == meaning.fabric_fingerprint
    assert all("matched_phrase" not in row for row in receipt["matches"])
    for match, row in zip(meaning.matches, receipt["matches"], strict=True):
        expected = match.as_dict()
        expected.pop("matched_phrase")
        assert row == expected
    assert validate_compiled_meaning_receipt(receipt) == receipt

    tampered = json.loads(json.dumps(receipt))
    tampered["matches"][0]["end"] += 1
    with pytest.raises(SemanticFabricError, match="receipt fingerprint mismatch"):
        validate_compiled_meaning_receipt(tampered)

    forged = json.loads(json.dumps(receipt))
    forged["matches"][0]["authorized"] = True
    payload = {key: forged[key] for key in forged if key != "fingerprint"}
    forged["fingerprint"] = content_fingerprint(payload)
    with pytest.raises(SemanticFabricError, match="overclaims authority"):
        validate_compiled_meaning_receipt(forged)


def test_compiled_meaning_helpers_preserve_cross_lex_order(fabric: SemanticFabric) -> None:
    meaning = fabric.translate("I inspect his shield.")
    assert isinstance(meaning, CompiledMeaning)
    assert meaning.for_lex("action")
    assert meaning.for_lex("referent")
    assert "action.inspect" in meaning.concepts("action")


def test_unknown_genre_or_lex_never_falls_back_to_guess(fabric: SemanticFabric) -> None:
    with pytest.raises(SemanticFabricError, match="unknown semantic-fabric genre"):
        fabric.translate("inspect", genre_ids=("invented_genre",))
    with pytest.raises(SemanticFabricError, match="unknown semantic-fabric Lex"):
        fabric.translate("inspect", lex_ids=("invented_lex",))


def test_manifest_hash_rejects_public_corpus_tamper(tmp_path: Path) -> None:
    copied = _copy_sealed_corpora(tmp_path)
    pack = copied / "action-lex.json"
    pack.write_bytes(pack.read_bytes().replace(b'"inspection"', b'"inspectiox"', 1))
    with pytest.raises(SemanticFabricError, match="hash mismatch"):
        SemanticFabric.load(copied)


def test_resealed_pack_still_cannot_claim_authority(tmp_path: Path) -> None:
    copied = _copy_sealed_corpora(tmp_path)
    relative = "action-lex.json"
    pack_path = copied / relative
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    pack["authority"] = "authorized"
    payload = {key: pack[key] for key in pack if key != "fingerprint"}
    pack["fingerprint"] = content_fingerprint(payload)
    pack_path.write_text(
        json.dumps(pack, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _rewrite_manifest(copied, relative)
    with pytest.raises(SemanticFabricError, match="recognition authority"):
        SemanticFabric.load(copied)


def test_builder_is_deterministic_and_default_loader_is_repo_local(tmp_path: Path) -> None:
    copied_root = tmp_path / "repo"
    (copied_root / "corpus").mkdir(parents=True)
    shutil.copytree(ROOT / "corpus" / "semantic-fabric", copied_root / "corpus" / "semantic-fabric")
    shutil.copytree(
        ROOT / "corpus" / "capability-glossary",
        copied_root / "corpus" / "capability-glossary",
    )
    before = {
        path.name: path.read_bytes() for path in (copied_root / "corpus" / "semantic-fabric").glob("*.json")
    }
    build_semantic_fabric(copied_root)
    after = {
        path.name: path.read_bytes() for path in (copied_root / "corpus" / "semantic-fabric").glob("*.json")
    }
    assert after == before
    assert load_default_semantic_fabric(ROOT).fingerprint == fabric_fingerprint()


def test_builder_rejects_unknown_expansion_fields_instead_of_silently_dropping_them(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "repo"
    (copied_root / "corpus").mkdir(parents=True)
    shutil.copytree(CORPUS, copied_root / "corpus" / "semantic-fabric")
    shutil.copytree(CAPABILITY_CORPUS, copied_root / "corpus" / "capability-glossary")
    expansion = {
        "schema": "semantic-fabric-genre-expansions/1",
        "sources": [],
        "packs": {
            "referent": {
                "entries": [],
                "constructions": [],
                "ambiguities": [],
                "entrys": [],
            },
        },
    }
    expansion_path = copied_root / "corpus" / "semantic-fabric" / "source" / "genre-expansions.json"
    expansion_path.write_text(json.dumps(expansion), encoding="utf-8")

    with pytest.raises(ValueError, match="referent expansion fields do not match v1"):
        build_semantic_fabric(copied_root)


def fabric_fingerprint() -> str:
    capability_lex = CapabilityGlossary.load(CAPABILITY_CORPUS)
    return SemanticFabric.load(CORPUS, capability_glossary=capability_lex).fingerprint
