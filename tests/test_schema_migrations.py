from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Callable

import pytest

from aetherstate.schema_migrations import (
    DOMAIN_INVALID,
    LEDGER_IDENTITY_INVALID,
    LEDGER_ROW_INVALID,
    LEDGER_SCHEMA_INVALID,
    LEDGER_UNKNOWN_VERSION,
    REGISTRY_INVALID,
    TRANSACTION_ACTIVE,
    SchemaMigration,
    SchemaMigrationError,
    SchemaMigrationRunner,
)


LEDGER = "aetherstate_schema_migrations"
FAILURE = "migration_owned_invalid"
ROOT = Path(__file__).resolve().parents[1]


def _connection(path: Path | str = ":memory:") -> sqlite3.Connection:
    return sqlite3.connect(path, check_same_thread=False)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _exact_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE aetherstate_schema_migrations("
        "version INTEGER PRIMARY KEY CHECK(typeof(version)='integer' AND version > 0),"
        "name TEXT NOT NULL UNIQUE CHECK(name <> ''),"
        "domain TEXT NOT NULL CHECK(domain <> ''),"
        "applied_at REAL NOT NULL CHECK("
        "typeof(applied_at) IN ('integer','real') AND applied_at > 0))"
    )


def _temp_exact_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TEMP TABLE aetherstate_schema_migrations("
        "version INTEGER PRIMARY KEY CHECK(typeof(version)='integer' AND version > 0),"
        "name TEXT NOT NULL UNIQUE CHECK(name <> ''),"
        "domain TEXT NOT NULL CHECK(domain <> ''),"
        "applied_at REAL NOT NULL CHECK("
        "typeof(applied_at) IN ('integer','real') AND applied_at > 0))"
    )


def _migration(
    *,
    version: int = 1,
    name: str = "owned-table",
    domain: str = "owned",
    failure_code: str = FAILURE,
    applies: Callable[[sqlite3.Connection], bool] | None = None,
    is_current: Callable[[sqlite3.Connection], bool] | None = None,
    transform: Callable[[sqlite3.Connection], None] | None = None,
    postcondition: Callable[[sqlite3.Connection], bool] | None = None,
    requires_cleanup: bool = False,
) -> SchemaMigration:
    current = is_current or (lambda connection: _has_table(connection, "owned"))
    return SchemaMigration(
        version=version,
        name=name,
        domain=domain,
        failure_code=failure_code,
        applies=applies or (lambda connection: not current(connection)),
        is_current=current,
        transform=transform or (lambda connection: connection.execute("CREATE TABLE owned(value TEXT)")),
        postcondition=postcondition or current,
        requires_cleanup=requires_cleanup,
    )


def _runner(
    connection: sqlite3.Connection,
    migrations: tuple[SchemaMigration, ...] | list[SchemaMigration],
    *,
    clock: Callable[[], float] = lambda: 100.0,
) -> SchemaMigrationRunner:
    return SchemaMigrationRunner(connection, threading.RLock(), migrations, clock=clock)


@pytest.mark.parametrize(
    "duplicate",
    [
        [_migration(), _migration(version=2, name="owned-table")],
        [_migration(), _migration(version=1, name="second-owned-table")],
    ],
)
def test_duplicate_registry_identity_fails_before_creating_ledger(
    duplicate: list[SchemaMigration],
) -> None:
    connection = _connection()

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, duplicate).run_domain("owned")

    assert raised.value.code == REGISTRY_INVALID
    assert str(raised.value) == REGISTRY_INVALID
    assert not _has_table(connection, LEDGER)


def test_success_records_only_after_postcondition() -> None:
    connection = _connection()
    observed_ledger_counts: list[int] = []

    def postcondition(candidate: sqlite3.Connection) -> bool:
        observed_ledger_counts.append(
            candidate.execute(f"SELECT count(*) FROM {LEDGER}").fetchone()[0]
            if _has_table(candidate, LEDGER)
            else 0
        )
        return _has_table(candidate, "owned")

    runner = _runner(connection, [_migration(postcondition=postcondition)])

    assert runner.run_domain("owned") == (1,)
    assert observed_ledger_counts == [0]
    assert runner.applied() == ((1, "owned-table", "owned"),)


