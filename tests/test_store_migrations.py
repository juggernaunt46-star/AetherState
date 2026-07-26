"""Store-core migration ownership and startup regressions."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

import aetherstate.store_schema as store_schema
from aetherstate.database_schema import database_schema_migrations
from aetherstate.schema_migrations import SchemaMigrationError, SchemaMigrationRunner
from aetherstate.store import Store
from aetherstate.store_schema import (
    STORE_CHAT_LINEAGE_VERSION,
    STORE_CORE_DOMAIN,
    STORE_CORE_FAILURE,
    STORE_CORE_VERSION,
    STORE_MIGRATION_COLUMNS,
    store_schema_migrations,
)
from aetherstate.turn_lifecycle import TURN_LIFECYCLE_DOMAIN, TURN_LIFECYCLE_SCHEMA_VERSION
from aetherstate.worldlex_store import WORLDLEX_DOMAIN, WORLDLEX_SCHEMA_VERSION
from aetherstate.schema_migrations import _normalize_sql_outside_quotes
from tests.support.schema_history import rebuild_schema_fixture, schema_snapshot


FIXTURES = Path(__file__).parent / "fixtures" / "hardening" / "schema-history"
HISTORICAL_124 = FIXTURES / "1.24.0-release-fdf71e2" / "core.schema.sql"
HISTORICAL_100 = FIXTURES / "1.0.0-release-2cd07ef" / "core.schema.sql"
HISTORICAL_CORE_FIXTURES = tuple(
    FIXTURES / name / "core.schema.sql"
    for name in (
        "1.0.0-release-2cd07ef",
        "1.1.0-release-ed63e38",
        "1.22.0-release-9091614",
        "1.23.0-release-1f4aad0",
        "1.23.0-final-34dfe8f",
        "1.24.0-release-fdf71e2",
    )
)


def _response_id() -> str:
    return "response:" + "a" * 64


def _receipt_rows(store: Store) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in store.db.execute(
            "SELECT branch_id, turn_index, lifecycle_source, response_occurrence_id, "
            "source_message_fingerprint, receipt_fingerprint "
            "FROM chat_accepted_message_receipts ORDER BY lifecycle_source"
        )
    )


def _fixture_owned_projection_hash(connection: sqlite3.Connection) -> str:
    """Test-only canonicalizer for the frozen Store-owned schema projection."""
    table_names = tuple(sorted(store_schema._CORE_TABLES))
    placeholders = ",".join("?" for _ in table_names)
    objects = tuple(
        tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE substr(name, 1, 7) <> 'sqlite_' AND "
            f"(name IN ({placeholders}) OR tbl_name IN ({placeholders})) "
            "ORDER BY type, name",
            table_names * 2,
        )
    )
    out: list[tuple[object, ...]] = []
    for object_type, name, table_name, sql in objects:
        out.append((
            "schema", object_type, name, table_name,
            None if sql is None else _normalize_sql_outside_quotes(str(sql)),
        ))
        if object_type != "table":
            continue
        quoted_table = '"' + str(name).replace('"', '""') + '"'
        for row in connection.execute(f"PRAGMA main.table_xinfo({quoted_table})"):
            out.append(("table_xinfo", name, *tuple(row)))
        for row in sorted(
            (tuple(row) for row in connection.execute(f"PRAGMA main.index_list({quoted_table})")),
            key=lambda value: str(value[1]),
        ):
            index_name = str(row[1])
            out.append(("index_list", name, index_name, *row[2:]))
            quoted_index = '"' + index_name.replace('"', '""') + '"'
            for index_row in connection.execute(f"PRAGMA main.index_xinfo({quoted_index})"):
                out.append(("index_xinfo", index_name, *tuple(index_row)))
    return hashlib.sha256(repr(tuple(out)).encode("utf-8")).hexdigest()


def test_six_fixture_projections_equal_frozen_production_identities(tmp_path: Path) -> None:
    """Changing fixture SQL, declarations, or index metadata must break frozen Store admission."""
    actual: dict[str, str] = {}
    for fixture in HISTORICAL_CORE_FIXTURES:
        path = tmp_path / f"{fixture.parent.name}.sqlite3"
        connection = rebuild_schema_fixture(path, fixture)
        actual[fixture.parent.name] = _fixture_owned_projection_hash(connection)
        connection.close()
    assert actual == store_schema.STORE_CORE_HISTORICAL_PROJECTION_HASHES


def _ledger(store: Store) -> tuple[tuple[int, str, str], ...]:
    return tuple(
        tuple(row)
        for row in store.db.execute(
            "SELECT version, name, domain FROM main.aetherstate_schema_migrations "
            "ORDER BY version"
        )
    )


def test_store_core_starts_without_optional_domain_rows(tmp_path: Path) -> None:
    """Core startup must not claim optional PlayerLex or Player Lessons ownership."""
    store = Store(tmp_path / "core-only.sqlite3")
    try:
        assert [row[0] for row in _ledger(store)] == [1, 2, 3, 4, 7]
        assert {migration.version for migration in database_schema_migrations()} == {
            1, 2, 3, 4, 5, 6, 7,
        }
    finally:
        store.close()


def _objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name FROM main.sqlite_schema "
            "WHERE substr(name, 1, 7) <> 'sqlite_' ORDER BY type, name"
        )
    )


def test_new_store_creates_current_core_and_records_versions_1_and_4(tmp_path: Path) -> None:
    """Removing either Store migration must leave its durable identity absent."""
    store = Store(tmp_path / "new.sqlite3")
    try:
        assert _ledger(store) == (
            (1, "store-core-1.24-baseline", STORE_CORE_DOMAIN),
            (WORLDLEX_SCHEMA_VERSION, "worldlex-1.24-baseline", WORLDLEX_DOMAIN),
            (TURN_LIFECYCLE_SCHEMA_VERSION, "turn-lifecycle-1.24-baseline", TURN_LIFECYCLE_DOMAIN),
            (4, "store-chat-lineage-1.24-baseline", STORE_CORE_DOMAIN),
            (7, "system-health-1.24-baseline", "system-health"),
        )
        assert {migration.version for migration in database_schema_migrations()} == {
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        }
        assert store.schema_migrations.applied() == _ledger(store)
        assert STORE_MIGRATION_COLUMNS == (
            ("caps", "native", "TEXT DEFAULT ''"),
            ("caps", "anyof", "INTEGER DEFAULT -1"),
            ("sessions", "genesis", "TEXT DEFAULT ''"),
            ("sessions", "genesis_epoch", "INTEGER DEFAULT 0"),
            ("creator_seed_receipts", "receipt_fingerprint", "TEXT DEFAULT ''"),
            ("sessions", "mode", "TEXT DEFAULT 'enriched'"),
            ("sessions", "label", "TEXT DEFAULT ''"),
            ("sessions", "narrator_speaker", "TEXT DEFAULT ''"),
            ("sessions", "experience_mode", "TEXT DEFAULT ''"),
            ("sessions", "experience_mode_source", "TEXT DEFAULT ''"),
            ("sessions", "experience_mode_locked_turn", "INTEGER"),
            ("sessions", "core_fingerprint", "TEXT DEFAULT ''"),
            ("sessions", "character_actor_id", "TEXT DEFAULT ''"),
            ("sessions", "persona_actor_id", "TEXT DEFAULT ''"),
            ("world_event_records", "source", "TEXT DEFAULT ''"),
            ("turns", "accepted_response_occurrence_id", "TEXT DEFAULT ''"),
            ("ops_journal", "lifecycle_source", "TEXT DEFAULT ''"),
            ("ops_journal", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("effect_receipts", "lifecycle_source", "TEXT DEFAULT ''"),
            ("effect_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("mechanic_settlement_receipts", "lifecycle_source", "TEXT DEFAULT ''"),
            ("mechanic_settlement_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("claim_records", "lifecycle_source", "TEXT DEFAULT ''"),
            ("claim_records", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("world_event_records", "lifecycle_source", "TEXT DEFAULT ''"),
            ("world_event_records", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("chat_continuity_seed_receipts", "lifecycle_source", "TEXT DEFAULT ''"),
            ("chat_continuity_seed_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("memories", "visibility", "TEXT DEFAULT ''"),
            ("memories", "scoped_actors", "TEXT DEFAULT '[]'"),
            ("memories", "journal_op_id", "INTEGER"),
            ("memories", "journal_op_ref", "TEXT DEFAULT ''"),
            ("memories", "source_message_fingerprint", "TEXT DEFAULT ''"),
            ("memories", "lifecycle_source", "TEXT DEFAULT ''"),
            ("memories", "response_occurrence_id", "TEXT DEFAULT ''"),
            ("memories", "source_journal_op_refs", "TEXT DEFAULT '[]'"),
        )
        assert len(STORE_MIGRATION_COLUMNS) == 36
        assert len({fixture.parent.name for fixture in HISTORICAL_CORE_FIXTURES}) == 6
    finally:
        store.close()


def test_current_124_core_is_adopted_without_rewriting_rows(tmp_path: Path) -> None:
    """A 1.24 core receives only durable migration identity and exact lineage outputs."""
    path = tmp_path / "historical.sqlite3"
    legacy = rebuild_schema_fixture(path, HISTORICAL_124)
    legacy.execute(
        "INSERT INTO sessions(session_id, external_id, active_branch, created_at, last_seen) "
        "VALUES('session:1', 'external:1', 'branch:1', 1.0, 2.0)"
    )
    legacy.execute("INSERT INTO branches(branch_id, session_id, head_turn) VALUES('branch:1','session:1',0)")
    legacy.execute(
        "INSERT INTO turns(branch_id, turn_index, klass, gen_type, accepted_response_occurrence_id) "
        "VALUES('branch:1',0,'normal','normal', ?)",
        (_response_id(),),
    )
    legacy.execute(
        "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text) "
        "VALUES('branch:1',0,'literal user text','literal assistant text')"
    )
    legacy.execute("INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts) VALUES('branch:1',0,0,'[]','rule',3.0)")
    legacy.execute("INSERT INTO checkpoints(branch_id, turn_index, state) VALUES('branch:1',0,'{}')")
    legacy.commit()
    expected = tuple(
        tuple(row)
        for row in legacy.execute(
            "SELECT session_id, external_id, active_branch, created_at, last_seen FROM sessions"
        )
    ), tuple(tuple(row) for row in legacy.execute("SELECT * FROM branches")), tuple(
        tuple(row) for row in legacy.execute("SELECT * FROM turns")
    ), tuple(tuple(row) for row in legacy.execute("SELECT * FROM ops_journal")), tuple(
        tuple(row) for row in legacy.execute("SELECT * FROM checkpoints")
    )
    legacy.close()

    store = Store(path)
    try:
        actual = tuple(
            tuple(row)
            for row in store.db.execute(
                "SELECT session_id, external_id, active_branch, created_at, last_seen FROM sessions"
            )
        ), tuple(tuple(row) for row in store.db.execute("SELECT * FROM branches")), tuple(
            tuple(row) for row in store.db.execute("SELECT * FROM turns")
        ), tuple(tuple(row) for row in store.db.execute("SELECT * FROM ops_journal")), tuple(
            tuple(row) for row in store.db.execute("SELECT * FROM checkpoints")
        )
        assert actual == expected
        assert _ledger(store) == (
            (STORE_CORE_VERSION, "store-core-1.24-baseline", STORE_CORE_DOMAIN),
            (WORLDLEX_SCHEMA_VERSION, "worldlex-1.24-baseline", WORLDLEX_DOMAIN),
            (TURN_LIFECYCLE_SCHEMA_VERSION, "turn-lifecycle-1.24-baseline", TURN_LIFECYCLE_DOMAIN),
            (STORE_CHAT_LINEAGE_VERSION, "store-chat-lineage-1.24-baseline", STORE_CORE_DOMAIN),
            (7, "system-health-1.24-baseline", "system-health"),
        )
        receipts = _receipt_rows(store)
        assert len(receipts) == 3
        assert {row[2] for row in receipts} == {
            "user_text", "assistant_response", "deferred_extraction",
        }
    finally:
        store.close()


def test_core_transform_failure_rolls_back_every_column_index_backfill_and_ledger_row(tmp_path: Path) -> None:
    """A transform failure cannot leave a partial Store migration durable."""
    path = tmp_path / "rollback.sqlite3"
    connection = rebuild_schema_fixture(path, HISTORICAL_100)
    before = schema_snapshot(connection)
    real = store_schema_migrations()[0]

    def transform_then_interrupt(db: sqlite3.Connection) -> None:
        real.transform(db)
        db.execute("CREATE TRIGGER rollback_receipt_probe AFTER INSERT ON memories BEGIN SELECT 1; END")
        raise RuntimeError("interrupt after real Store transform")

    failing = replace(real, transform=transform_then_interrupt)
    runner = SchemaMigrationRunner(connection, threading.RLock(), (failing,))
    with pytest.raises(SchemaMigrationError) as raised:
        runner.run_domain(STORE_CORE_DOMAIN)
    assert raised.value.code == STORE_CORE_FAILURE
    assert schema_snapshot(connection) == before
    assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name='aetherstate_schema_migrations'").fetchone() is None
    connection.close()


def test_store_reopen_performs_no_schema_or_data_mutation(tmp_path: Path) -> None:
    """Once both versions are ledgered, reopening Store emits no schema or data writes."""
    path = tmp_path / "reopen.sqlite3"
    first = Store(path)
    first.db.execute("CREATE TABLE sentinel(value TEXT)")
    first.db.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
    first.db.commit()
    before_schema = schema_snapshot(first.db)
    before_ledger = _ledger(first)
    first.close()
    trace: list[str] = []
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.set_trace_callback(trace.append)
    original_connect = sqlite3.connect
    sqlite3.connect = lambda *_args, **_kwargs: connection  # type: ignore[assignment]
    try:
        second = Store(path)
        assert schema_snapshot(second.db) == before_schema
        assert _ledger(second) == before_ledger
        assert second.db.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
        assert any(statement.lstrip().upper().startswith("BEGIN IMMEDIATE") for statement in trace)
        assert not any(
            "INSERT INTO main.aetherstate_schema_migrations" in statement
            or statement.lstrip().upper().startswith("ALTER TABLE")
            for statement in trace
        )
    finally:
        sqlite3.connect = original_connect  # type: ignore[assignment]
        second.close()


def test_store_lock_exists_before_any_schema_transform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema callbacks receive Store's re-entrant transaction lock, not a late lock."""
    observed: list[bool] = []
    original_registry = database_schema_migrations()
    store_core_v4 = next(
        migration
        for migration in original_registry
        if (
            migration.version,
            migration.name,
            migration.domain,
        )
        == (4, "store-chat-lineage-1.24-baseline", STORE_CORE_DOMAIN)
    )
    probe = replace(
        store_core_v4,
        version=max(migration.version for migration in original_registry) + 1,
        name="store-lock-probe",
        failure_code="store_lock_probe_invalid",
        is_current=lambda _db: bool(observed),
        applies=lambda _db: True,
        transform=lambda _db: observed.append(bool(self_lock._is_owned())),
        postcondition=lambda _db: bool(observed),
        requires_cleanup=False,
    )
    import aetherstate.store as store_module

    self_lock = None

    def registry():
        return original_registry + (probe,)

    monkeypatch.setattr(store_module, "database_schema_migrations", registry)
    original_init = SchemaMigrationRunner.__init__

    def capture_init(self, connection, lock, migrations, **kwargs):
        nonlocal self_lock
        self_lock = lock
        return original_init(self, connection, lock, migrations, **kwargs)

    monkeypatch.setattr(SchemaMigrationRunner, "__init__", capture_init)
    store = Store(tmp_path / "lock.sqlite3")
    try:
        assert observed == [True]
    finally:
        store.close()


