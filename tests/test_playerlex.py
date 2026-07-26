from __future__ import annotations

import copy
import json
import sqlite3
import threading
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest

from aetherstate.capability_glossary import CapabilityGlossary
from aetherstate.database_schema import database_schema_migrations
from aetherstate.schema_migrations import (
    TRANSACTION_ACTIVE,
    SchemaMigrationError,
    SchemaMigrationRunner,
    _PENDING_CLEANUP,
    _PENDING_CLEANUP_SCHEMA_SQL,
)
from aetherstate.semantic_atlas import SemanticAtlas, load_default_semantic_atlas
from aetherstate.playerlex import (
    PlayerLex,
    PlayerLexConflictError,
    PlayerLexError,
    PlayerLexNotFoundError,
    PlayerLexRetryableRemovalError,
    PlayerLexValidationError,
    _SCHEMA_STATEMENTS,
    _V1_ROW_COLUMNS,
    _V1_SCHEMA_STATEMENTS,
    _folded_text_with_source_spans,
    playerlex_schema_migrations,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus" / "capability-glossary"
PC_CONNECTORS = "_\u203f\u2040\u2054\ufe33\ufe34\ufe4d\ufe4e\ufe4f\uff3f"


@pytest.fixture(scope="module")
def glossary() -> CapabilityGlossary:
    return CapabilityGlossary.load(CORPUS)


@pytest.fixture(scope="module")
def atlas() -> SemanticAtlas:
    return load_default_semantic_atlas()


@pytest.fixture()
def playerlex(glossary: CapabilityGlossary):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    service = PlayerLex(connection, glossary)
    try:
        yield connection, service
    finally:
        connection.close()


def _v1_row_from_current_entry(
    connection: sqlite3.Connection,
    glossary: CapabilityGlossary,
    entry_id: str,
    *,
    raw_entry_id: object | None = None,
) -> tuple[str, dict[str, object]]:
    row = connection.execute(
        "SELECT storage_token, entry_id, kind, surface, normalized_surface, concept_id, "
        "meaning_fingerprint, approval_revision, approved_at, created_at, updated_at, record_json "
        "FROM playerlex_entries WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    record = json.loads(row["record_json"])
    record["schema"] = "playerlex-entry/1"
    record.pop("lex_id")
    record["concept"] = glossary.concept_classification(row["concept_id"])
    values: dict[str, object] = {
        "entry_id": row["entry_id"] if raw_entry_id is None else raw_entry_id,
        "schema": "playerlex-entry/1",
        "kind": row["kind"],
        "surface": row["surface"],
        "normalized_surface": row["normalized_surface"],
        "concept_id": row["concept_id"],
        "meaning_fingerprint": row["meaning_fingerprint"],
        "approval_revision": row["approval_revision"],
        "approved_at": row["approved_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "record_json": json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    return row["storage_token"], values


def test_playerlex_initialization_records_only_version_5(glossary: CapabilityGlossary):
    """PlayerLex claims only its deferred migration after core stays absent."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        PlayerLex(connection, glossary)
        assert tuple(connection.execute(
            "SELECT version FROM aetherstate_schema_migrations ORDER BY version"
        )) == ((5,),)
    finally:
        connection.close()


def test_optional_migration_failure_preserves_secure_delete_and_rolls_back_rebuild(
    glossary: CapabilityGlossary,
):
    """A failed deferred migration must leave no ledger claim or partial PlayerLex rebuild."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute(_V1_SCHEMA_STATEMENTS[0])
        connection.execute(_V1_SCHEMA_STATEMENTS[1])
        for statement in _V1_SCHEMA_STATEMENTS[2:]:
            connection.execute(statement)
        connection.commit()
        migration = next(
            item for item in database_schema_migrations() if item.version == 5
        )

        def fail_after_rebuild(candidate: sqlite3.Connection) -> None:
            migration.transform(candidate)
            raise RuntimeError("synthetic deferred migration interruption")

        runner = SchemaMigrationRunner(
            connection,
            threading.RLock(),
            tuple(
                replace(item, transform=fail_after_rebuild) if item.version == 5 else item
                for item in database_schema_migrations()
            ),
        )
        with pytest.raises(PlayerLexError, match="failed verification"):
            PlayerLex(connection, glossary, migrations=runner)
        assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert "lex_id" not in [row[1] for row in connection.execute("PRAGMA table_info(playerlex_entries)")]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
    finally:
        connection.close()


def test_explicit_alias_proposes_exact_span_without_authority_or_input_persistence(playerlex):
    connection, service = playerlex
    approved = service.approve(
        kind="alias",
        surface="Ghost Step",
        concept_id="skill.stealth",
    )

    assert approved["schema"] == "playerlex-entry/2"
    assert approved["lex_id"] == "capability"
    assert approved["concept"]["lex_id"] == "capability"
    assert approved["status"] == "current"
    assert approved["concept"]["concept_id"] == "skill.stealth"
    assert approved["provenance"] == {
        "approval": "explicit_local",
        "approved_via": "local_control_api",
        "approved_at": approved["provenance"]["approved_at"],
        "approval_revision": 1,
    }

    text = "I use GHOST   STEP before the sentry turns."
    proposal = service.propose(text)
    assert proposal["schema"] == "playerlex-recognition-proposal/1"
    assert proposal["match_count"] == 1
    assert proposal["refused"] == []
    match = proposal["matches"][0]
    start = text.index("GHOST")
    end = start + len("GHOST   STEP")
    assert match["source_span"] == {"start": start, "end": end, "text": text[start:end]}
    assert match["concept"] == approved["concept"]
    assert match["recognized"] is True
    assert match["authorized"] is False
    assert match["executable"] is False
    assert match["requires_context_binding"] is True
    assert "receipt_id" not in match
    assert "assignment" not in match

    stored = connection.execute("SELECT surface, record_json FROM playerlex_entries").fetchone()
    assert stored["surface"] == "Ghost Step"
    assert "before the sentry" not in stored["record_json"]


@pytest.mark.parametrize("kind", ["name", "alias"])
def test_literal_surface_kinds_refuse_substrings(kind: str, playerlex):
    _connection, service = playerlex
    service.approve(kind=kind, surface="veil", concept_id="skill.stealth")

    assert service.propose("The veil falls.")["match_count"] == 1
    assert service.propose("The unveiling begins.")["matches"] == []


def test_literal_boundaries_block_punctuation_wrapped_and_combining_substrings(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="(veil)", concept_id="skill.stealth")
    service.approve(kind="alias", surface="e", concept_id="skill.investigation")

    assert service.propose("Use (veil), then say e.")["match_count"] == 2
    assert service.propose("The un(veil)ing begins.")["matches"] == []
    assert service.propose("The un((veil))ing begins.")["matches"] == []
    assert service.propose("e\u0301")["matches"] == []


def test_literal_boundaries_scan_connectors_and_outward_wrappers(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="veil", concept_id="skill.stealth")
    service.approve(kind="alias", surface="(shroud)", concept_id="skill.investigation")

    for connector in PC_CONNECTORS:
        assert service.propose(f"veil{connector}ing")["matches"] == []
        assert service.propose(f"un{connector}veil")["matches"] == []
    assert service.propose("un((veil))ing")["matches"] == []
    assert service.propose("un((shroud))ing")["matches"] == []
    assert service.propose("Use ((veil)).")["match_count"] == 1


def test_literal_matching_uses_full_casefold_and_preserves_overlapping_source_spans(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="Straße", concept_id="skill.investigation")
    service.approve(kind="alias", surface="a-a", concept_id="skill.stealth")

    text = "Use STRASSE now."
    casefolded = service.propose(text)
    assert casefolded["matches"][0]["source_span"] == {
        "start": 4,
        "end": 11,
        "text": "STRASSE",
    }
    overlapping = service.propose("a-a-a")
    assert [item["source_span"] for item in overlapping["matches"]] == [
        {"start": 0, "end": 3, "text": "a-a"},
        {"start": 2, "end": 5, "text": "a-a"},
    ]


def test_whole_input_nfkc_maps_all_modern_hangul_jamo_to_exact_source_spans(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="가나", concept_id="skill.investigation")

    text = "Use 가나 now."
    proposal = service.propose(text)
    assert proposal["matches"][0]["source_span"] == {
        "start": 4,
        "end": 8,
        "text": "가나",
    }

    for codepoint in range(0xAC00, 0xD7A4):
        syllable = chr(codepoint)
        jamo = unicodedata.normalize("NFD", syllable)
        folded, starts, ends = _folded_text_with_source_spans(jamo)
        assert folded == syllable
        assert starts == [0]
        assert ends == [len(jamo)]


def test_literal_boundaries_are_checked_in_normalized_token_space(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="veil", concept_id="skill.stealth")

    assert unicodedata.normalize("NFKC", "₨") == "Rs"
    assert service.propose("₨veil")["matches"] == []
    assert service.propose("veil₨")["matches"] == []
    assert service.propose("ⓤveil")["matches"] == []


def test_approved_ambiguity_remains_multiple_candidates(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="read the room", concept_id="skill.stealth")
    service.approve(kind="alias", surface="read the room", concept_id="skill.investigation")

    proposal = service.propose("I read the room before speaking.")
    assert [item["concept"]["concept_id"] for item in proposal["matches"]] == [
        "skill.investigation",
        "skill.stealth",
    ]


def test_cross_lex_colliding_ids_are_distinct_approvals_and_proposals(
    atlas: SemanticAtlas,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        service = PlayerLex(connection, atlas)
        capability = service.approve(
            kind="alias",
            surface="Patchwork",
            lex_id="capability",
            concept_id="action.repair",
        )
        action = service.approve(
            kind="alias",
            surface="Patchwork",
            lex_id="action",
            concept_id="action.repair",
        )

        assert capability["concept"] == atlas.meaning("capability", "action.repair")
        assert action["concept"] == atlas.meaning("action", "action.repair")
        assert capability["concept"]["meaning_fingerprint"] != action["concept"]["meaning_fingerprint"]
        listed = service.list_entries()
        assert {(entry["lex_id"], entry["concept"]["concept_id"]) for entry in listed} == {
            ("capability", "action.repair"),
            ("action", "action.repair"),
        }

        proposal = service.propose("Use Patchwork now.")
        assert proposal["match_count"] == 2
        assert {(match["lex_id"], match["concept"]["concept_id"]) for match in proposal["matches"]} == {
            ("capability", "action.repair"),
            ("action", "action.repair"),
        }
        assert all(match["authorized"] is False for match in proposal["matches"])
        with pytest.raises(PlayerLexConflictError):
            service.approve(
                kind="alias",
                surface="patchwork",
                lex_id="action",
                concept_id="action.repair",
            )
    finally:
        connection.close()


def test_catalog_search_paging_alias_genre_and_exact_lookup(atlas: SemanticAtlas):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        service = PlayerLex(connection, atlas)
        cursor = None
        concepts: list[dict[str, object]] = []
        while True:
            page = service.list_concepts(limit=100, cursor=cursor)
            assert page["schema"] == "semantic-atlas-search/1"
            assert len(page["concepts"]) <= 100
            concepts.extend(page["concepts"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(concepts) == 327
        assert len({(row["lex_id"], row["concept_id"]) for row in concepts}) == 327

        alias = service.list_concepts("restore machine", lex_id="capability", limit=10)["concepts"]
        genre = service.list_concepts("fireball blast", lex_id="capability", limit=10)["concepts"]
        assert ("capability", "action.repair") in {(row["lex_id"], row["concept_id"]) for row in alias}
        assert any(row["concept_id"] == "family.direct_pressure" for row in genre)

        exact = service.list_concepts(lex_id="action", concept_id="action.repair")
        assert exact == {
            "schema": "semantic-atlas-search/1",
            "concepts": [atlas.meaning("action", "action.repair")],
            "next_cursor": None,
        }
        with pytest.raises(PlayerLexValidationError, match="between 1 and 100"):
            service.list_concepts(limit=101)
        with pytest.raises(PlayerLexValidationError, match="requires lex_id"):
            service.list_concepts(concept_id="action.repair")
    finally:
        connection.close()


def test_catalog_accepts_the_full_semantic_atlas_cursor_bound(atlas: SemanticAtlas):
    query = chr(0x754C) * 120
    long_cursor = atlas._encode_cursor(1, query=query, lex_id="action")
    assert 512 < len(long_cursor) <= 4096

    class LongCursorAtlas:
        capability_glossary = atlas.capability_glossary

        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def meaning(self, lex_id: str, concept_id: str):
            return atlas.meaning(lex_id, concept_id)

        def search(
            self,
            query: str = "",
            lex_id: str | None = None,
            limit: int = 50,
            cursor: str | None = None,
        ):
            self.calls.append(cursor)
            if cursor is None:
                return {
                    "schema": "semantic-atlas-search/1",
                    "concepts": [atlas.meaning("action", "action.repair")],
                    "next_cursor": long_cursor,
                }
            assert cursor == long_cursor
            return {
                "schema": "semantic-atlas-search/1",
                "concepts": [],
                "next_cursor": None,
            }

    wrapped = LongCursorAtlas()
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        service = PlayerLex(connection, wrapped)
        first = service.list_concepts(query, lex_id="action", limit=1)
        assert first["next_cursor"] == long_cursor
        second = service.list_concepts(
            query,
            lex_id="action",
            limit=1,
            cursor=first["next_cursor"],
        )
        assert second["concepts"] == []
        assert second["next_cursor"] is None
        assert wrapped.calls == [None, long_cursor]
    finally:
        connection.close()


def test_anchored_authoring_pattern_preserves_uninterpreted_capture_spans(playerlex):
    _connection, service = playerlex
    service.approve(
        kind="authoring_pattern",
        surface="ghost-step to {destination} with {style}",
        concept_id="action.planar_travel",
    )
    text = "ghost-step to the eastern arch with quiet precision"

    proposal = service.propose(text)
    assert proposal["match_count"] == 1
    match = proposal["matches"][0]
    assert match["source_span"] == {"start": 0, "end": len(text), "text": text}
    assert match["captures"] == [
        {
            "slot": "destination",
            "start": text.index("the eastern arch"),
            "end": text.index("the eastern arch") + len("the eastern arch"),
            "text": "the eastern arch",
        },
        {
            "slot": "style",
            "start": text.index("quiet precision"),
            "end": len(text),
            "text": "quiet precision",
        },
    ]
    assert service.propose("I " + text)["matches"] == []


def test_authoring_pattern_refuses_an_ambiguous_capture_partition(playerlex):
    _connection, service = playerlex
    service.approve(
        kind="authoring_pattern",
        surface="go {target} with {style}",
        concept_id="action.planar_travel",
    )

    assert service.propose("go east with haste")["match_count"] == 1
    assert service.propose("go east with runes with haste")["matches"] == []


@pytest.mark.parametrize(
    ("surface", "message"),
    [
        ("{destination", "balanced"),
        ("{destination}", "literal"),
        ("go {target} then {target}", "unique"),
        ("go {a} {b} {c} {d} {e}", "one to four"),
        ("go {BadSlot}", "lowercase"),
        ("go {target}{style}", "separator"),
        ("go {target} {style}", "separator"),
    ],
)
def test_malformed_or_overbroad_patterns_are_rejected(surface: str, message: str, playerlex):
    _connection, service = playerlex
    with pytest.raises(PlayerLexValidationError, match=message):
        service.approve(
            kind="authoring_pattern",
            surface=surface,
            concept_id="action.planar_travel",
        )


def test_duplicate_exact_approval_unknown_concept_and_bounded_input_refuse(playerlex):
    _connection, service = playerlex
    service.approve(kind="alias", surface="quiet door", concept_id="skill.stealth")

    with pytest.raises(PlayerLexConflictError, match="already approved"):
        service.approve(kind="alias", surface="  QUIET   DOOR  ", concept_id="skill.stealth")
    with pytest.raises(PlayerLexValidationError, match="unknown concept"):
        service.approve(kind="alias", surface="unknown", concept_id="concept.missing")
    with pytest.raises(PlayerLexValidationError, match="at most 2000"):
        service.propose("x" * 2001)


class _ChangedGlossary:
    def __init__(self, base: CapabilityGlossary, concept_id: str) -> None:
        self.concepts = copy.deepcopy(base.concepts)
        self._base = base
        self._concept_id = concept_id
        self._fingerprint = "sha256:" + "f" * 64
        self.concepts[concept_id]["meaning_fingerprint"] = self._fingerprint

    def concept_classification(self, concept_id: str) -> dict:
        result = self._base.concept_classification(concept_id)
        if concept_id == self._concept_id:
            result["meaning_fingerprint"] = self._fingerprint
        return result


def test_stale_meaning_is_visible_refused_and_requires_explicit_reapproval(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        original = PlayerLex(connection, glossary)
        entry = original.approve(kind="name", surface="Night Fold", concept_id="skill.stealth")

        changed = PlayerLex(connection, _ChangedGlossary(glossary, "skill.stealth"))
        listed = changed.list_entries()[0]
        assert listed["entry_id"] == entry["entry_id"]
        assert listed["status"] == "stale"
        assert listed["current_meaning_fingerprint"] == "sha256:" + "f" * 64

        refused = changed.propose("Night Fold")
        assert refused["matches"] == []
        assert refused["refused"] == [
            {
                "entry_id": entry["entry_id"],
                "lex_id": "capability",
                "reason": "meaning_fingerprint_changed",
                "approved_meaning_fingerprint": entry["concept"]["meaning_fingerprint"],
                "current_meaning_fingerprint": "sha256:" + "f" * 64,
            }
        ]

        with pytest.raises(PlayerLexValidationError, match="complete replacement"):
            changed.correct(entry["entry_id"])

        corrected = changed.correct(
            entry["entry_id"],
            kind=listed["kind"],
            surface=listed["surface"],
            concept_id=listed["concept"]["concept_id"],
            expected_meaning_fingerprint=entry["concept"]["meaning_fingerprint"],
            expected_approval_revision=entry["provenance"]["approval_revision"],
        )
        assert corrected["status"] == "current"
        assert corrected["provenance"]["approval_revision"] == 2
        assert changed.propose("Night Fold")["match_count"] == 1
    finally:
        connection.close()


def test_correction_and_hard_removal_survive_reopen_without_session_tables(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "playerlex.sqlite3"
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    service = PlayerLex(connection, glossary)
    entry = service.approve(kind="alias", surface="Old Name", concept_id="skill.stealth")
    corrected = service.correct(
        entry["entry_id"],
        kind="name",
        surface="New Name",
        concept_id="skill.investigation",
        expected_meaning_fingerprint=entry["concept"]["meaning_fingerprint"],
        expected_approval_revision=entry["provenance"]["approval_revision"],
    )
    assert corrected["kind"] == "name"
    assert corrected["surface"] == "New Name"
    assert corrected["concept"]["concept_id"] == "skill.investigation"
    connection.close()

    reopened_connection = sqlite3.connect(path, check_same_thread=False)
    reopened_connection.row_factory = sqlite3.Row
    reopened = PlayerLex(reopened_connection, glossary)
    assert [item["surface"] for item in reopened.list_entries()] == ["New Name"]
    tables = {
        row[0]
        for row in reopened_connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "playerlex_entries" in tables
    assert "sessions" not in tables
    assert reopened.remove(entry["entry_id"])
    reopened_connection.close()

    final_connection = sqlite3.connect(path, check_same_thread=False)
    final_connection.row_factory = sqlite3.Row
    try:
        final = PlayerLex(final_connection, glossary)
        assert final.list_entries() == []
        assert final.remove(entry["entry_id"])
    finally:
        final_connection.close()


def test_corrupt_stored_record_stays_visible_refused_repairable_and_removable(
    playerlex,
):
    connection, service = playerlex
    entry = service.approve(
        kind="alias",
        surface="Broken Alias",
        concept_id="skill.stealth",
    )
    valid = service.approve(
        kind="alias",
        surface="Working Alias",
        concept_id="skill.investigation",
    )
    connection.execute(
        "UPDATE playerlex_entries SET record_json=? WHERE entry_id=?",
        ('{"schema":"playerlex-entry/1"}', entry["entry_id"]),
    )
    connection.commit()

    listed = service.list_entries()
    assert len(listed) == 2
    assert listed[0]["entry_id"] == entry["entry_id"]
    assert listed[0]["surface"] == "Broken Alias"
    assert listed[0]["status"] == "corrupt"
    assert listed[0]["removable"] is True
    assert listed[1]["entry_id"] == valid["entry_id"]
    assert listed[1]["status"] == "current"

    proposal = service.propose("Broken Alias and Working Alias")
    assert [item["entry_id"] for item in proposal["matches"]] == [valid["entry_id"]]
    assert proposal["refused"] == [
        {
            "entry_id": entry["entry_id"],
            "lex_id": "capability",
            "reason": "corrupt_entry",
        }
    ]

    repaired = service.correct(
        entry["entry_id"],
        kind="alias",
        surface="Repaired Alias",
        concept_id="skill.investigation",
        expected_corrupt_version=listed[0]["corrupt_version"],
    )
    assert repaired["status"] == "current"
    assert repaired["surface"] == "Repaired Alias"

    removable = service.approve(
        kind="alias",
        surface="Remove Corrupt",
        concept_id="skill.stealth",
    )
    connection.execute(
        "UPDATE playerlex_entries SET record_json=? WHERE entry_id=?",
        ('{"schema":"playerlex-entry/1"}', removable["entry_id"]),
    )
    connection.commit()
    assert service.remove(removable["entry_id"])
    assert [item["surface"] for item in service.list_entries()] == [
        "Repaired Alias",
        "Working Alias",
    ]


def test_authority_shaped_or_forged_current_concept_facets_are_quarantined(playerlex):
    connection, service = playerlex
    extra = service.approve(kind="alias", surface="Forged Extra", concept_id="skill.stealth")
    changed = service.approve(kind="alias", surface="Forged Facet", concept_id="skill.investigation")

    for entry, mutation in (
        (extra, ("authorized", True)),
        (changed, ("definition", "forged meaning")),
    ):
        row = connection.execute(
            "SELECT record_json FROM playerlex_entries WHERE entry_id=?", (entry["entry_id"],)
        ).fetchone()
        record = json.loads(row["record_json"])
        facet, value = mutation
        record["concept"][facet] = value
        connection.execute(
            "UPDATE playerlex_entries SET record_json=? WHERE entry_id=?",
            (json.dumps(record), entry["entry_id"]),
        )
    connection.commit()

    listed = service.list_entries()
    assert {item["status"] for item in listed} == {"corrupt"}
    assert "authorized" not in json.dumps(listed)
    proposal = service.propose("Forged Extra and Forged Facet")
    assert proposal["matches"] == []
    assert {item["reason"] for item in proposal["refused"]} == {"corrupt_entry"}


def test_malformed_concurrency_columns_use_an_opaque_corrupt_version_for_repair(playerlex):
    connection, service = playerlex
    entry = service.approve(kind="alias", surface="Broken Version", concept_id="skill.stealth")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        """
        UPDATE playerlex_entries
        SET meaning_fingerprint='broken', approval_revision='broken', record_json='{}'
        WHERE entry_id=?
        """,
        (entry["entry_id"],),
    )
    connection.commit()
    connection.execute("PRAGMA ignore_check_constraints=OFF")

    corrupt = service.list_entries()[0]
    assert corrupt["status"] == "corrupt"
    assert corrupt["approval_revision"] is None
    assert corrupt["corrupt_version"].startswith("sha256:")

    connection.execute(
        "UPDATE playerlex_entries SET updated_at=updated_at + 1 WHERE entry_id=?",
        (entry["entry_id"],),
    )
    connection.commit()
    with pytest.raises(PlayerLexConflictError, match="changed since"):
        service.correct(
            entry["entry_id"],
            kind="alias",
            surface="Repaired Version",
            concept_id="skill.investigation",
            expected_corrupt_version=corrupt["corrupt_version"],
        )

    refreshed = service.list_entries()[0]
    repaired = service.correct(
        entry["entry_id"],
        kind="alias",
        surface="Repaired Version",
        concept_id="skill.investigation",
        expected_corrupt_version=refreshed["corrupt_version"],
    )
    assert repaired["status"] == "current"
    assert repaired["provenance"]["approval_revision"] == 1


def test_malformed_primary_keys_get_safe_exact_repair_and_removal_locators(playerlex):
    connection, service = playerlex
    binary = service.approve(kind="alias", surface="Binary ID", concept_id="skill.stealth")
    slash = service.approve(kind="alias", surface="Slash ID", concept_id="skill.investigation")
    connection.execute(
        "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
        (sqlite3.Binary(b"\xff\x00"), binary["entry_id"]),
    )
    connection.execute(
        "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
        ("bad/id", slash["entry_id"]),
    )
    connection.commit()

    listed = {item["surface"]: item for item in service.list_entries()}
    assert all(item["status"] == "corrupt" for item in listed.values())
    assert all("/" not in item["entry_id"] for item in listed.values())
    assert all(item["entry_id"].startswith("playerlex_corrupt_") for item in listed.values())

    repaired = service.correct(
        listed["Binary ID"]["entry_id"],
        kind="alias",
        surface="Repaired Binary ID",
        concept_id="skill.investigation",
        expected_corrupt_version=listed["Binary ID"]["corrupt_version"],
    )
    assert repaired["status"] == "current"
    assert repaired["entry_id"].startswith("playerlex_")
    assert not repaired["entry_id"].startswith("playerlex_corrupt_")

    stale_locator = listed["Slash ID"]["entry_id"]
    connection.execute("UPDATE playerlex_entries SET updated_at=updated_at + 1 WHERE surface='Slash ID'")
    connection.commit()
    with pytest.raises(PlayerLexConflictError, match="changed since"):
        service.remove(stale_locator)
    refreshed_locator = next(
        item["entry_id"] for item in service.list_entries() if item["surface"] == "Slash ID"
    )
    assert refreshed_locator != stale_locator
    assert service.remove(refreshed_locator)
    assert service.remove(refreshed_locator)
    rows = connection.execute("SELECT entry_id, surface FROM playerlex_entries").fetchall()
    assert [row["surface"] for row in rows] == ["Repaired Binary ID"]


def _replace_corrupt_row_with_identical_logical_bytes(connection, service):
    approved = service.approve(
        kind="alias",
        surface="Reincarnated Alias",
        concept_id="skill.stealth",
    )
    connection.execute(
        "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
        ("bad/id", approved["entry_id"]),
    )
    connection.commit()
    stale = service.list_entries()[0]
    columns = (
        "entry_id",
        "schema",
        "kind",
        "surface",
        "normalized_surface",
        "concept_id",
        "meaning_fingerprint",
        "approval_revision",
        "approved_at",
        "created_at",
        "updated_at",
        "record_json",
    )
    original = connection.execute(f"SELECT {', '.join(columns)} FROM playerlex_entries").fetchone()
    assert service.remove(stale["entry_id"])

    replacement = service.approve(
        kind="alias",
        surface="Reincarnated Alias",
        concept_id="skill.stealth",
    )
    assignments = ", ".join(f"{column}=?" for column in columns)
    connection.execute(
        f"UPDATE playerlex_entries SET {assignments} WHERE entry_id=?",
        (*tuple(original[column] for column in columns), replacement["entry_id"]),
    )
    connection.commit()
    current = service.list_entries()[0]
    assert current["corrupt_version"] == stale["corrupt_version"]
    return stale, current


def test_stale_corrupt_locator_cannot_remove_identical_row_reincarnation(playerlex):
    _connection, service = playerlex
    stale, current = _replace_corrupt_row_with_identical_logical_bytes(*playerlex)

    assert current["entry_id"] != stale["entry_id"]
    assert service.remove(stale["entry_id"])
    assert service.list_entries()[0]["entry_id"] == current["entry_id"]


def test_stale_corrupt_locator_cannot_correct_identical_row_reincarnation(playerlex):
    _connection, service = playerlex
    stale, current = _replace_corrupt_row_with_identical_logical_bytes(*playerlex)

    assert current["entry_id"] != stale["entry_id"]
    with pytest.raises(PlayerLexNotFoundError, match="unknown"):
        service.correct(
            stale["entry_id"],
            kind="alias",
            surface="Wrong Reincarnation",
            concept_id="skill.investigation",
            expected_corrupt_version=stale["corrupt_version"],
        )
    assert service.list_entries()[0]["entry_id"] == current["entry_id"]


def test_retired_storage_identity_cannot_be_reused(playerlex):
    connection, service = playerlex
    approved = service.approve(
        kind="alias",
        surface="Retired Identity",
        concept_id="skill.stealth",
    )
    row = connection.execute(
        "SELECT storage_token, entry_id, schema, kind, surface, normalized_surface, concept_id, "
        "meaning_fingerprint, approval_revision, approved_at, created_at, updated_at, record_json "
        "FROM playerlex_entries WHERE entry_id=?",
        (approved["entry_id"],),
    ).fetchone()
    assert service.remove(approved["entry_id"])
    assert (
        connection.execute(
            "SELECT count(*) FROM playerlex_retired_storage_tokens WHERE storage_token=?",
            (row["storage_token"],),
        ).fetchone()[0]
        == 1
    )

    with pytest.raises(sqlite3.IntegrityError, match="retired PlayerLex storage token"):
        connection.execute(
            """
            INSERT INTO playerlex_entries(
                storage_token, entry_id, schema, kind, surface, normalized_surface, concept_id,
                meaning_fingerprint, approval_revision, approved_at, created_at, updated_at,
                record_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            tuple(row),
        )
    connection.rollback()


def test_retired_storage_identity_cannot_be_assigned_by_update_or_stale_reopen(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        service = PlayerLex(connection, glossary)
        approved = service.approve(
            kind="alias",
            surface="Retired Update Identity",
            concept_id="skill.stealth",
        )
        connection.execute(
            "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
            ("bad/id", approved["entry_id"]),
        )
        connection.commit()
        stale = service.list_entries()[0]
        columns = (
            "entry_id",
            "schema",
            "kind",
            "surface",
            "normalized_surface",
            "concept_id",
            "meaning_fingerprint",
            "approval_revision",
            "approved_at",
            "created_at",
            "updated_at",
            "record_json",
        )
        original = connection.execute(
            f"SELECT storage_token, {', '.join(columns)} FROM playerlex_entries"
        ).fetchone()
        assert service.remove(stale["entry_id"])

        replacement = service.approve(
            kind="alias",
            surface="Retired Update Identity",
            concept_id="skill.stealth",
        )
        replacement_storage_token = connection.execute(
            "SELECT storage_token FROM playerlex_entries WHERE entry_id=?",
            (replacement["entry_id"],),
        ).fetchone()[0]
        assignments = ", ".join(f"{column}=?" for column in columns)
        connection.execute(
            f"UPDATE playerlex_entries SET {assignments} WHERE entry_id=?",
            (*tuple(original[column] for column in columns), replacement["entry_id"]),
        )
        connection.commit()
        current = service.list_entries()[0]
        assert current["corrupt_version"] == stale["corrupt_version"]

        with pytest.raises(sqlite3.IntegrityError, match="immutable PlayerLex storage token"):
            connection.execute(
                "UPDATE playerlex_entries SET storage_token=? WHERE storage_token=?",
                (original["storage_token"], replacement_storage_token),
            )
        connection.rollback()
        with pytest.raises(PlayerLexNotFoundError, match="unknown"):
            service.correct(
                stale["entry_id"],
                kind="alias",
                surface="Wrong Retired Rewrite",
                concept_id="skill.investigation",
                expected_corrupt_version=stale["corrupt_version"],
            )

        reopened = PlayerLex(connection, glossary)
        with pytest.raises(PlayerLexNotFoundError, match="unknown"):
            reopened.correct(
                stale["entry_id"],
                kind="alias",
                surface="Wrong Reopened Rewrite",
                concept_id="skill.investigation",
                expected_corrupt_version=stale["corrupt_version"],
            )
        assert reopened.list_entries()[0]["entry_id"] == current["entry_id"]
    finally:
        connection.close()


@pytest.mark.parametrize("reuse", ["storage_token", "entry_id", "approval", "rowid"])
def test_insert_or_replace_cannot_reincarnate_an_active_identity(reuse: str, playerlex):
    connection, service = playerlex
    approved = service.approve(
        kind="alias",
        surface="Replacement Guard",
        concept_id="skill.stealth",
    )
    row = connection.execute("SELECT rowid AS source_rowid, * FROM playerlex_entries").fetchone()
    columns = tuple(column for column in row.keys() if column != "source_rowid")
    values = {column: row[column] for column in columns}
    values["storage_token"] = "f" * 32
    values["entry_id"] = "playerlex_" + "e" * 32
    values["surface"] = "Replacement Intruder"
    values["normalized_surface"] = "replacement intruder"
    if reuse == "storage_token":
        values["storage_token"] = row["storage_token"]
    elif reuse == "entry_id":
        values["entry_id"] = row["entry_id"]
    else:
        values["surface"] = row["surface"]
        values["normalized_surface"] = row["normalized_surface"]

    insert_columns = columns
    insert_values = tuple(values[column] for column in columns)
    if reuse == "rowid":
        insert_columns = ("rowid", *insert_columns)
        insert_values = (row["source_rowid"], *insert_values)

    with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
        connection.execute(
            f"INSERT OR REPLACE INTO playerlex_entries({', '.join(insert_columns)}) "
            f"VALUES({', '.join('?' for _ in insert_columns)})",
            insert_values,
        )
    connection.rollback()
    assert [entry["entry_id"] for entry in service.list_entries()] == [approved["entry_id"]]
    assert connection.execute("SELECT count(*) FROM playerlex_retired_storage_tokens").fetchone()[0] == 0


def test_retired_storage_tombstones_refuse_update_delete_and_replacement(playerlex):
    connection, service = playerlex
    approved = service.approve(
        kind="alias",
        surface="Immutable Tombstone",
        concept_id="skill.stealth",
    )
    token = connection.execute(
        "SELECT storage_token FROM playerlex_entries WHERE entry_id=?",
        (approved["entry_id"],),
    ).fetchone()[0]
    assert service.remove(approved["entry_id"])
    tombstone_rowid = connection.execute(
        "SELECT rowid FROM playerlex_retired_storage_tokens WHERE storage_token=?",
        (token,),
    ).fetchone()[0]

    statements = (
        (
            "UPDATE playerlex_retired_storage_tokens SET storage_token=? WHERE storage_token=?",
            ("f" * 32, token),
        ),
        ("DELETE FROM playerlex_retired_storage_tokens WHERE storage_token=?", (token,)),
        (
            "INSERT OR REPLACE INTO playerlex_retired_storage_tokens(rowid, storage_token) VALUES(?,?)",
            (tombstone_rowid, "e" * 32),
        ),
    )
    for sql, parameters in statements:
        with pytest.raises(sqlite3.IntegrityError, match="retired PlayerLex|PlayerLex retired"):
            connection.execute(sql, parameters)
        connection.rollback()

    assert [
        row[0]
        for row in connection.execute("SELECT storage_token FROM playerlex_retired_storage_tokens").fetchall()
    ] == [token]


def test_proposal_rejects_surrogate_code_points_before_matching(playerlex):
    _connection, service = playerlex
    service.approve(
        kind="authoring_pattern",
        surface="go {destination}",
        concept_id="skill.stealth",
    )

    with pytest.raises(PlayerLexValidationError, match="surrogate"):
        service.propose("go \ud800")


def test_initialization_refuses_outer_transaction_then_retries_cleanly(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        with pytest.raises(PlayerLexError, match="active transaction"):
            PlayerLex(connection, glossary)
        connection.rollback()
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
            ).fetchone()
            is None
        )

        service = PlayerLex(connection, glossary)
        assert service.list_entries() == []
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def test_initialization_rejects_a_lookalike_table_without_exact_schema(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE playerlex_entries(
                entry_id, schema, kind, surface, normalized_surface, concept_id,
                meaning_fingerprint, approval_revision, approved_at, created_at, updated_at,
                record_json
            )
            """
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="schema|verification"):
            PlayerLex(connection, glossary)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='playerlex_surface_idx'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize("temporary", [False, True])
def test_playerlex_mixed_case_owned_prefix_collision_refuses_before_transform_or_ledger(
    glossary: CapabilityGlossary,
    temporary: bool,
) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        object_name = "PlAyErLeX_FoReIgN"
        connection.execute(
            f'CREATE {"TEMP " if temporary else ""}TABLE "{object_name}"(foreign_value TEXT)'
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)

        catalog = "sqlite_temp_schema" if temporary else "main.sqlite_schema"
        assert connection.execute(
            f"SELECT sql FROM {catalog} WHERE name=?",
            (object_name,),
        ).fetchone() is not None
        assert connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='playerlex_entries'"
        ).fetchone() is None
        ledger = connection.execute(
            "SELECT 1 FROM main.sqlite_schema "
            "WHERE type='table' AND name='aetherstate_schema_migrations'"
        ).fetchone()
        if ledger is not None:
            assert connection.execute(
                "SELECT 1 FROM aetherstate_schema_migrations WHERE version=5"
            ).fetchone() is None
    finally:
        connection.close()


def test_initialization_rejects_index_collation_or_direction_drift(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        PlayerLex(connection, glossary)
        connection.execute("DROP INDEX playerlex_surface_idx")
        connection.execute(
            """
            CREATE INDEX playerlex_surface_idx
            ON playerlex_entries(normalized_surface COLLATE NOCASE DESC, kind, concept_id)
            """
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("object_type", "object_name", "drop_sql"),
    [
        ("index", "playerlex_surface_idx", "DROP INDEX playerlex_surface_idx"),
        (
            "trigger",
            "playerlex_retire_storage_token",
            "DROP TRIGGER playerlex_retire_storage_token",
        ),
    ],
)
def test_initialization_does_not_repair_missing_current_schema_objects(
    glossary: CapabilityGlossary,
    object_type: str,
    object_name: str,
    drop_sql: str,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        PlayerLex(connection, glossary)
        connection.execute(drop_sql)
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                (object_type, object_name),
            ).fetchone()
            is None
        )
    finally:
        connection.close()


@pytest.mark.parametrize("temporary", [False, True])
def test_initialization_rejects_extra_trigger_on_retired_token_table(
    glossary: CapabilityGlossary,
    temporary: bool,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        PlayerLex(connection, glossary)
        connection.execute(
            f"""
            CREATE {"TEMP " if temporary else ""}TRIGGER erase_playerlex_retirement
            AFTER INSERT ON playerlex_retired_storage_tokens
            BEGIN
                DELETE FROM playerlex_retired_storage_tokens
                WHERE storage_token=NEW.storage_token;
            END
            """
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
        assert (
            connection.execute(
                f"SELECT 1 FROM {'sqlite_temp_master' if temporary else 'sqlite_master'} "
                "WHERE type='trigger' AND name='erase_playerlex_retirement'"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


@pytest.mark.parametrize("object_kind", ["table", "view", "index"])
def test_initialization_rejects_every_extra_persistent_playerlex_schema_object(
    glossary: CapabilityGlossary,
    object_kind: str,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        PlayerLex(connection, glossary)
        if object_kind == "table":
            connection.execute("CREATE TABLE playerlex_unexpected_table(secret TEXT)")
            object_name = "playerlex_unexpected_table"
        elif object_kind == "view":
            connection.execute(
                "CREATE VIEW playerlex_unexpected_view AS SELECT surface, record_json FROM playerlex_entries"
            )
            object_name = "playerlex_unexpected_view"
        else:
            connection.execute("CREATE TABLE unrelated_playerlex_index_owner(value TEXT)")
            connection.execute(
                "CREATE INDEX playerlex_unexpected_index ON unrelated_playerlex_index_owner(value)"
            )
            object_name = "playerlex_unexpected_index"
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (object_name,)).fetchone()
            is not None
        )
    finally:
        connection.close()


def test_exact_v1_schema_with_extra_persistent_view_refuses_without_migration(
    glossary: CapabilityGlossary,
):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        for statement in _V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "CREATE VIEW playerlex_unexpected_view AS SELECT surface, record_json FROM playerlex_entries"
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
        assert "lex_id" not in [row[1] for row in connection.execute("PRAGMA table_info(playerlex_entries)")]
        view_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='playerlex_unexpected_view'"
        ).fetchone()[0]
        assert "playerlex_entries_v1" not in view_sql
        assert connection.execute("SELECT * FROM playerlex_unexpected_view").fetchall() == []
    finally:
        connection.close()


def test_nonexact_v1_schema_is_refused_without_repair(glossary: CapabilityGlossary):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        for statement in _V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute("DROP TRIGGER playerlex_reject_identity_replacement")
        connection.commit()

        with pytest.raises(PlayerLexError, match="verification"):
            PlayerLex(connection, glossary)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(playerlex_entries)")]
        assert "lex_id" not in columns
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='playerlex_reject_identity_replacement'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='playerlex_entries_v1'").fetchone()
            is None
        )
    finally:
        connection.close()


def test_exact_v1_schema_migrates_entries_corruption_and_tombstones(
    glossary: CapabilityGlossary,
):
    source = sqlite3.connect(":memory:", check_same_thread=False)
    source.row_factory = sqlite3.Row
    try:
        source_service = PlayerLex(source, glossary)
        valid = source_service.approve(
            kind="alias",
            surface="V1 Approval",
            concept_id="skill.stealth",
        )
        corrupt = source_service.approve(
            kind="alias",
            surface="V1 Corrupt",
            concept_id="skill.investigation",
        )
        valid_token, valid_values = _v1_row_from_current_entry(source, glossary, valid["entry_id"])
        corrupt_token, corrupt_values = _v1_row_from_current_entry(
            source,
            glossary,
            corrupt["entry_id"],
            raw_entry_id=sqlite3.Binary(b"\xffv1"),
        )
    finally:
        source.close()

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        for statement in _V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        placeholders = ",".join("?" for _ in range(len(_V1_ROW_COLUMNS) + 1))
        for storage_token, values in (
            (valid_token, valid_values),
            (corrupt_token, corrupt_values),
        ):
            connection.execute(
                f"INSERT INTO playerlex_entries(storage_token, {', '.join(_V1_ROW_COLUMNS)}) "
                f"VALUES({placeholders})",
                (storage_token, *(values[column] for column in _V1_ROW_COLUMNS)),
            )
        retired_token = "f" * 32
        connection.execute(
            "INSERT INTO playerlex_retired_storage_tokens(storage_token) VALUES(?)",
            (retired_token,),
        )
        stored_corrupt = connection.execute(
            f"SELECT {', '.join(_V1_ROW_COLUMNS)} FROM playerlex_entries WHERE storage_token=?",
            (corrupt_token,),
        ).fetchone()
        corrupt_version = PlayerLex._version_from_values(
            _V1_ROW_COLUMNS,
            {column: stored_corrupt[column] for column in _V1_ROW_COLUMNS},
        )
        old_locator = f"playerlex_corrupt_{corrupt_token}_{corrupt_version.removeprefix('sha256:')}"
        connection.commit()

        migrated = PlayerLex(connection, glossary)
        entries = {item["surface"]: item for item in migrated.list_entries()}
        assert entries["V1 Approval"]["entry_id"] == valid["entry_id"]
        assert entries["V1 Approval"]["schema"] == "playerlex-entry/2"
        assert entries["V1 Approval"]["lex_id"] == "capability"
        assert entries["V1 Approval"]["concept"]["lex_id"] == "capability"
        assert entries["V1 Approval"]["status"] == "current"
        assert entries["V1 Corrupt"]["entry_id"] == old_locator
        assert entries["V1 Corrupt"]["lex_id"] == "capability"
        assert entries["V1 Corrupt"]["status"] == "corrupt"
        assert (
            connection.execute(
                "SELECT storage_token FROM playerlex_entries WHERE entry_id=?",
                (valid["entry_id"],),
            ).fetchone()[0]
            == valid_token
        )
        assert (
            connection.execute(
                "SELECT 1 FROM playerlex_retired_storage_tokens WHERE storage_token=?",
                (retired_token,),
            ).fetchone()
            is not None
        )
        columns = [row[1] for row in connection.execute("PRAGMA table_info(playerlex_entries)")]
        assert columns[6:8] == ["lex_id", "concept_id"]
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='playerlex_entries_v1'").fetchone()
            is None
        )

        assert migrated.remove(old_locator)
        assert (
            connection.execute(
                "SELECT 1 FROM playerlex_retired_storage_tokens WHERE storage_token=?",
                (corrupt_token,),
            ).fetchone()
            is not None
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM playerlex_retired_storage_tokens WHERE storage_token=?",
                (retired_token,),
            )
    finally:
        connection.close()


def test_exact_v2_four_lex_schema_migrates_to_claimlex_and_preserves_rows_tokens_and_tombstones(
    atlas: SemanticAtlas,
):
    source = sqlite3.connect(":memory:", check_same_thread=False)
    source.row_factory = sqlite3.Row
    try:
        source_service = PlayerLex(source, atlas)
        approved = source_service.approve(
            kind="alias",
            surface="Four Lex Approval",
            lex_id="capability",
            concept_id="skill.stealth",
        )
        removed = source_service.approve(
            kind="alias",
            surface="Four Lex Retired",
            lex_id="capability",
            concept_id="skill.investigation",
        )
        approved_row = source.execute(
            "SELECT * FROM playerlex_entries WHERE entry_id=?",
            (approved["entry_id"],),
        ).fetchone()
        columns = tuple(item[1] for item in source.execute("PRAGMA table_info(playerlex_entries)"))
        approved_values = tuple(approved_row[column] for column in columns)
        removed_token = source.execute(
            "SELECT storage_token FROM playerlex_entries WHERE entry_id=?",
            (removed["entry_id"],),
        ).fetchone()[0]
        assert source_service.remove(removed["entry_id"])
        assert source.execute(
            "SELECT 1 FROM playerlex_retired_storage_tokens WHERE storage_token=?",
            (removed_token,),
        ).fetchone() is not None
    finally:
        source.close()

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        previous_schema = _SCHEMA_STATEMENTS[0].replace(
            "'capability', 'referent', 'scene', 'action', 'claim'",
            "'capability', 'referent', 'scene', 'action'",
        )
        for statement in (previous_schema, *_SCHEMA_STATEMENTS[1:]):
            connection.execute(statement)
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO playerlex_entries ({', '.join(columns)}) VALUES ({placeholders})",
            approved_values,
        )
        connection.execute(
            "INSERT INTO playerlex_retired_storage_tokens(storage_token) VALUES(?)",
            (removed_token,),
        )
        connection.commit()

        migrated = PlayerLex(connection, atlas)
        entry = next(
            item for item in migrated.list_entries() if item["entry_id"] == approved["entry_id"]
        )
        assert entry["surface"] == "Four Lex Approval"
        assert entry["lex_id"] == "capability"
        assert connection.execute(
            "SELECT storage_token FROM playerlex_entries WHERE entry_id=?",
            (approved["entry_id"],),
        ).fetchone()[0] == approved_row["storage_token"]
        assert connection.execute(
            "SELECT 1 FROM playerlex_retired_storage_tokens WHERE storage_token=?",
            (removed_token,),
        ).fetchone() is not None

        claim = atlas.search(lex_id="claim", limit=1)["concepts"][0]
        claim_entry = migrated.approve(
            kind="alias",
            surface="ClaimLex Approval",
            lex_id="claim",
            concept_id=claim["concept_id"],
        )
        assert claim_entry["lex_id"] == "claim"
        assert "'claim'" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_four_lex_predecessor_with_altered_surface_index_refuses_before_v5_claim(
    atlas: SemanticAtlas,
):
    """A same-name predecessor index with changed collation/order is not admissible."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        previous_schema = _SCHEMA_STATEMENTS[0].replace(
            "'capability', 'referent', 'scene', 'action', 'claim'",
            "'capability', 'referent', 'scene', 'action'",
        )
        for statement in (previous_schema, *_SCHEMA_STATEMENTS[1:]):
            connection.execute(statement)
        connection.execute("DROP INDEX playerlex_surface_idx")
        connection.execute(
            "CREATE INDEX playerlex_surface_idx "
            "ON playerlex_entries(normalized_surface COLLATE NOCASE DESC, kind, lex_id, concept_id)"
        )
        connection.commit()

        with pytest.raises(PlayerLexError, match="failed verification"):
            PlayerLex(connection, atlas)
        assert "'claim'" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
    finally:
        connection.close()


def test_four_lex_predecessor_with_altered_retired_token_check_refuses_before_v5_claim(
    tmp_path: Path, atlas: SemanticAtlas,
):
    """The predecessor's retired-token table SQL is as sealed as its live table SQL."""
    connection = sqlite3.connect(
        tmp_path / "playerlex-altered-retired-check.sqlite3",
        check_same_thread=False,
        factory=_CheckpointFailingConnection,
    )
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        previous_schema = _SCHEMA_STATEMENTS[0].replace(
            "'capability', 'referent', 'scene', 'action', 'claim'",
            "'capability', 'referent', 'scene', 'action'",
        )
        for statement in (previous_schema, *_SCHEMA_STATEMENTS[1:]):
            connection.execute(statement)
        for trigger in (
            "playerlex_reject_retired_storage_token", "playerlex_retire_storage_token",
            "playerlex_reject_storage_token_update", "playerlex_reject_identity_replacement",
            "playerlex_reject_retired_storage_token_update",
            "playerlex_reject_retired_storage_token_delete",
            "playerlex_reject_retired_storage_token_replacement",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE playerlex_retired_storage_tokens")
        connection.execute(_SCHEMA_STATEMENTS[1].replace("length(storage_token) = 32", "length(storage_token) = 31"))
        for statement in _SCHEMA_STATEMENTS[3:]:
            connection.execute(statement)
        connection.commit()
        altered_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='playerlex_retired_storage_tokens'"
        ).fetchone()[0]
        connection.fail_checkpoint_call = 1

        with pytest.raises(PlayerLexError, match="failed verification"):
            PlayerLex(connection, atlas)
        assert connection.checkpoint_calls == 0
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='playerlex_retired_storage_tokens'"
        ).fetchone()[0] == altered_sql
        assert "'claim'" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
    finally:
        connection.close()


def test_exact_v1_schema_migrates_rows_with_default_tuple_row_factory(
    glossary: CapabilityGlossary,
):
    source = sqlite3.connect(":memory:", check_same_thread=False)
    source.row_factory = sqlite3.Row
    try:
        source_service = PlayerLex(source, glossary)
        approved = source_service.approve(
            kind="alias",
            surface="Tuple Migration",
            concept_id="skill.stealth",
        )
        storage_token, values = _v1_row_from_current_entry(
            source,
            glossary,
            approved["entry_id"],
        )
    finally:
        source.close()

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        for statement in _V1_SCHEMA_STATEMENTS:
            connection.execute(statement)
        placeholders = ",".join("?" for _ in range(len(_V1_ROW_COLUMNS) + 1))
        connection.execute(
            f"INSERT INTO playerlex_entries(storage_token, {', '.join(_V1_ROW_COLUMNS)}) "
            f"VALUES({placeholders})",
            (storage_token, *(values[column] for column in _V1_ROW_COLUMNS)),
        )
        connection.commit()

        migrated = PlayerLex(connection, glossary)
        assert connection.row_factory is None
        entries = migrated.list_entries()
        assert len(entries) == 1
        assert entries[0]["entry_id"] == approved["entry_id"]
        assert entries[0]["lex_id"] == "capability"
        assert entries[0]["status"] == "current"
    finally:
        connection.close()


def test_remove_refuses_an_outer_transaction(playerlex):
    connection, service = playerlex
    entry = service.approve(kind="alias", surface="Keep Me", concept_id="skill.stealth")

    connection.execute("BEGIN")
    with pytest.raises(PlayerLexError, match="active transaction"):
        service.remove(entry["entry_id"])
    connection.rollback()

    assert [item["entry_id"] for item in service.list_entries()] == [entry["entry_id"]]


def test_secure_removal_scrubs_approved_text_from_database_and_wal(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "secure-playerlex.sqlite3"
    wal_path = Path(str(path) + "-wal")
    marker = "PLAYERLEX_PRIVATE_DELETE_9D3A8F6C"
    marker_bytes = marker.encode("utf-8")
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        service = PlayerLex(connection, glossary)
        assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
        entry = service.approve(kind="alias", surface=marker, concept_id="skill.stealth")

        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert checkpoint[0] == 0
        assert marker_bytes in path.read_bytes()

        assert service.remove(entry["entry_id"])
        assert service.list_entries() == []
        assert not wal_path.exists() or wal_path.stat().st_size == 0
        assert marker_bytes not in path.read_bytes()
        if wal_path.exists():
            assert marker_bytes not in wal_path.read_bytes()
    finally:
        connection.close()


class _CheckpointFailingConnection(sqlite3.Connection):
    checkpoint_calls: int
    fail_checkpoint_call: int

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.checkpoint_calls = 0
        self.fail_checkpoint_call = 0

    def execute(self, sql, parameters=()):
        if " ".join(sql.split()).casefold() == "pragma wal_checkpoint(truncate)":
            self.checkpoint_calls += 1
            if self.checkpoint_calls == self.fail_checkpoint_call:
                raise sqlite3.OperationalError("simulated checkpoint contention")
        return super().execute(sql, parameters)


class _PendingMarkerFailingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if "INSERT" in sql.upper() and _PENDING_CLEANUP in sql:
            raise sqlite3.OperationalError("simulated pending-marker write failure")
        return super().execute(sql, parameters)


def _create_four_lex_predecessor(connection: sqlite3.Connection) -> None:
    previous = _SCHEMA_STATEMENTS[0].replace(
        "'capability', 'referent', 'scene', 'action', 'claim'",
        "'capability', 'referent', 'scene', 'action'",
    )
    for statement in (previous, *_SCHEMA_STATEMENTS[1:]):
        connection.execute(statement)
    connection.commit()


def test_playerlex_marker_write_failure_rolls_back_transform_and_v5_claim(
    tmp_path: Path, atlas: SemanticAtlas,
):
    """A v5 rebuild must not commit its ledger unless its cleanup marker commits with it."""
    connection = sqlite3.connect(
        tmp_path / "playerlex-marker-write-failure.sqlite3", factory=_PendingMarkerFailingConnection,
    )
    try:
        _create_four_lex_predecessor(connection)
        with pytest.raises(PlayerLexError, match="failed verification"):
            PlayerLex(connection, atlas)
        assert "'claim'" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name=?", (_PENDING_CLEANUP,)
        ).fetchone() is None
    finally:
        connection.close()


def test_playerlex_temp_pending_marker_shadow_refuses_before_transform_or_v5_claim(
    atlas: SemanticAtlas,
):
    """A TEMP marker shadow must fail closed before a v5 migration changes main."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        _create_four_lex_predecessor(connection)
        connection.execute(f"CREATE TEMP TABLE {_PENDING_CLEANUP}(domain TEXT, version INTEGER)")
        with pytest.raises(PlayerLexError, match="migration_cleanup_schema_invalid"):
            PlayerLex(connection, atlas)
        assert "'claim'" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
    finally:
        connection.close()


@pytest.mark.parametrize(
    "foreign_sql",
    (
        f"CREATE UNIQUE INDEX {_PENDING_CLEANUP}_foreign ON {_PENDING_CLEANUP}(domain COLLATE NOCASE)",
        f"CREATE TRIGGER {_PENDING_CLEANUP}_foreign AFTER INSERT ON {_PENDING_CLEANUP} BEGIN SELECT 1; END",
        f"CREATE VIEW {_PENDING_CLEANUP}_foreign AS SELECT domain, version FROM {_PENDING_CLEANUP}",
    ),
    ids=("index-metadata", "trigger", "view"),
)
def test_playerlex_pending_marker_foreign_object_refuses_before_transform_or_v5_claim(
    atlas: SemanticAtlas, foreign_sql: str,
):
    """No foreign marker index, trigger, or view is trusted for v5 admission."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        _create_four_lex_predecessor(connection)
        connection.execute(_PENDING_CLEANUP_SCHEMA_SQL)
        connection.execute(foreign_sql)
        connection.commit()
        with pytest.raises(PlayerLexError, match="migration_cleanup_schema_invalid"):
            PlayerLex(connection, atlas)
        assert "'claim'" not in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='playerlex_entries'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE name='aetherstate_schema_migrations'"
        ).fetchone() is None
    finally:
        connection.close()


@pytest.mark.parametrize(
    "collision_kind",
    ("mixed-case-temp-table", "uppercase-foreign-view-reference"),
)
def test_cleanup_protocol_rejects_ascii_case_insensitive_marker_collisions_before_mutation(
    atlas: SemanticAtlas,
    collision_kind: str,
):
    """SQLite-equivalent marker identities and references fail before any main mutation."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        _create_four_lex_predecessor(connection)
        if collision_kind == "mixed-case-temp-table":
            connection.execute(
                "CREATE TEMP TABLE AeThErStAtE_ScHeMa_PeNdInG_ClEaNuP("
                "domain TEXT, version INTEGER)"
            )
        else:
            connection.execute(_PENDING_CLEANUP_SCHEMA_SQL)
            connection.execute(
                "CREATE VIEW unrelated_cleanup_view AS "
                "SELECT domain, version FROM AETHERSTATE_SCHEMA_PENDING_CLEANUP"
            )
        connection.commit()
        schema_before = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                "ORDER BY type, name"
            )
        )
        changes_before = connection.total_changes

        with pytest.raises(PlayerLexError, match="migration_cleanup_schema_invalid"):
            PlayerLex(connection, atlas)

        assert tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                "ORDER BY type, name"
            )
        ) == schema_before
        assert connection.total_changes == changes_before
        assert not connection.in_transaction
    finally:
        connection.close()


def test_cleanup_protocol_ignores_unicode_identifier_outside_sqlite_ascii_folding():
    """An unrelated quoted Unicode identifier does not block valid cleanup admission."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    runner = SchemaMigrationRunner(
        connection,
        threading.RLock(),
        database_schema_migrations(),
    )
    unrelated_name = "aether\u017ftate_schema_pending_cleanup"
    try:
        connection.execute(f'CREATE TABLE "{unrelated_name}"(value TEXT)')
        connection.commit()

        runner.mark_cleanup_pending("playerlex", 5)

        assert runner.cleanup_pending("playerlex", 5) is True
        assert connection.execute(
            "SELECT name FROM main.sqlite_schema WHERE type='table' AND name=?",
            (unrelated_name,),
        ).fetchone() == (unrelated_name,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "identity_kind",
    ("unregistered", "mismatched-owner", "registered-non-owner"),
)
def test_cleanup_marker_requires_exact_registered_cleanup_owner_without_persistence(
    identity_kind: str,
):
    """Public marker writes accept only the registry migration that owns cleanup."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    migrations = database_schema_migrations()
    runner = SchemaMigrationRunner(connection, threading.RLock(), migrations)
    try:
        if identity_kind == "unregistered":
            domain, version = "foreign-domain", 99
        elif identity_kind == "mismatched-owner":
            domain, version = "playerlex", 6
        else:
            migration = next(item for item in migrations if not item.requires_cleanup)
            domain, version = migration.domain, migration.version
        schema_before = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                "ORDER BY type, name"
            )
        )

        with pytest.raises(SchemaMigrationError) as raised:
            runner.mark_cleanup_pending(domain, version)

        assert raised.value.code == "migration_cleanup_identity_invalid"
        assert str(raised.value) == "migration_cleanup_identity_invalid"
        assert tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                "ORDER BY type, name"
            )
        ) == schema_before
        assert not connection.in_transaction
    finally:
        connection.close()


def test_pending_marker_mutators_refuse_outer_transaction_without_touching_caller_state():
    """Public marker mutation paths never roll back or mutate a caller-owned transaction."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    lock = threading.RLock()
    runner = SchemaMigrationRunner(connection, lock, database_schema_migrations())
    try:
        runner.mark_cleanup_pending("playerlex", 5)
        connection.execute("CREATE TABLE caller_sentinel(value TEXT)")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_sentinel(value) VALUES('preserve')")
        for operation in (runner.mark_cleanup_pending, runner.clear_cleanup_pending):
            with pytest.raises(SchemaMigrationError) as raised:
                operation("playerlex", 5)
            assert raised.value.code == TRANSACTION_ACTIVE
            assert connection.in_transaction
            assert connection.execute("SELECT value FROM caller_sentinel").fetchone()[0] == "preserve"
        connection.rollback()
    finally:
        connection.close()


def test_playerlex_declares_atomic_cleanup_requirement():
    """Only the deferred v5 registry entry opts into the runner's cleanup protocol."""
    (migration,) = playerlex_schema_migrations()
    assert migration.requires_cleanup is True


def test_healthy_current_playerlex_wal_reopen_skips_checkpoint_contention(
    tmp_path: Path, atlas: SemanticAtlas,
):
    """A clean current v5 catalog is not a pending migration-cleanup retry."""
    connection = sqlite3.connect(
        tmp_path / "playerlex-current-wal.sqlite3", factory=_CheckpointFailingConnection,
    )
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        PlayerLex(connection, atlas)
        connection.checkpoint_calls = 0
        connection.fail_checkpoint_call = 1

        PlayerLex(connection, atlas)
        assert connection.checkpoint_calls == 0
    finally:
        connection.close()


def test_post_migration_checkpoint_retry_blocks_playerlex_availability(tmp_path: Path, atlas: SemanticAtlas):
    """A claimed v5 migration remains unavailable until its post-rebuild cleanup succeeds."""
    path = tmp_path / "playerlex-post-migration-checkpoint.sqlite3"
    connection = sqlite3.connect(path, factory=_CheckpointFailingConnection)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        previous = _SCHEMA_STATEMENTS[0].replace(
            "'capability', 'referent', 'scene', 'action', 'claim'",
            "'capability', 'referent', 'scene', 'action'",
        )
        for statement in (previous, *_SCHEMA_STATEMENTS[1:]):
            connection.execute(statement)
        connection.commit()
        connection.fail_checkpoint_call = 2
        with pytest.raises(PlayerLexError):
            PlayerLex(connection, atlas)
        assert connection.execute(
            "SELECT version FROM aetherstate_schema_migrations WHERE version=5"
        ).fetchone()[0] == 5
        assert tuple(connection.execute(
            f"SELECT domain, version FROM main.{_PENDING_CLEANUP}"
        )) == (("playerlex", 5),)
        connection.fail_checkpoint_call = 3
        with pytest.raises(PlayerLexError):
            PlayerLex(connection, atlas)
        connection.fail_checkpoint_call = 0
        PlayerLex(connection, atlas)
        assert tuple(connection.execute(
            f"SELECT domain, version FROM main.{_PENDING_CLEANUP}"
        )) == ()
    finally:
        connection.close()


def test_checkpoint_failure_is_retryable_and_retry_finishes_secure_removal(
    tmp_path: Path,
    glossary: CapabilityGlossary,
):
    path = tmp_path / "retry-secure-playerlex.sqlite3"
    wal_path = Path(str(path) + "-wal")
    marker = "PLAYERLEX_RETRY_DELETE_4A72C9E1"
    marker_bytes = marker.encode("utf-8")
    connection = sqlite3.connect(
        path,
        check_same_thread=False,
        factory=_CheckpointFailingConnection,
    )
    connection.row_factory = sqlite3.Row
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        service = PlayerLex(connection, glossary)
        entry = service.approve(kind="alias", surface=marker, concept_id="skill.stealth")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.checkpoint_calls = 0
        connection.fail_checkpoint_call = 2

        with pytest.raises(PlayerLexRetryableRemovalError, match="retry") as failed:
            service.remove(entry["entry_id"])
        assert failed.value.entry_id == entry["entry_id"]
        assert (
            connection.execute(
                "SELECT count(*) FROM playerlex_entries WHERE entry_id=?", (entry["entry_id"],)
            ).fetchone()[0]
            == 0
        )
        assert marker_bytes in path.read_bytes()

        connection.fail_checkpoint_call = 0
        assert service.remove(entry["entry_id"])
        assert service.remove(entry["entry_id"])
        assert marker_bytes not in path.read_bytes()
        assert not wal_path.exists() or marker_bytes not in wal_path.read_bytes()
    finally:
        connection.close()


async def test_playerlex_control_api_covers_concepts_crud_proposal_and_errors(client):
    concepts = await client.get("/aether/playerlex/concepts", params={"q": "stealth"})
    assert concepts.status_code == 200
    assert concepts.json()["schema"] == "semantic-atlas-search/1"
    assert "next_cursor" in concepts.json()
    assert any(item["concept_id"] == "skill.stealth" for item in concepts.json()["concepts"])

    created = await client.post(
        "/aether/playerlex",
        json={"kind": "alias", "surface": "Soft Shadow", "concept_id": "skill.stealth"},
    )
    assert created.status_code == 201, created.text
    entry = created.json()["entry"]
    assert entry["provenance"]["approval"] == "explicit_local"

    duplicate = await client.post(
        "/aether/playerlex",
        json={"kind": "alias", "surface": "soft shadow", "concept_id": "skill.stealth"},
    )
    assert duplicate.status_code == 409
    unknown = await client.post(
        "/aether/playerlex",
        json={"kind": "alias", "surface": "lost", "concept_id": "concept.missing"},
    )
    assert unknown.status_code == 422

    proposal = await client.post("/aether/playerlex/propose", json={"text": "Use SOFT SHADOW."})
    assert proposal.status_code == 200
    assert proposal.json()["matches"][0]["authorized"] is False

    pattern = await client.post(
        "/aether/playerlex",
        json={
            "kind": "authoring_pattern",
            "surface": "go {destination}",
            "concept_id": "skill.investigation",
        },
    )
    assert pattern.status_code == 201
    surrogate = await client.post(
        "/aether/playerlex/propose",
        content=b'{"text":"go \\ud800"}',
        headers={"content-type": "application/json"},
    )
    assert surrogate.status_code == 422
    assert "surrogate" in surrogate.json()["error"]
    escaped_scalar = await client.post(
        "/aether/playerlex/propose",
        content=b'{"text":"go \\ud83d\\ude00"}',
        headers={"content-type": "application/json"},
    )
    literal_scalar = await client.post(
        "/aether/playerlex/propose",
        json={"text": "go \U0001f600"},
    )
    assert escaped_scalar.status_code == 200
    assert escaped_scalar.json() == literal_scalar.json()
    removed_pattern = await client.delete(f"/aether/playerlex/{pattern.json()['entry']['entry_id']}")
    assert removed_pattern.status_code == 200

    incomplete = await client.patch(f"/aether/playerlex/{entry['entry_id']}", json={})
    assert incomplete.status_code == 422

    raced = await client.patch(
        f"/aether/playerlex/{entry['entry_id']}",
        json={
            "kind": entry["kind"],
            "surface": "Quiet Passage",
            "concept_id": "skill.investigation",
            "expected_meaning_fingerprint": "sha256:" + "0" * 64,
            "expected_approval_revision": entry["provenance"]["approval_revision"],
        },
    )
    assert raced.status_code == 409

    corrected = await client.patch(
        f"/aether/playerlex/{entry['entry_id']}",
        json={
            "kind": entry["kind"],
            "surface": "Quiet Passage",
            "concept_id": "skill.investigation",
            "expected_meaning_fingerprint": entry["concept"]["meaning_fingerprint"],
            "expected_approval_revision": entry["provenance"]["approval_revision"],
        },
    )
    assert corrected.status_code == 200
    assert corrected.json()["entry"]["provenance"]["approval_revision"] == 2

    listed = await client.get("/aether/playerlex")
    assert listed.status_code == 200
    assert [item["surface"] for item in listed.json()["entries"]] == ["Quiet Passage"]

    removed = await client.delete(f"/aether/playerlex/{entry['entry_id']}")
    assert removed.status_code == 200
    assert removed.json() == {"removed": True, "entry_id": entry["entry_id"]}
    missing = await client.delete(f"/aether/playerlex/{entry['entry_id']}")
    assert missing.status_code == 200
    assert missing.json() == {"removed": True, "entry_id": entry["entry_id"]}


async def test_playerlex_control_api_pages_searches_and_exactly_looks_up_cross_lex_catalog(
    client,
):
    first = await client.get("/aether/playerlex/concepts", params={"limit": 100})
    assert first.status_code == 200
    assert len(first.json()["concepts"]) == 100
    assert first.json()["next_cursor"]
    second = await client.get(
        "/aether/playerlex/concepts",
        params={"limit": 100, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert second.json()["concepts"]
    assert second.json()["concepts"][0] != first.json()["concepts"][0]

    alias = await client.get(
        "/aether/playerlex/concepts",
        params={"query": "restore machine", "lex_id": "capability", "limit": 10},
    )
    assert alias.status_code == 200
    assert any(row["concept_id"] == "action.repair" for row in alias.json()["concepts"])
    exact = await client.get(
        "/aether/playerlex/concepts",
        params={"lex_id": "action", "concept_id": "action.repair"},
    )
    assert exact.status_code == 200
    assert [(row["lex_id"], row["concept_id"]) for row in exact.json()["concepts"]] == [
        ("action", "action.repair")
    ]
    too_large = await client.get("/aether/playerlex/concepts", params={"limit": 101})
    assert too_large.status_code == 422

    for lex_id in ("capability", "action"):
        created = await client.post(
            "/aether/playerlex",
            json={
                "kind": "alias",
                "surface": "API Patchwork",
                "lex_id": lex_id,
                "concept_id": "action.repair",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["entry"]["lex_id"] == lex_id
    proposal = await client.post("/aether/playerlex/propose", json={"text": "Use API Patchwork."})
    assert proposal.status_code == 200
    assert {row["lex_id"] for row in proposal.json()["matches"]} == {
        "capability",
        "action",
    }


async def test_playerlex_control_api_repairs_and_removes_malformed_primary_keys(
    client,
    proxy_app,
):
    binary = await client.post(
        "/aether/playerlex",
        json={"kind": "alias", "surface": "API Binary ID", "concept_id": "skill.stealth"},
    )
    slash = await client.post(
        "/aether/playerlex",
        json={"kind": "alias", "surface": "API Slash ID", "concept_id": "skill.investigation"},
    )
    assert binary.status_code == slash.status_code == 201

    database = proxy_app.state.store.db
    database.execute(
        "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
        (sqlite3.Binary(b"\xff\x01"), binary.json()["entry"]["entry_id"]),
    )
    database.execute(
        "UPDATE playerlex_entries SET entry_id=? WHERE entry_id=?",
        ("api/bad/id", slash.json()["entry"]["entry_id"]),
    )
    database.commit()

    listed = await client.get("/aether/playerlex")
    entries = {item["surface"]: item for item in listed.json()["entries"]}
    binary_corrupt = entries["API Binary ID"]
    slash_corrupt = entries["API Slash ID"]
    assert "/" not in binary_corrupt["entry_id"]
    assert "/" not in slash_corrupt["entry_id"]

    repaired = await client.patch(
        f"/aether/playerlex/{binary_corrupt['entry_id']}",
        json={
            "kind": "alias",
            "surface": "API Repaired ID",
            "concept_id": "skill.investigation",
            "expected_corrupt_version": binary_corrupt["corrupt_version"],
        },
    )
    assert repaired.status_code == 200
    assert repaired.json()["entry"]["status"] == "current"

    removed = await client.delete(f"/aether/playerlex/{slash_corrupt['entry_id']}")
    assert removed.status_code == 200
    assert (
        database.execute("SELECT count(*) FROM playerlex_entries WHERE surface='API Slash ID'").fetchone()[0]
        == 0
    )


def test_console_exposes_playerlex_approval_correction_removal_and_proposal_controls():
    html = (ROOT / "src" / "aetherstate" / "static" / "console.html").read_text(encoding="utf-8")
    assert '"PlayerLex"' in html
    assert "/aether/playerlex/concepts" in html
    assert "/aether/playerlex/propose" in html
    assert "Approve locally" in html
    assert "recognizes meaning only" in html
    assert "playerLexEdit" in html
    assert "playerLexDelete" in html
    assert "expected_meaning_fingerprint" in html
    assert "expected_approval_revision" in html
    assert "expected_corrupt_version" in html
    assert 'const PRIVILEGED_STATE_TABS=new Set(["Overview","Edit"])' in html
    assert 'if(t==="Player Lessons"||t==="PlayerLex"){S=null;J=null' in html
    assert "await load(PRIVILEGED_STATE_TABS.has(t))" in html
    assert "encodeURIComponent(PL_EDIT)" in html
    assert "encodeURIComponent(id)" in html
    assert "playerLexSearchQueue" in html
    assert "setTimeout(()=>playerLexSearch(false),220)" in html
    assert "PL_NEXT_CURSOR" in html
    assert 'id="pllex"' in html
    assert "Lex-qualified" in html
    assert "current_concept||e.concept" in html
    assert "concepts?limit=300" not in html


def test_console_rerenders_and_navigation_clear_hidden_playerlex_edit_state():
    html = (ROOT / "src" / "aetherstate" / "static" / "console.html").read_text(encoding="utf-8")

    reset = html.split("function playerLexReset()", 1)[1].split("async function playerLexTab()", 1)[0]
    assert 'PL_EDIT=""' in reset
    assert "PL_CONCEPT=null" in reset
    assert "PL_CHOICES=[]" in reset
    assert "PL_NEXT_CURSOR=null" in reset

    tab = html.split("async function playerLexTab(){", 1)[1].split("const playerLexConceptText", 1)[0]
    assert tab.lstrip().startswith("playerLexReset();")

    navigation = html.split("async function go(t){", 1)[1].split("async function render()", 1)[0]
    assert navigation.index('if(tab==="PlayerLex")playerLexReset()') < navigation.index("tab=t")


def test_console_invalidates_inflight_playerlex_searches_before_debounce_and_state_changes():
    html = (ROOT / "src" / "aetherstate" / "static" / "console.html").read_text(encoding="utf-8")

    invalidator = html.split("function playerLexInvalidateSearch(){", 1)[1].split(
        "function playerLexReset()", 1
    )[0]
    assert "clearTimeout(PL_SEARCH_TIMER)" in invalidator
    assert "PL_SEARCH_TIMER=null" in invalidator
    assert "PL_SEARCH_SEQ++" in invalidator

    queued = html.split("function playerLexSearchQueue(){", 1)[1].split("async function playerLexSearch", 1)[
        0
    ]
    assert queued.index("playerLexInvalidateSearch()") < queued.index(
        "setTimeout(()=>playerLexSearch(false),220)"
    )

    for function_name, next_name in (
        ("playerLexPick", "playerLexLexChanged"),
        ("playerLexEdit", "playerLexCancel"),
        ("playerLexCancel", "playerLexDelete"),
        ("playerLexDelete", "playerLexTest"),
    ):
        body = html.split(f"function {function_name}", 1)[1].split(f"function {next_name}", 1)[0]
        assert "playerLexInvalidateSearch()" in body or "playerLexReset()" in body, function_name