def test_transform_failure_rolls_back_schema_data_and_ledger() -> None:
    connection = _connection()

    def exploding_transform(candidate: sqlite3.Connection) -> None:
        candidate.execute("CREATE TABLE temporary_sentinel(value TEXT)")
        candidate.execute("INSERT INTO temporary_sentinel(value) VALUES ('sentinel')")
        raise RuntimeError("not public")

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration(transform=exploding_transform)]).run_domain("owned")

    assert raised.value.code == FAILURE
    assert str(raised.value) == FAILURE
    assert not _has_table(connection, "temporary_sentinel")
    assert not _has_table(connection, LEDGER)
    assert not connection.in_transaction


def test_current_unledgered_domain_adopts_without_transform() -> None:
    connection = _connection()
    connection.execute("CREATE TABLE owned(value TEXT)")
    connection.row_factory = sqlite3.Row
    transforms: list[str] = []

    def transform(candidate: sqlite3.Connection) -> None:
        transforms.append("called")
        candidate.execute("CREATE TABLE should_not_exist(value TEXT)")

    runner = _runner(connection, [_migration(transform=transform)])

    assert runner.run_domain("owned") == (1,)
    assert transforms == []
    assert runner.applied() == ((1, "owned-table", "owned"),)
    assert not _has_table(connection, "should_not_exist")


def test_second_run_is_a_zero_mutation_noop() -> None:
    from tests.support.schema_history import ledger_rows, schema_snapshot

    connection = _connection()
    connection.execute("CREATE TABLE caller_sentinel(value TEXT)")
    connection.execute("INSERT INTO caller_sentinel(value) VALUES ('preserved')")
    connection.commit()
    runner = _runner(connection, [_migration()])
    assert runner.run_domain("owned") == (1,)
    changes_before = connection.total_changes
    schema_before = schema_snapshot(connection)
    ledger_before = ledger_rows(connection)
    sentinel_before = connection.execute("SELECT value FROM caller_sentinel").fetchall()
    trace: list[str] = []
    connection.set_trace_callback(trace.append)

    try:
        assert runner.run_domain("owned") == ()
    finally:
        connection.set_trace_callback(None)

    assert connection.total_changes == changes_before
    assert schema_snapshot(connection) == schema_before
    assert ledger_rows(connection) == ledger_before
    assert connection.execute("SELECT value FROM caller_sentinel").fetchall() == sentinel_before
    assert not [
        statement
        for statement in trace
        if statement.lstrip().upper().startswith(
            ("BEGIN", "COMMIT", "ROLLBACK", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")
        )
    ]
    assert not connection.in_transaction


@pytest.mark.parametrize(
    ("version", "name", "code"),
    [(2, "future", LEDGER_UNKNOWN_VERSION), (1, "other-name", LEDGER_IDENTITY_INVALID)],
)
def test_unknown_ahead_or_changed_ledger_identity_fails_content_free(
    version: int, name: str, code: str
) -> None:
    connection = _connection()
    _exact_ledger(connection)
    connection.execute(
        f"INSERT INTO {LEDGER}(version, name, domain, applied_at) VALUES (?, ?, ?, ?)",
        (version, name, "owned", 100.0),
    )
    connection.commit()

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration()]).run_domain("owned")

    assert raised.value.code == code
    assert str(raised.value) == code


def test_malformed_ledger_shape_fails_before_other_schema_write() -> None:
    connection = _connection()
    connection.execute(f"CREATE TABLE {LEDGER}(version INTEGER)")
    before = tuple(connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name"))

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration()]).run_domain("owned")

    assert raised.value.code == LEDGER_SCHEMA_INVALID
    assert tuple(connection.execute("SELECT type, name, sql FROM sqlite_schema ORDER BY name")) == before
    assert not _has_table(connection, "owned")


