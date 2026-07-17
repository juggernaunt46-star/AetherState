from __future__ import annotations

import copy
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aetherstate.capability_glossary import CapabilityGlossary, content_fingerprint
from aetherstate.store import Store
from aetherstate.worldlex_store import (
    CrossWorldDefinitionError,
    DefinitionConflictError,
    DefinitionLineageError,
    DefinitionValidationError,
    WorldIdentityError,
    WorldLexStore,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "capability-glossary"
WORLD_A = "world_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WORLD_B = "world_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WORLD_C = "world_cccccccccccccccccccccccccccccccc"


@pytest.fixture(scope="module")
def glossary() -> CapabilityGlossary:
    return CapabilityGlossary.load(CORPUS)


def _definition(
    glossary: CapabilityGlossary,
    *,
    world_id: str = WORLD_A,
    definition_id: str = "shadow_practice",
    revision: int = 1,
    parent_fingerprint: str | None = None,
    owner_scope: str = "world",
    owner_id: str | None = None,
    description: str = "Patient movement through watched spaces.",
) -> dict:
    draft = {
        "kind": "skill",
        "name": "Shadow Practice",
        "definition_id": definition_id,
        "revision": revision,
        "world_id": world_id,
        "owner_scope": owner_scope,
        "owner_id": owner_id if owner_id is not None else world_id,
        "concept_ids": ["skill.stealth"],
        "grounding_evidence": ["Creator-authored stealth training"],
        "description": description,
    }
    if parent_fingerprint is not None:
        draft["parent_fingerprint"] = parent_fingerprint
    return glossary.freeze_definition(draft)


def _refingerprint(record: dict, **changes) -> dict:
    revised = copy.deepcopy(record)
    revised.update(changes)
    revised.pop("fingerprint", None)
    revised["fingerprint"] = content_fingerprint(revised)
    return revised


def _repository(path: Path) -> tuple[sqlite3.Connection, WorldLexStore]:
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    return connection, WorldLexStore(connection)


def test_composes_with_aetherstate_store_and_preserves_outer_transaction(
    glossary: CapabilityGlossary,
):
    store = Store(":memory:")
    try:
        session = store.get_or_create_session("worldlex-composition")
        repository = WorldLexStore(store.db, store.apply_guard())
        repository.ensure_world_lineage(WORLD_A)
        repository.append_definition(_definition(glossary))

        assert store.get_or_create_session("worldlex-composition")["session_id"] == session["session_id"]

        store.db.execute("BEGIN")
        repository.ensure_world_lineage(WORLD_B)
        assert repository.has_world_lineage(WORLD_B)
        store.db.rollback()
        assert not repository.has_world_lineage(WORLD_B)
        assert repository.get_definition(WORLD_A, "shadow_practice")["revision"] == 1
    finally:
        store.close()


def test_world_ids_are_canonical_stable_and_parented():
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    try:
        assert repository.ensure_world_lineage(WORLD_A) == WORLD_A
        assert repository.ensure_world_lineage(WORLD_A) == WORLD_A

        minted = repository.ensure_world_lineage(parent_world_id=WORLD_A)
        assert re.fullmatch(r"world_[0-9a-f]{32}", minted)
        assert repository.ensure_world_lineage(minted, parent_world_id=WORLD_A) == minted
        assert repository.get_world_lineage(minted).parent_world_id == WORLD_A

        with pytest.raises(WorldIdentityError, match="already has parent"):
            repository.ensure_world_lineage(minted)
        with pytest.raises(WorldIdentityError, match="unknown parent"):
            repository.ensure_world_lineage(WORLD_B, parent_world_id=WORLD_C)
        assert not repository.has_world_lineage(WORLD_B)

        with pytest.raises(WorldIdentityError, match="32 lowercase hex"):
            repository.ensure_world_lineage("blackglass")
        with pytest.raises(WorldIdentityError, match="own parent"):
            repository.ensure_world_lineage(WORLD_C, parent_world_id=WORLD_C)
    finally:
        connection.close()


def test_definition_reopens_with_exact_lineage_and_detached_snapshots(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "worldlex.sqlite3"
    connection, repository = _repository(path)
    repository.ensure_world_lineage(WORLD_A)
    first = _definition(glossary)
    admitted = repository.append_definition(first)
    assert admitted.inserted

    first["description"] = "caller mutation"
    admitted.record["description"] = "result mutation"
    first_fingerprint = admitted.record["fingerprint"]
    connection.close()

    reopened_connection, reopened = _repository(path)
    try:
        stored_first = reopened.get_definition(WORLD_A, "shadow_practice", 1)
        assert stored_first["description"] == "Patient movement through watched spaces."
        assert reopened.get_by_fingerprint(first_fingerprint) == stored_first

        second = _definition(
            glossary,
            revision=2,
            parent_fingerprint=first_fingerprint,
            description="A refined practice with tighter timing.",
        )
        assert reopened.append_definition(second).inserted
        assert reopened.get_definition(WORLD_A, "shadow_practice") == second
        assert [row["revision"] for row in reopened.list_revisions(WORLD_A, "shadow_practice")] == [
            1,
            2,
        ]
    finally:
        reopened_connection.close()


def test_exact_duplicate_is_idempotent_but_changed_revision_conflicts(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    record = _definition(glossary)
    try:
        assert repository.append_definition(record).inserted
        duplicate = repository.append_definition(copy.deepcopy(record))
        assert not duplicate.inserted
        assert duplicate.record == record

        changed = _refingerprint(record, description="Different immutable content.")
        with pytest.raises(DefinitionConflictError, match="already immutable"):
            repository.append_definition(changed)
        assert repository.list_revisions(WORLD_A, "shadow_practice") == [record]
    finally:
        connection.close()


def test_bad_fingerprint_and_unknown_world_are_rejected_without_writes(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    try:
        record = _definition(glossary)
        bad_fingerprint = dict(record, fingerprint="sha256:" + "0" * 64)
        with pytest.raises(DefinitionValidationError, match="does not match"):
            repository.append_definition(bad_fingerprint)

        unknown_world = _definition(glossary, world_id=WORLD_B)
        with pytest.raises(WorldIdentityError, match="unknown world"):
            repository.append_definition(unknown_world)

        assert repository.list_revisions(WORLD_A, "shadow_practice") == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"schema": "capability-definition/2"}, "schema must be"),
        ({"compiler_version": "capability-compiler/2"}, "compiler_version must be"),
        ({"kind": "enemy_kit"}, "kind must be"),
        ({"owner_scope": "faction"}, "owner_scope must be"),
        ({"owner_id": "someone_else"}, "world-scoped"),
        ({"owner_id": ""}, "non-empty, trimmed"),
    ],
)
def test_definition_envelope_validation(
    glossary: CapabilityGlossary,
    changes: dict,
    error: str,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    try:
        invalid = _refingerprint(_definition(glossary), **changes)
        with pytest.raises(DefinitionValidationError, match=error):
            repository.append_definition(invalid)
        assert repository.list_revisions(WORLD_A, "shadow_practice") == []
    finally:
        connection.close()


def test_revision_must_be_exact_next_with_existing_immediate_parent(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    first = _definition(glossary)
    repository.append_definition(first)
    try:
        skipped = _definition(
            glossary,
            revision=3,
            parent_fingerprint=first["fingerprint"],
        )
        with pytest.raises(DefinitionLineageError, match="exact next revision 2"):
            repository.append_definition(skipped)

        missing_parent = _definition(
            glossary,
            revision=2,
            parent_fingerprint="sha256:" + "1" * 64,
        )
        with pytest.raises(DefinitionLineageError, match="does not exist"):
            repository.append_definition(missing_parent)

        other = _definition(glossary, definition_id="other_skill")
        repository.append_definition(other)
        wrong_definition = _definition(
            glossary,
            revision=2,
            parent_fingerprint=other["fingerprint"],
        )
        with pytest.raises(DefinitionLineageError, match="another definition"):
            repository.append_definition(wrong_definition)

        second = _definition(
            glossary,
            revision=2,
            parent_fingerprint=first["fingerprint"],
        )
        repository.append_definition(second)
        stale_parent = _definition(
            glossary,
            revision=3,
            parent_fingerprint=first["fingerprint"],
        )
        with pytest.raises(DefinitionLineageError, match="immediately previous"):
            repository.append_definition(stale_parent)
        assert [row["revision"] for row in repository.list_revisions(WORLD_A, "shadow_practice")] == [
            1,
            2,
        ]
    finally:
        connection.close()


def test_cross_world_definition_links_are_rejected_transactionally(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    repository.ensure_world_lineage(WORLD_B, parent_world_id=WORLD_A)
    first_a = _definition(glossary, world_id=WORLD_A)
    first_b = _definition(glossary, world_id=WORLD_B)
    repository.append_definition(first_a)
    repository.append_definition(first_b)
    try:
        crossing = _definition(
            glossary,
            world_id=WORLD_B,
            revision=2,
            parent_fingerprint=first_a["fingerprint"],
        )
        with pytest.raises(CrossWorldDefinitionError, match="belongs to world"):
            repository.append_definition(crossing)

        candidate = _definition(glossary, world_id=WORLD_B, definition_id="second_skill")
        with pytest.raises(CrossWorldDefinitionError, match="does not match expected world"):
            repository.append_definition(candidate, expected_world_id=WORLD_A)

        assert [row["revision"] for row in repository.list_revisions(WORLD_B, "shadow_practice")] == [
            1
        ]
        assert repository.get_definition(WORLD_B, "second_skill") is None
    finally:
        connection.close()


def test_kind_and_owner_are_continuous_across_revisions(glossary: CapabilityGlossary):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    first = _definition(
        glossary,
        owner_scope="enemy_blueprint",
        owner_id="enemy_blueprint_sentinel",
    )
    repository.append_definition(first)
    base_second = _definition(
        glossary,
        revision=2,
        parent_fingerprint=first["fingerprint"],
        owner_scope="enemy_blueprint",
        owner_id="enemy_blueprint_sentinel",
    )
    try:
        changed_kind = _refingerprint(base_second, kind="ability")
        with pytest.raises(DefinitionLineageError, match="kind cannot change"):
            repository.append_definition(changed_kind)

        changed_owner = _refingerprint(base_second, owner_id="enemy_blueprint_raider")
        with pytest.raises(DefinitionLineageError, match="owner_scope and owner_id"):
            repository.append_definition(changed_owner)

        assert repository.append_definition(base_second).inserted
    finally:
        connection.close()


def test_sqlite_guards_reject_raw_mutation_and_replacement(glossary: CapabilityGlossary):
    connection = sqlite3.connect(":memory:")
    repository = WorldLexStore(connection)
    repository.ensure_world_lineage(WORLD_A)
    record = _definition(glossary)
    repository.append_definition(record)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="definition revisions are immutable"):
            connection.execute(
                "UPDATE worldlex_capability_definitions SET owner_id = ? WHERE fingerprint = ?",
                ("mutated", record["fingerprint"]),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="definition revisions are immutable"):
            connection.execute(
                "DELETE FROM worldlex_capability_definitions WHERE fingerprint = ?",
                (record["fingerprint"],),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="world identities are immutable"):
            connection.execute(
                "UPDATE worldlex_world_lineages SET parent_world_id = ? WHERE world_id = ?",
                (WORLD_B, WORLD_A),
            )
        connection.rollback()
        assert repository.get_definition(WORLD_A, "shadow_practice") == record
    finally:
        connection.close()


def _concurrent_appends(path: Path, records: list[dict]) -> list[tuple[str, bool | str]]:
    barrier = threading.Barrier(len(records))

    def append(record: dict) -> tuple[str, bool | str]:
        connection, repository = _repository(path)
        try:
            barrier.wait(timeout=10)
            try:
                result = repository.append_definition(record)
            except Exception as exc:  # The concrete exception is asserted by the caller.
                return type(exc).__name__, str(exc)
            return "ok", result.inserted
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=len(records)) as executor:
        return list(executor.map(append, records))


def test_concurrent_exact_insert_is_idempotent_and_conflict_has_one_winner(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "concurrent.sqlite3"
    setup_connection, setup = _repository(path)
    setup.ensure_world_lineage(WORLD_A)
    setup_connection.close()

    exact = _definition(glossary, definition_id="concurrent_exact")
    exact_results = _concurrent_appends(path, [exact, copy.deepcopy(exact)])
    assert sorted(exact_results) == [("ok", False), ("ok", True)]

    winner_a = _definition(
        glossary,
        definition_id="concurrent_conflict",
        description="Candidate A",
    )
    winner_b = _definition(
        glossary,
        definition_id="concurrent_conflict",
        description="Candidate B",
    )
    conflict_results = _concurrent_appends(path, [winner_a, winner_b])
    assert sum(result == ("ok", True) for result in conflict_results) == 1
    assert sum(result[0] == "DefinitionConflictError" for result in conflict_results) == 1

    verify_connection, verify = _repository(path)
    try:
        assert len(verify.list_revisions(WORLD_A, "concurrent_exact")) == 1
        assert len(verify.list_revisions(WORLD_A, "concurrent_conflict")) == 1
    finally:
        verify_connection.close()
