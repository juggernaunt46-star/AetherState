from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import pytest

from aetherstate.capability_glossary import CapabilityGlossary
from aetherstate.worldlex import AdapterContract, DefinitionRef, OwnerRef, SubjectRef
from aetherstate.worldlex_assignment import (
    ADAPTER_PAYLOAD_SCHEMA,
    ASSIGNMENT_SCHEMA,
    AssignmentAdapterError,
    AssignmentResolutionError,
    AssignmentScopeError,
    AssignmentValidationError,
    materialize_assignment,
    validate_assignment,
)
from aetherstate.worldlex_store import WorldLexStore


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "capability-glossary"
WORLD_A = "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WORLD_B = "world_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SHA_UNKNOWN = "sha256:" + "0" * 64


@pytest.fixture(scope="module")
def glossary() -> CapabilityGlossary:
    return CapabilityGlossary.load(CORPUS)


def _record(
    glossary: CapabilityGlossary,
    *,
    world_id: str = WORLD_A,
    definition_id: str = "shadow_practice",
    revision: int = 1,
    parent_fingerprint: str | None = None,
    owner_scope: str = "world",
    owner_id: str | None = None,
    name: str = "Shadow Practice",
    concept_id: str = "skill.stealth",
    description: str = "Patient movement through watched spaces.",
) -> dict:
    draft = {
        "kind": "skill",
        "name": name,
        "definition_id": definition_id,
        "revision": revision,
        "world_id": world_id,
        "owner_scope": owner_scope,
        "owner_id": owner_id if owner_id is not None else world_id,
        "concept_ids": [concept_id],
        "grounding_evidence": ["Creator-authored training and world evidence"],
        "description": description,
    }
    if parent_fingerprint is not None:
        draft["parent_fingerprint"] = parent_fingerprint
    return glossary.freeze_definition(draft)


def _ref(record: dict, **changes) -> DefinitionRef:
    values = {
        "definition_schema": record["schema"],
        "definition_id": record["definition_id"],
        "revision": record["revision"],
        "fingerprint": record["fingerprint"],
        "world_id": record["world_id"],
        "kind": record["kind"],
        "owner": OwnerRef(
            kind=record["owner_scope"],
            id=record["owner_id"],
            world_id=record["world_id"],
        ),
    }
    values.update(changes)
    return DefinitionRef(**values)


def _adapter() -> AdapterContract:
    return AdapterContract.create(
        adapter_id="custom-skill-adapter/1",
        receipt_id="skill-check/1",
        definition_schemas=["capability-definition/1"],
        definition_kinds=["skill"],
        consumed_fields=["concept_ids", "description", "mastery"],
        concept_ids=["skill.stealth"],
    )


def _setup(glossary: CapabilityGlossary) -> tuple[sqlite3.Connection, WorldLexStore, dict]:
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    record = _record(glossary)
    repository.append_definition(record)
    return connection, repository, record


def test_materialization_is_deterministic_journal_ready_and_non_executable(
    glossary: CapabilityGlossary,
):
    connection, repository, record = _setup(glossary)
    subject = SubjectRef("actor", "actor.scout", WORLD_A)
    contract = _adapter()
    try:
        first = materialize_assignment(
            repository,
            definition=_ref(record),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
            adapter_contract=contract,
        )
        second = materialize_assignment(
            repository,
            definition=_ref(record),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
            adapter_contract=contract,
        )
        payload = first.as_dict()

        assert first == second
        assert payload == second.as_dict()
        assert payload["schema"] == ASSIGNMENT_SCHEMA
        assert payload["assignment_id"].startswith("assignment_")
        assert payload["recognized"] is True
        assert payload["authorized"] is True
        assert payload["executable"] is False
        assert payload["adapter_payload"]["schema"] == ADAPTER_PAYLOAD_SCHEMA
        assert payload["adapter_payload"]["receipt_admitted"] is False
        assert payload["adapter_payload"]["executable"] is False
        assert validate_assignment(payload) == first
    finally:
        connection.close()


def test_materialized_snapshots_are_detached_from_every_caller_value(
    glossary: CapabilityGlossary,
):
    connection, repository, record = _setup(glossary)
    contract_payload = _adapter().as_dict()
    original_description = record["description"]
    try:
        assignment = materialize_assignment(
            repository,
            definition=_ref(record).as_dict(),
            subject=SubjectRef("actor", "actor.scout", WORLD_A).as_dict(),
            acquisition_source="earned.quest_7",
            acquisition_turn=14,
            adapter_contract=contract_payload,
        )
        record["description"] = "caller changed the compiler record"
        contract_payload["consumed_fields"].clear()
        detached = assignment.as_dict()
        detached["definition_snapshot"]["description"] = "caller changed the journal copy"
        detached["adapter_payload"]["consumed_fields"]["description"] = "forged"

        assert assignment.definition_snapshot["description"] == original_description
        assert assignment.adapter_contract_snapshot["consumed_fields"] == [
            "concept_ids",
            "description",
            "mastery",
        ]
        assert assignment.adapter_payload["consumed_fields"]["description"] == original_description
        validate_assignment(assignment.as_dict())
    finally:
        connection.close()