def test_sparse_domain_execution_does_not_require_a_contiguous_prefix() -> None:
    connection = _connection()
    optional = _migration(version=2, name="optional-table", domain="optional")
    runner = _runner(connection, [_migration(), optional])

    assert runner.run_domain("optional") == (2,)
    assert runner.applied() == ((2, "optional-table", "optional"),)
    assert _has_table(connection, "owned")
    with pytest.raises(SchemaMigrationError) as raised:
        runner.run_domain("missing")
    assert raised.value.code == DOMAIN_INVALID


def test_concurrent_domain_runs_serialize_and_record_once() -> None:
    connection = _connection()
    lock = threading.RLock()
    barrier = threading.Barrier(2)
    transforms: list[str] = []
    results: list[tuple[int, ...]] = []
    errors: list[BaseException] = []

    def transform(candidate: sqlite3.Connection) -> None:
        transforms.append("called")
        candidate.execute("CREATE TABLE owned(value TEXT)")

    runner = SchemaMigrationRunner(connection, lock, [_migration(transform=transform)], clock=lambda: 100.0)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(runner.run_domain("owned"))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join()
    second.join()

    assert errors == []
    assert sorted(results) == [(), (1,)]
    assert transforms == ["called"]
    assert runner.applied() == ((1, "owned-table", "owned"),)
    assert not connection.in_transaction


def test_active_outer_transaction_is_refused_without_commit() -> None:
    connection = _connection()
    connection.execute("CREATE TABLE caller_sentinel(value TEXT)")
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_sentinel(value) VALUES ('preserved')")

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration()]).run_domain("owned")

    assert raised.value.code == TRANSACTION_ACTIVE
    assert connection.in_transaction
    assert connection.execute("SELECT value FROM caller_sentinel").fetchone() == ("preserved",)
    assert not _has_table(connection, LEDGER)
    connection.rollback()


def test_external_exact_ledger_completion_becomes_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "external-completion.db"
    connection = _connection(path)
    transforms: list[str] = []
    applies_calls = 0

    def applies(candidate: sqlite3.Connection) -> bool:
        nonlocal applies_calls
        applies_calls += 1
        if applies_calls == 1:
            external = _connection(path)
            _exact_ledger(external)
            external.execute(
                f"INSERT INTO {LEDGER}(version, name, domain, applied_at) VALUES (1, 'owned-table', 'owned', 100.0)"
            )
            external.commit()
            external.close()
        return True

    def transform(candidate: sqlite3.Connection) -> None:
        transforms.append("called")
        candidate.execute("CREATE TABLE owned(value TEXT)")

    migration = _migration(
        applies=applies,
        is_current=lambda candidate: False,
        transform=transform,
        postcondition=lambda candidate: True,
    )
    runner = _runner(connection, [migration])

    assert runner.run_domain("owned") == ()
    assert transforms == []
    assert runner.applied() == ((1, "owned-table", "owned"),)
    assert not connection.in_transaction


@pytest.mark.parametrize("method", ["run_domain", "applied"])
def test_initial_ledger_sqlite_failures_are_content_free(method: str) -> None:
    connection = _connection()
    runner = _runner(connection, [_migration()])
    connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)

    try:
        with pytest.raises(SchemaMigrationError) as raised:
            if method == "run_domain":
                runner.run_domain("owned")
            else:
                runner.applied()
    finally:
        connection.set_authorizer(None)

    assert raised.value.code == LEDGER_SCHEMA_INVALID
    assert str(raised.value) == LEDGER_SCHEMA_INVALID


def test_ledger_literal_case_drift_fails_before_transform() -> None:
    connection = _connection()
    connection.execute(
        "CREATE TABLE aetherstate_schema_migrations("
        "version INTEGER PRIMARY KEY CHECK(typeof(version)='INTEGER' AND version > 0),"
        "name TEXT NOT NULL UNIQUE CHECK(name <> ''),"
        "domain TEXT NOT NULL CHECK(domain <> ''),"
        "applied_at REAL NOT NULL CHECK("
        "typeof(applied_at) IN ('integer','real') AND applied_at > 0))"
    )
    transformed: list[str] = []

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration(transform=lambda candidate: transformed.append("called"))]).run_domain(
            "owned"
        )

    assert raised.value.code == LEDGER_SCHEMA_INVALID
    assert transformed == []