def test_foreign_core_column_or_index_collision_fails_before_mutation(tmp_path: Path) -> None:
    """A foreign Store-owned namespace collision is refused before any migration writes."""
    path = tmp_path / "collision.sqlite3"
    connection = rebuild_schema_fixture(path, HISTORICAL_124)
    connection.execute("ALTER TABLE branches ADD COLUMN foreign_core TEXT")
    connection.commit()
    before = _objects(connection)
    connection.close()
    with pytest.raises(SchemaMigrationError) as raised:
        Store(path)
    assert raised.value.code == STORE_CORE_FAILURE
    after = sqlite3.connect(path)
    try:
        assert _objects(after) == before
    finally:
        after.close()


def test_exact_current_core_mixed_case_temp_collision_rejects_before_current_fast_path_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite-equivalent TEMP names must fail before exact-current admission."""
    path = tmp_path / "current-temp-collision.sqlite3"
    seeded = Store(path)
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations")
    seeded.db.commit()
    before = schema_snapshot(seeded.db)
    seeded.close()
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("CREATE TEMP TABLE SESSIONS(foreign_core TEXT)")
    import aetherstate.store as store_module

    monkeypatch.setattr(store_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    with pytest.raises(SchemaMigrationError) as raised:
        Store(path)
    assert raised.value.code == STORE_CORE_FAILURE
    assert schema_snapshot(connection) == before
    assert tuple(connection.execute("SELECT version FROM aetherstate_schema_migrations")) == ()
    connection.close()


def test_exact_current_core_temp_index_name_collision_rejects_before_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting Store-owned index names from v1's TEMP guard must fail closed."""
    path = tmp_path / "current-temp-index-collision.sqlite3"
    seeded = Store(path)
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations")
    seeded.db.commit()
    before = schema_snapshot(seeded.db)
    seeded.close()
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("CREATE TEMP TABLE idx_director_branch(foreign_core TEXT)")
    import aetherstate.store as store_module

    monkeypatch.setattr(store_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    with pytest.raises(SchemaMigrationError) as raised:
        Store(path)
    assert raised.value.code == STORE_CORE_FAILURE
    assert schema_snapshot(connection) == before
    assert tuple(connection.execute("SELECT version FROM aetherstate_schema_migrations")) == ()
    connection.close()


def test_exact_current_lineage_temp_collision_rejects_before_any_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the separately owned lineage table must not let v1 ledger first."""
    path = tmp_path / "current-lineage-temp-collision.sqlite3"
    seeded = Store(path)
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations")
    seeded.db.commit()
    before = schema_snapshot(seeded.db)
    seeded.close()
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute(
        "CREATE TEMP TABLE chat_accepted_message_receipts(foreign_core TEXT)"
    )
    import aetherstate.store as store_module

    monkeypatch.setattr(store_module.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    with pytest.raises(SchemaMigrationError) as raised:
        Store(path)
    assert tuple(connection.execute("SELECT version FROM aetherstate_schema_migrations")) == ()
    assert raised.value.code == STORE_CORE_FAILURE
    assert schema_snapshot(connection) == before
    connection.close()


def test_lineage_response_index_identity_repairs_sql_index_list_and_xinfo_before_ledger(
    tmp_path: Path,
) -> None:
    """Reducing v4 index validation to column names must not ledger altered metadata."""
    path = tmp_path / "lineage-index-identity.sqlite3"
    seeded = Store(path)
    seeded.db.execute("DROP INDEX idx_chat_accepted_message_receipts_response")
    seeded.db.execute(
        "CREATE INDEX idx_chat_accepted_message_receipts_response ON "
        "chat_accepted_message_receipts(branch_id COLLATE NOCASE, turn_index DESC, "
        "lifecycle_source, response_occurrence_id)"
    )
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    seeded.db.commit()
    seeded.close()

    repaired = Store(path)
    try:
        index_sql = repaired.db.execute(
            "SELECT sql FROM main.sqlite_schema WHERE type='index' "
            "AND name='idx_chat_accepted_message_receipts_response'"
        ).fetchone()[0]
        assert _normalize_sql_outside_quotes(str(index_sql)) == (
            "create index idx_chat_accepted_message_receipts_response on "
            "chat_accepted_message_receipts(branch_id,turn_index,lifecycle_source,"
            "response_occurrence_id)"
        )
        assert tuple(
            tuple(row)
            for row in repaired.db.execute(
                "PRAGMA main.index_list('chat_accepted_message_receipts')"
            )
        ) == (
            (0, "idx_chat_accepted_message_receipts_response", 0, "c", 0),
            (1, "sqlite_autoindex_chat_accepted_message_receipts_1", 1, "pk", 0),
        )
        assert tuple(
            tuple(row)
            for row in repaired.db.execute(
                "PRAGMA main.index_xinfo('idx_chat_accepted_message_receipts_response')"
            )
        ) == (
            (0, 0, "branch_id", 0, "BINARY", 1),
            (1, 1, "turn_index", 0, "BINARY", 1),
            (2, 2, "lifecycle_source", 0, "BINARY", 1),
            (3, 3, "response_occurrence_id", 0, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        )
        assert any(row[0] == STORE_CHAT_LINEAGE_VERSION for row in _ledger(repaired))
    finally:
        repaired.close()


def test_receipts_complete_unique_memory_lineage_is_repaired_and_ambiguous_stays_unpromoted(
    tmp_path: Path,
) -> None:
    """A receipt-only v4 postcondition must not ledger blank uniquely provable memory lineage."""
    path = tmp_path / "receipts-complete-lineage-blank.sqlite3"
    seeded = Store(path)
    response_id = _response_id()
    seeded.db.execute("INSERT INTO branches(branch_id) VALUES('branch:1')")
    seeded.db.execute(
        "INSERT INTO turns(branch_id, turn_index, accepted_response_occurrence_id) VALUES(?,?,?)",
        ("branch:1", 2, response_id),
    )
    seeded.db.execute(
        "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text) VALUES(?,?,?,?)",
        ("branch:1", 2, "user text", "assistant text"),
    )
    seeded.db.executemany(
        "INSERT INTO memories(memory_id, branch_id, text, participants, tags, importance, created_turn, "
        "lifecycle_source, response_occurrence_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ("memory:unique", "branch:1", "uniquely provable", '["p"]', '["t"]', 3, 2, "assistant_response", response_id),
            ("memory:ambiguous", "branch:1", "ambiguous", '["p"]', '["t"]', 3, 2, "assistant_response", response_id),
        ),
    )
    unique_journal = seeded.db.execute(
        "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, lifecycle_source, response_occurrence_id) "
        "VALUES('branch:1',2,2,?,'assistant_response',?)",
        (json.dumps([{"op": "memory_event", "text": "uniquely provable", "participants": ["p"], "tags": ["t"], "importance": 3}]), response_id),
    ).lastrowid
    for _ in range(2):
        seeded.db.execute(
            "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, lifecycle_source, response_occurrence_id) "
            "VALUES('branch:1',2,2,?,'assistant_response',?)",
            (json.dumps([{"op": "memory_event", "text": "ambiguous", "participants": ["p"], "tags": ["t"], "importance": 3}]), response_id),
        )
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    seeded.db.commit()
    seeded.close()

    first_repair = Store(path)
    first_repair.db.execute(
        "UPDATE memories SET journal_op_id=NULL, journal_op_ref='', source_message_fingerprint='' "
        "WHERE memory_id='memory:unique'"
    )
    first_repair.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    first_repair.db.commit()
    first_repair.close()

    repaired = Store(path)
    try:
        assert tuple(
            repaired.db.execute(
                "SELECT journal_op_id, journal_op_ref, source_message_fingerprint FROM memories "
                "WHERE memory_id='memory:unique'"
            ).fetchone()
        ) == (
            unique_journal,
            f"{unique_journal}:0",
            "sha256:b093f6e8c5e545b441d51e5ff50b68adbb4b773d0fa6d547a5c3e4ee3ef19bf5",
        )
        assert tuple(
            repaired.db.execute(
                "SELECT journal_op_id, journal_op_ref, source_message_fingerprint FROM memories "
                "WHERE memory_id='memory:ambiguous'"
            ).fetchone()
        ) == (None, "", "")
        assert len(_receipt_rows(repaired)) == 3
        assert any(row[0] == STORE_CHAT_LINEAGE_VERSION for row in _ledger(repaired))
    finally:
        repaired.close()


def test_exact_historical_admission_rejects_dropped_altered_index_and_temp_owned_objects(tmp_path: Path) -> None:
    """Each non-reviewed Store-owned projection is refused before the real transform."""
    mutations = (
        ("dropped", "DROP TABLE notes"),
        ("wrong-index", "DROP INDEX idx_director_branch; CREATE INDEX idx_director_branch ON director(turn_index, branch_id)"),
        ("temp", "CREATE TEMP TABLE sessions(foreign_core TEXT)"),
    )
    for label, mutation in mutations:
        path = tmp_path / f"{label}.sqlite3"
        connection = rebuild_schema_fixture(path, HISTORICAL_124)
        connection.executescript(mutation)
        if label != "temp":
            connection.commit()
        before = schema_snapshot(connection)
        original_connect = sqlite3.connect
        if label == "temp":
            sqlite3.connect = lambda *_args, **_kwargs: connection  # type: ignore[assignment]
        else:
            connection.close()
        try:
            with pytest.raises(SchemaMigrationError) as raised:
                Store(path)
        finally:
            sqlite3.connect = original_connect  # type: ignore[assignment]
        assert raised.value.code == STORE_CORE_FAILURE
        after = sqlite3.connect(path)
        try:
            assert schema_snapshot(after) == before
        finally:
            after.close()


def test_lineage_schema_and_data_completion_repair_or_rejects_before_ledger(tmp_path: Path) -> None:
    """A current-looking lineage table is not current until receipts are deterministically complete."""
    path = tmp_path / "lineage-incomplete.sqlite3"
    store = Store(path)
    store.db.execute(
        "INSERT INTO branches(branch_id) VALUES('branch:1')"
    )
    store.db.execute(
        "INSERT INTO turns(branch_id, turn_index, accepted_response_occurrence_id) VALUES(?,?,?)",
        ("branch:1", 2, _response_id()),
    )
    store.db.execute(
        "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text) VALUES(?,?,?,?)",
        ("branch:1", 2, "u", "a"),
    )
    store.db.execute("DELETE FROM chat_accepted_message_receipts")
    store.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    store.db.commit()
    store.close()
    repaired = Store(path)
    try:
        assert len(_receipt_rows(repaired)) == 3
        assert any(row[0] == STORE_CHAT_LINEAGE_VERSION for row in _ledger(repaired))
    finally:
        repaired.close()


def test_unique_memory_lineage_skips_malformed_journal_and_promotes_later_unique_match(tmp_path: Path) -> None:
    """A malformed unrelated journal payload cannot block a later unique lineage proof."""
    path = tmp_path / "malformed-journal.sqlite3"
    store = Store(path)
    store.db.execute("INSERT INTO branches(branch_id) VALUES('branch:1')")
    store.db.execute(
        "INSERT INTO turns(branch_id, turn_index, accepted_response_occurrence_id) VALUES(?,?,?)",
        ("branch:1", 2, _response_id()),
    )
    store.db.execute(
        "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text) VALUES(?,?,?,?)",
        ("branch:1", 2, "u", "a"),
    )
    store.db.execute("DELETE FROM chat_accepted_message_receipts")
    store.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    store.db.execute(
        "INSERT INTO memories(memory_id, branch_id, text, participants, tags, importance, created_turn, lifecycle_source, response_occurrence_id) "
        "VALUES('memory:1','branch:1','remember this','[\"p\"]','[\"t\"]',3,2,'assistant_response',?)",
        (_response_id(),),
    )
    store.db.execute(
        "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, lifecycle_source, response_occurrence_id) VALUES('branch:1',2,2,'not-json','assistant_response',?)",
        (_response_id(),),
    )
    store.db.execute(
        "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, lifecycle_source, response_occurrence_id) VALUES('branch:1',2,2,?,'assistant_response',?)",
        (json.dumps([{"op": "memory_event", "text": "remember this", "participants": ["p"], "tags": ["t"], "importance": 3}]), _response_id()),
    )
    store.db.commit()
    store.close()
    repaired = Store(path)
    try:
        row = repaired.db.execute(
            "SELECT journal_op_ref, source_message_fingerprint FROM memories WHERE memory_id='memory:1'"
        ).fetchone()
        assert str(row["journal_op_ref"] or "").endswith(":0")
        assert str(row["source_message_fingerprint"] or "").startswith("sha256:")
    finally:
        repaired.close()


def test_receipts_complete_ambiguous_memory_false_lineage_is_cleared_before_ledger(
    tmp_path: Path,
) -> None:
    """Treating ambiguous memory lineage as current must not preserve forged lineage fields."""
    path = tmp_path / "receipts-complete-ambiguous-lineage.sqlite3"
    seeded = Store(path)
    response_id = _response_id()
    seeded.db.execute("INSERT INTO branches(branch_id) VALUES('branch:1')")
    seeded.db.execute(
        "INSERT INTO turns(branch_id, turn_index, accepted_response_occurrence_id) VALUES(?,?,?)",
        ("branch:1", 2, response_id),
    )
    seeded.db.execute(
        "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text) VALUES(?,?,?,?)",
        ("branch:1", 2, "user text", "assistant text"),
    )
    seeded.db.execute(
        "INSERT INTO memories(memory_id, branch_id, text, participants, tags, importance, created_turn, "
        "lifecycle_source, response_occurrence_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("memory:ambiguous", "branch:1", "ambiguous", '["p"]', '["t"]', 3, 2, "assistant_response", response_id),
    )
    for _ in range(2):
        seeded.db.execute(
            "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, lifecycle_source, response_occurrence_id) "
            "VALUES('branch:1',2,2,?,'assistant_response',?)",
            (json.dumps([{"op": "memory_event", "text": "ambiguous", "participants": ["p"], "tags": ["t"], "importance": 3}]), response_id),
        )
    seeded.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    seeded.db.commit()
    seeded.close()

    first_repair = Store(path)
    first_repair.db.execute(
        "UPDATE memories SET journal_op_id=1, journal_op_ref='1:0', "
        "source_message_fingerprint='sha256:ffff...' WHERE memory_id='memory:ambiguous'"
    )
    first_repair.db.execute("DELETE FROM aetherstate_schema_migrations WHERE version=4")
    first_repair.db.commit()
    first_repair.close()

    repaired = Store(path)
    try:
        assert tuple(
            repaired.db.execute(
                "SELECT journal_op_id, journal_op_ref, source_message_fingerprint FROM memories "
                "WHERE memory_id='memory:ambiguous'"
            ).fetchone()
        ) == (None, "", "")
        assert len(_receipt_rows(repaired)) == 3
        assert any(row[0] == STORE_CHAT_LINEAGE_VERSION for row in _ledger(repaired))
    finally:
        repaired.close()


def test_abandoned_genesis_claim_recovers_on_every_relevant_start_not_only_first_migration(tmp_path: Path) -> None:
    """Operational genesis recovery is recurring even when the migration ledger is unchanged."""
    path = tmp_path / "genesis.sqlite3"
    first = Store(path)
    first.db.execute("INSERT INTO sessions(session_id, genesis, genesis_epoch) VALUES('session:1','llm',3)")
    first.db.commit()
    first.close()
    second = Store(path)
    try:
        assert tuple(second.db.execute("SELECT genesis, genesis_epoch FROM sessions").fetchone()) == ("rules", 4)
        second.db.execute("UPDATE sessions SET genesis='llm'")
        second.db.commit()
    finally:
        second.close()
    third = Store(path)
    try:
        assert tuple(third.db.execute("SELECT genesis, genesis_epoch FROM sessions").fetchone()) == ("rules", 5)
    finally:
        third.close()


def test_store_migration_leaves_foreign_keys_default_unchanged(tmp_path: Path) -> None:
    """Store initialization must not mutate the caller's foreign-key setting."""
    path = tmp_path / "foreign-keys.sqlite3"
    store = Store(path)
    try:
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        store.close()