def test_missing_wrong_revision_fingerprint_and_world_are_rejected(
    glossary: CapabilityGlossary,
):
    connection, repository, record = _setup(glossary)
    repository.ensure_world_lineage(WORLD_B)
    subject_a = SubjectRef("actor", "actor.scout", WORLD_A)
    try:
        missing = DefinitionRef(
            definition_schema=record["schema"],
            definition_id="missing_skill",
            revision=1,
            fingerprint=SHA_UNKNOWN,
            world_id=WORLD_A,
            kind="skill",
            owner=OwnerRef("world", WORLD_A, WORLD_A),
        )
        with pytest.raises(AssignmentResolutionError, match="does not exist"):
            materialize_assignment(
                repository,
                definition=missing,
                subject=subject_a,
                acquisition_source="creator",
                acquisition_turn=0,
            )

        wrong_revision = _ref(record, revision=2)
        with pytest.raises(AssignmentResolutionError, match="different id or revision"):
            materialize_assignment(
                repository,
                definition=wrong_revision,
                subject=subject_a,
                acquisition_source="creator",
                acquisition_turn=0,
            )

        wrong_fingerprint = _ref(record, fingerprint=SHA_UNKNOWN)
        with pytest.raises(AssignmentResolutionError, match="requested fingerprint"):
            materialize_assignment(
                repository,
                definition=wrong_fingerprint,
                subject=subject_a,
                acquisition_source="creator",
                acquisition_turn=0,
            )

        wrong_world = _ref(
            record,
            world_id=WORLD_B,
            owner=OwnerRef("world", WORLD_B, WORLD_B),
        )
        with pytest.raises(AssignmentScopeError, match="different world"):
            materialize_assignment(
                repository,
                definition=wrong_world,
                subject=SubjectRef("actor", "actor.scout", WORLD_B),
                acquisition_source="creator",
                acquisition_turn=0,
            )
    finally:
        connection.close()


def test_subject_and_definition_owner_must_match_exact_scope(glossary: CapabilityGlossary):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    actor_record = _record(
        glossary,
        definition_id="scout_practice",
        owner_scope="actor",
        owner_id="actor.scout",
    )
    enemy_record = _record(
        glossary,
        definition_id="sentinel_practice",
        owner_scope="enemy_blueprint",
        owner_id="enemy_blueprint.sentinel",
    )
    repository.append_definition(actor_record)
    repository.append_definition(enemy_record)
    try:
        with pytest.raises(AssignmentScopeError, match="exact owner subject"):
            materialize_assignment(
                repository,
                definition=_ref(actor_record),
                subject=SubjectRef("actor", "actor.other", WORLD_A),
                acquisition_source="creator",
                acquisition_turn=0,
            )
        with pytest.raises(AssignmentScopeError, match="exact owner subject"):
            materialize_assignment(
                repository,
                definition=_ref(enemy_record),
                subject=SubjectRef("enemy_blueprint", "enemy_blueprint.raider", WORLD_A),
                acquisition_source="creator",
                acquisition_turn=0,
            )

        forged_owner = _ref(
            actor_record,
            owner=OwnerRef("actor", "actor.other", WORLD_A),
        )
        with pytest.raises(AssignmentResolutionError, match="does not exactly match"):
            materialize_assignment(
                repository,
                definition=forged_owner,
                subject=SubjectRef("actor", "actor.other", WORLD_A),
                acquisition_source="creator",
                acquisition_turn=0,
            )

        accepted = materialize_assignment(
            repository,
            definition=_ref(actor_record),
            subject=SubjectRef("actor", "actor.scout", WORLD_A),
            acquisition_source="creator",
            acquisition_turn=0,
        )
        assert accepted.subject.id == "actor.scout"
    finally:
        connection.close()


def test_adapter_contract_and_payload_forgery_are_rejected(glossary: CapabilityGlossary):
    connection, repository, record = _setup(glossary)
    subject = SubjectRef("actor", "actor.scout", WORLD_A)
    try:
        contract = _adapter()
        assignment = materialize_assignment(
            repository,
            definition=_ref(record),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
            adapter_contract=contract,
        )

        forged_contract = contract.as_dict()
        forged_contract["receipt_id"] = "different-receipt/1"
        with pytest.raises(AssignmentAdapterError, match="invalid or forged"):
            materialize_assignment(
                repository,
                definition=_ref(record),
                subject=subject,
                acquisition_source="creator",
                acquisition_turn=0,
                adapter_contract=forged_contract,
            )

        forged_payload = assignment.as_dict()
        forged_payload["adapter_payload"]["consumed_fields"]["description"] = "forged"
        with pytest.raises(AssignmentAdapterError, match="does not match"):
            validate_assignment(forged_payload)

        missing_payload = assignment.as_dict()
        missing_payload["adapter_payload"] = None
        with pytest.raises(AssignmentAdapterError, match="requires"):
            validate_assignment(missing_payload)

        forged_execution = assignment.as_dict()
        forged_execution["executable"] = True
        with pytest.raises(AssignmentValidationError, match="never claims execution"):
            validate_assignment(forged_execution)
    finally:
        connection.close()