def test_bogus_sql_null_ledger_index_fails_before_callbacks() -> None:
    connection = _connection()
    _exact_ledger(connection)
    connection.execute("PRAGMA writable_schema=ON")
    connection.execute(
        "INSERT INTO main.sqlite_schema(type, name, tbl_name, rootpage, sql) "
        "VALUES ('index', 'bogus_autoindex', ?, 0, NULL)",
        (LEDGER,),
    )
    connection.execute("PRAGMA writable_schema=OFF")
    connection.commit()
    transformed: list[str] = []

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration(transform=lambda candidate: transformed.append("called"))]).run_domain(
            "owned"
        )

    assert raised.value.code == LEDGER_SCHEMA_INVALID
    assert transformed == []


@pytest.mark.parametrize(
    "statement",
    (
        "CREATE TABLE unrelated(value TEXT DEFAULT 'aetherstate_schema_pending_cleanup')",
        "CREATE VIEW unrelated AS SELECT 1 /* aetherstate_schema_pending_cleanup */",
    ),
)
def test_cleanup_discovery_ignores_marker_text_in_literals_and_comments(statement: str) -> None:
    connection = _connection()
    connection.execute(statement)
    runner = _runner(connection, [_migration(requires_cleanup=True)])

    assert runner.cleanup_pending("owned", 1) is False
    assert not _has_table(connection, "aetherstate_schema_pending_cleanup")


def test_temp_ledger_collision_refuses_before_transform_or_durable_success() -> None:
    connection = _connection()
    _temp_exact_ledger(connection)
    transforms: list[str] = []

    def transform(candidate: sqlite3.Connection) -> None:
        transforms.append("called")
        candidate.execute("CREATE TABLE owned(value TEXT)")

    runner = _runner(
        connection,
        [_migration(transform=transform)],
    )

    with pytest.raises(SchemaMigrationError) as raised:
        runner.run_domain("owned")

    assert raised.value.code == LEDGER_SCHEMA_INVALID
    assert transforms == []
    with pytest.raises(SchemaMigrationError) as applied_raised:
        runner.applied()
    assert applied_raised.value.code == LEDGER_SCHEMA_INVALID
    assert not _has_table(connection, LEDGER)
    assert connection.execute(f"SELECT count(*) FROM temp.{LEDGER}").fetchone() == (0,)


def test_applicability_is_rechecked_after_begin_before_any_write(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    connection = _connection(path)
    connection.execute("CREATE TABLE recognized_before_lock(value TEXT)")
    connection.commit()
    calls = 0

    def applies(candidate: sqlite3.Connection) -> bool:
        nonlocal calls
        calls += 1
        recognized = _has_table(candidate, "recognized_before_lock")
        if calls == 1:
            replacement = _connection(path)
            replacement.execute("DROP TABLE recognized_before_lock")
            replacement.execute("CREATE TABLE replaced_shape(value TEXT)")
            replacement.commit()
            replacement.close()
        return recognized

    migration = _migration(
        applies=applies,
        is_current=lambda candidate: _has_table(candidate, "owned"),
    )
    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [migration]).run_domain("owned")

    assert raised.value.code == FAILURE
    assert calls == 2
    assert not _has_table(connection, "owned")
    assert not _has_table(connection, LEDGER)
    assert not connection.in_transaction


@pytest.mark.parametrize("applied_at", [0, "not-a-clock"])
def test_malformed_ledger_row_fails_before_other_schema_write(applied_at: object) -> None:
    connection = _connection()
    _exact_ledger(connection)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        f"INSERT INTO {LEDGER}(version, name, domain, applied_at) VALUES (1, 'owned-table', 'owned', ?)",
        (applied_at,),
    )
    connection.execute("PRAGMA ignore_check_constraints=OFF")
    connection.commit()

    with pytest.raises(SchemaMigrationError) as raised:
        _runner(connection, [_migration()]).run_domain("owned")

    assert raised.value.code == LEDGER_ROW_INVALID
    assert not _has_table(connection, "owned")


@pytest.mark.parametrize("foreign_keys", ["OFF", "ON"])
def test_foreign_key_pragma_is_preserved_for_caller_setting(foreign_keys: str) -> None:
    def enabled(connection: sqlite3.Connection) -> int:
        return connection.execute("PRAGMA foreign_keys").fetchone()[0]

    expected = 1 if foreign_keys == "ON" else 0

    success = _connection()
    success.execute(f"PRAGMA foreign_keys={foreign_keys}")
    success_runner = _runner(success, [_migration()])
    assert success_runner.run_domain("owned") == (1,)
    assert enabled(success) == expected
    assert success_runner.run_domain("owned") == ()
    assert enabled(success) == expected

    adopted = _connection()
    adopted.execute(f"PRAGMA foreign_keys={foreign_keys}")
    adopted.execute("CREATE TABLE owned(value TEXT)")
    assert _runner(adopted, [_migration()]).run_domain("owned") == (1,)
    assert enabled(adopted) == expected

    rollback = _connection()
    rollback.execute(f"PRAGMA foreign_keys={foreign_keys}")
    with pytest.raises(SchemaMigrationError):
        _runner(rollback, [_migration(transform=lambda candidate: (_ for _ in ()).throw(ValueError()))]).run_domain(
            "owned"
        )
    assert enabled(rollback) == expected
    assert not _has_table(rollback, LEDGER)

    active = _connection()
    active.execute(f"PRAGMA foreign_keys={foreign_keys}")
    active.execute("CREATE TABLE caller_sentinel(value TEXT)")
    active.commit()
    active.execute("BEGIN")
    active.execute("INSERT INTO caller_sentinel(value) VALUES ('preserved')")
    with pytest.raises(SchemaMigrationError) as raised:
        _runner(active, [_migration()]).run_domain("owned")
    assert raised.value.code == TRANSACTION_ACTIVE
    assert enabled(active) == expected
    active.rollback()


def test_rebuild_schema_fixture_uses_tracked_create_only_history(tmp_path: Path) -> None:
    from tests.support.schema_history import rebuild_schema_fixture

    fixture = (
        ROOT
        / "tests/fixtures/hardening/schema-history/1.24.0-release-fdf71e2/core.schema.sql"
    )
    connection = rebuild_schema_fixture(tmp_path / "history.sqlite", fixture)
    try:
        assert connection.row_factory is sqlite3.Row
        assert _has_table(connection, "sessions")
    finally:
        connection.close()


def test_rebuild_schema_fixture_refuses_non_create_statement(tmp_path: Path) -> None:
    from tests.support.schema_history import rebuild_schema_fixture

    fixture = tmp_path / "invalid.schema.sql"
    fixture.write_text("DROP TABLE no_such_table;", encoding="utf-8")

    with pytest.raises(ValueError):
        rebuild_schema_fixture(tmp_path / "invalid.sqlite", fixture)


def test_rebuild_schema_fixture_accepts_complete_create_statements_without_line_boundaries(
    tmp_path: Path,
) -> None:
    from tests.support.schema_history import rebuild_schema_fixture

    fixture = tmp_path / "formatting.schema.sql"
    fixture.write_text(
        "CREATE TABLE first_shape(value TEXT); CREATE\nTABLE second_shape(value TEXT); "
        "/* formatting */ CREATE\tTABLE third_shape(value TEXT);",
        encoding="utf-8",
    )

    connection = rebuild_schema_fixture(tmp_path / "formatting.sqlite", fixture)
    try:
        assert _has_table(connection, "first_shape")
        assert _has_table(connection, "second_shape")
        assert _has_table(connection, "third_shape")
    finally:
        connection.close()