def test_adapter_must_cover_the_exact_definition_shape_and_concepts(
    glossary: CapabilityGlossary,
):
    connection, repository, record = _setup(glossary)
    subject = SubjectRef("actor", "actor.scout", WORLD_A)
    try:
        wrong_kind = AdapterContract.create(
            adapter_id="spell-adapter/1",
            receipt_id="spell-check/1",
            definition_schemas=["capability-definition/1"],
            definition_kinds=["spell"],
            consumed_fields=["description"],
        )
        with pytest.raises(AssignmentAdapterError, match="kind is outside"):
            materialize_assignment(
                repository,
                definition=_ref(record),
                subject=subject,
                acquisition_source="creator",
                acquisition_turn=0,
                adapter_contract=wrong_kind,
            )

        wrong_concept = AdapterContract.create(
            adapter_id="navigation-adapter/1",
            receipt_id="navigation-check/1",
            definition_schemas=["capability-definition/1"],
            definition_kinds=["skill"],
            consumed_fields=["description"],
            concept_ids=["skill.navigation"],
        )
        with pytest.raises(AssignmentAdapterError, match="coverage is absent"):
            materialize_assignment(
                repository,
                definition=_ref(record),
                subject=subject,
                acquisition_source="creator",
                acquisition_turn=0,
                adapter_contract=wrong_concept,
            )
    finally:
        connection.close()


def test_reopen_is_idempotent_and_old_revision_is_retained(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "assignment.sqlite3"
    first_connection = sqlite3.connect(path)
    first_repository = WorldLexStore(first_connection)
    first_repository.ensure_world_lineage(WORLD_A)
    revision_one = _record(glossary)
    first_repository.append_definition(revision_one)
    subject = SubjectRef("actor", "actor.scout", WORLD_A)
    first_assignment = materialize_assignment(
        first_repository,
        definition=_ref(revision_one),
        subject=subject,
        acquisition_source="creator",
        acquisition_turn=0,
    )
    first_connection.close()

    reopened_connection = sqlite3.connect(path)
    reopened = WorldLexStore(reopened_connection)
    try:
        reopened_assignment = materialize_assignment(
            reopened,
            definition=_ref(revision_one),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
        )
        assert reopened_assignment.as_dict() == first_assignment.as_dict()

        revision_two = _record(
            glossary,
            revision=2,
            parent_fingerprint=revision_one["fingerprint"],
            description="A refined practice with tighter timing.",
        )
        reopened.append_definition(revision_two)
        retained_old = materialize_assignment(
            reopened,
            definition=_ref(revision_one),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
        )
        newer = materialize_assignment(
            reopened,
            definition=_ref(revision_two),
            subject=subject,
            acquisition_source="progression",
            acquisition_turn=8,
        )
        assert retained_old.as_dict() == first_assignment.as_dict()
        assert retained_old.definition_snapshot["revision"] == 1
        assert newer.definition_snapshot["revision"] == 2
        assert newer.assignment_id != retained_old.assignment_id
    finally:
        reopened_connection.close()

    # Replay remains complete after the repository is closed and cannot consult "latest".
    assert validate_assignment(first_assignment.as_dict()) == first_assignment


def test_noncombat_institution_assignment_has_no_combat_assumptions(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    inquiry = _record(
        glossary,
        definition_id="harbor_inquiry",
        name="Harbor Inquiry",
        concept_id="skill.investigation",
        description="Audit manifests, interview witnesses, and reconcile missing cargo.",
    )
    repository.append_definition(inquiry)
    try:
        assignment = materialize_assignment(
            repository,
            definition=_ref(inquiry),
            subject=SubjectRef("institution", "institution.harbor_authority", WORLD_A),
            acquisition_source="world_genesis",
            acquisition_turn=0,
        )
        payload = assignment.as_dict()
        assert payload["subject"]["kind"] == "institution"
        assert payload["definition_snapshot"]["concept_ids"] == ["skill.investigation"]
        assert payload["authorized"] is True
        assert payload["executable"] is False
        serialized = repr(payload).casefold()
        for combat_field in ("damage", "hp", "moves", "pending_intent", "kit_size"):
            assert combat_field not in serialized
    finally:
        connection.close()


def test_assignment_identity_changes_with_acquisition_provenance(glossary: CapabilityGlossary):
    connection, repository, record = _setup(glossary)
    subject = SubjectRef("actor", "actor.scout", WORLD_A)
    try:
        creator = materialize_assignment(
            repository,
            definition=_ref(record),
            subject=subject,
            acquisition_source="creator",
            acquisition_turn=0,
        )
        earned = materialize_assignment(
            repository,
            definition=_ref(record),
            subject=subject,
            acquisition_source="earned.quest_7",
            acquisition_turn=14,
        )
        assert creator.assignment_id != earned.assignment_id
        assert creator.fingerprint != earned.fingerprint

        forged = copy.deepcopy(creator.as_dict())
        forged["acquisition_turn"] = 14
        with pytest.raises(AssignmentValidationError, match="assignment_id"):
            validate_assignment(forged)
    finally:
        connection.close()