def test_schema_snapshot_is_metadata_only_and_ledger_rows_are_ordered() -> None:
    from tests.support.schema_history import ledger_rows, schema_snapshot

    connection = _connection()
    connection.execute("CREATE TABLE content_free_shape(id INTEGER PRIMARY KEY, note TEXT)")
    before = schema_snapshot(connection)
    connection.execute("INSERT INTO content_free_shape(note) VALUES ('private row value')")
    assert schema_snapshot(connection) == before
    connection.commit()

    runner = _runner(connection, [_migration()])
    assert runner.run_domain("owned") == (1,)
    assert ledger_rows(connection) == ((1, "owned-table", "owned"),)


def test_schema_snapshot_preserves_visible_sqlite_prefix_and_quoted_literal_spacing() -> None:
    from tests.support.schema_history import schema_snapshot

    connection = _connection()
    connection.execute("CREATE TABLE sqliteXvisible(value TEXT)")
    connection.execute("CREATE TABLE quoted_spacing_one(value TEXT CHECK(value = 'a  b'))")
    connection.execute("CREATE TABLE quoted_spacing_two(value TEXT CHECK(value = 'a b'))")

    objects = {
        row[2]: row[4]
        for row in schema_snapshot(connection)
        if row[0] == "schema"
    }

    assert "sqliteXvisible" in objects
    assert objects["quoted_spacing_one"] != objects["quoted_spacing_two"]
    assert "'a  b'" in objects["quoted_spacing_one"]
    assert "'a b'" in objects["quoted_spacing_two"]


def test_schema_snapshot_preserves_check_token_boundaries() -> None:
    from tests.support.schema_history import schema_snapshot

    connection = _connection()
    connection.execute(
        "CREATE TABLE check_tokens_separate("
        "a INTEGER, b INTEGER, aandb INTEGER, CHECK(a AND b))"
    )
    connection.execute(
        "CREATE TABLE check_tokens_joined("
        "a INTEGER, b INTEGER, aandb INTEGER, CHECK(aandb))"
    )

    objects = {
        row[2]: row[4]
        for row in schema_snapshot(connection)
        if row[0] == "schema"
    }

    assert objects["check_tokens_separate"] != objects["check_tokens_joined"]
    assert "check(a and b)" in objects["check_tokens_separate"]
    assert "check(aandb)" in objects["check_tokens_joined"]


def test_schema_snapshot_preserves_comment_and_minus_operator_semantics() -> None:
    from tests.support.schema_history import schema_snapshot

    connection = _connection()
    connection.execute("CREATE TABLE check_minus_operator(x INTEGER, y INTEGER, CHECK(x - -y))")
    connection.execute("CREATE TABLE check_line_comment(x INTEGER, y INTEGER, CHECK(x--y\n))")
    connection.execute("INSERT INTO check_minus_operator(x, y) VALUES (0, 1)")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO check_line_comment(x, y) VALUES (0, 1)")

    objects = {
        row[2]: row[4]
        for row in schema_snapshot(connection)
        if row[0] == "schema"
    }

    assert objects["check_minus_operator"] != objects["check_line_comment"]
    assert "x - - y" in objects["check_minus_operator"]


def test_schema_snapshot_preserves_hex_numeric_token_boundaries() -> None:
    from tests.support.schema_history import schema_snapshot

    hexadecimal = _connection()
    hexadecimal.execute("CREATE VIEW v(value) AS SELECT 0x10")
    separated = _connection()
    separated.execute("CREATE VIEW v(value) AS SELECT 0 x10")

    assert hexadecimal.execute("SELECT value FROM v").fetchone() == (16,)
    assert separated.execute("SELECT value FROM v").fetchone() == (0,)
    assert schema_snapshot(hexadecimal) != schema_snapshot(separated)


def test_schema_snapshot_preserves_numeric_underscore_token_boundaries() -> None:
    from tests.support.schema_history import schema_snapshot

    underscored = _connection()
    separated = _connection()
    for connection, sql in (
        (underscored, "CREATE VIEW v(value) AS SELECT 1_0"),
        (separated, "CREATE VIEW v(value) AS SELECT 1 _0"),
    ):
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "INSERT INTO main.sqlite_schema(type, name, tbl_name, rootpage, sql) "
            "VALUES ('view', 'v', 'v', 0, ?)",
            (sql,),
        )
        connection.execute("PRAGMA writable_schema=OFF")

    assert schema_snapshot(underscored) != schema_snapshot(separated)


def test_schema_snapshot_preserves_unicode_identifier_bytes_in_checks() -> None:
    from tests.support.schema_history import schema_snapshot

    upper_bound = _connection()
    upper_bound.execute("CREATE TABLE subject(Ä INTEGER, ä INTEGER, CHECK(Ä > 0))")
    lower_bound = _connection()
    lower_bound.execute("CREATE TABLE subject(Ä INTEGER, ä INTEGER, CHECK(ä > 0))")

    upper_bound.execute("INSERT INTO subject(Ä, ä) VALUES (1, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        lower_bound.execute("INSERT INTO subject(Ä, ä) VALUES (1, 0)")
    assert schema_snapshot(upper_bound) != schema_snapshot(lower_bound)


def test_schema_snapshot_preserves_blob_literal_adjacency() -> None:
    from tests.support.schema_history import schema_snapshot

    blob_literal = _connection()
    blob_literal.execute("CREATE VIEW v(value) AS SELECT X'31' FROM (SELECT 7 AS x)")
    spaced_alias = _connection()
    spaced_alias.execute("CREATE VIEW v(value) AS SELECT X '31' FROM (SELECT 7 AS x)")
    commented_alias = _connection()
    commented_alias.execute("CREATE VIEW v(value) AS SELECT X /*gap*/ '31' FROM (SELECT 7 AS x)")

    assert blob_literal.execute("SELECT value, typeof(value) FROM v").fetchone() == (b"1", "blob")
    assert spaced_alias.execute("SELECT value, typeof(value) FROM v").fetchone() == (7, "integer")
    assert commented_alias.execute("SELECT value, typeof(value) FROM v").fetchone() == (7, "integer")
    assert schema_snapshot(blob_literal) != schema_snapshot(spaced_alias)
    assert schema_snapshot(blob_literal) != schema_snapshot(commented_alias)
    assert schema_snapshot(spaced_alias) == schema_snapshot(commented_alias)


def test_schema_history_helpers_read_main_only_and_reject_temp_ledger() -> None:
    from tests.support.schema_history import ledger_rows, schema_snapshot

    connection = _connection()
    connection.execute("CREATE TABLE owned(a INTEGER)")
    connection.execute("CREATE INDEX owned_index ON owned(a)")
    connection.execute("CREATE TEMP TABLE owned(b TEXT)")
    connection.execute("CREATE INDEX owned_index ON owned(b)")
    snapshot = schema_snapshot(connection)
    owned_metadata = [row for row in snapshot if len(row) > 1 and row[1] == "owned"]
    assert any(row[0] == "table_xinfo" and row[3] == "a" for row in owned_metadata)
    assert not any(row[0] == "table_xinfo" and row[3] == "b" for row in owned_metadata)

    _exact_ledger(connection)
    connection.execute(
        f"INSERT INTO main.{LEDGER}(version, name, domain, applied_at) VALUES (1, 'owned-table', 'owned', 100.0)"
    )
    _temp_exact_ledger(connection)
    connection.execute(
        f"INSERT INTO temp.{LEDGER}(version, name, domain, applied_at) VALUES (2, 'temp-owned', 'owned', 100.0)"
    )

    with pytest.raises(ValueError) as raised:
        ledger_rows(connection)
    assert str(raised.value) == "schema_history_ledger_namespace_invalid"


@pytest.mark.parametrize("clock_value", [True, 0, -1, float("inf"), float("nan")])
def test_invalid_clock_rolls_back_schema_and_ledger(clock_value: object) -> None:
    connection = _connection()
    runner = _runner(connection, [_migration()], clock=lambda: clock_value)  # type: ignore[arg-type]

    with pytest.raises(SchemaMigrationError) as raised:
        runner.run_domain("owned")

    assert raised.value.code == FAILURE
    assert not _has_table(connection, "owned")
    assert not _has_table(connection, LEDGER)
    assert not connection.in_transaction
