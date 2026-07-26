"""Synthetic historical-database upgrade meaning preservation."""
from __future__ import annotations

from dataclasses import replace
import sqlite3
import threading
from pathlib import Path

import pytest

from aetherstate.database_schema import database_schema_migrations
from aetherstate.schema_migrations import SchemaMigrationError, SchemaMigrationRunner
from aetherstate.store import Store
from tests.support.schema_history import (
    ledger_rows,
    read_historical_meaning,
    rebuild_schema_fixture,
    schema_snapshot,
    seed_supported_history,
)


FIXTURES = Path(__file__).parent / "fixtures" / "hardening" / "schema-history"
BASELINES = (
    "1.0.0-release-2cd07ef",
    "1.1.0-release-ed63e38",
    "1.22.0-release-9091614",
    "1.23.0-release-1f4aad0",
    "1.23.0-final-34dfe8f",
    "1.24.0-release-fdf71e2",
)
PATHS = tuple((baseline, shape) for baseline in BASELINES for shape in ("core", "full-start"))


def _fixture(baseline_id: str, shape: str) -> Path:
    return FIXTURES / baseline_id / f"{shape}.schema.sql"


def _rows(connection: sqlite3.Connection) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    tables = tuple(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND substr(name, 1, 7) <> 'sqlite_' ORDER BY name"
    ))
    return tuple((table, tuple(connection.execute(f'SELECT * FROM "{table}"'))) for table in tables)


@pytest.mark.parametrize(("baseline_id", "shape"), PATHS, ids=[f"{a}-{b}" for a, b in PATHS])
def test_supported_historical_database_upgrade_preserves_meaning(
    tmp_path: Path, baseline_id: str, shape: str,
) -> None:
    path = tmp_path / f"{baseline_id}-{shape}.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture(baseline_id, shape))
    expected = seed_supported_history(connection, baseline_id, shape)
    connection.close()

    store = Store(path)
    try:
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert ledger_rows(store.db) == (
            (1, "store-core-1.24-baseline", "store-core"),
            (2, "worldlex-1.24-baseline", "worldlex"),
            (3, "turn-lifecycle-1.24-baseline", "turn-lifecycle"),
            (4, "store-chat-lineage-1.24-baseline", "store-core"),
            (7, "system-health-1.24-baseline", "system-health"),
        )
        assert read_historical_meaning(store, expected) == expected
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize(("baseline_id", "shape"), PATHS, ids=[f"{a}-{b}" for a, b in PATHS])
def test_supported_database_second_startup_is_zero_mutation(
    tmp_path: Path, baseline_id: str, shape: str,
) -> None:
    path = tmp_path / f"second-{baseline_id}-{shape}.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture(baseline_id, shape))
    expected = seed_supported_history(connection, baseline_id, shape)
    connection.close()

    first = Store(path)
    try:
        read_historical_meaning(first, expected)
        schema_before = schema_snapshot(first.db)
        rows_before = _rows(first.db)
        ledger_before = ledger_rows(first.db)
    finally:
        first.close()
    second = Store(path)
    try:
        assert schema_snapshot(second.db) == schema_before
        assert _rows(second.db) == rows_before
        assert ledger_rows(second.db) == ledger_before
        assert read_historical_meaning(second, expected) == expected
    finally:
        second.close()


def test_interrupted_supported_upgrade_leaves_old_or_fully_current_state(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture(BASELINES[0], "core"))
    seed_supported_history(connection, BASELINES[0], "core")
    runner = SchemaMigrationRunner(connection, threading.RLock(), database_schema_migrations())
    assert runner.run_domain("store-core") == (1, 4)
    assert runner.run_domain("worldlex") == (2,)
    before_schema = schema_snapshot(connection)
    before_ledger = ledger_rows(connection)
    migration = next(item for item in database_schema_migrations() if item.version == 3)

    def interrupted(candidate: sqlite3.Connection) -> None:
        migration.transform(candidate)
        raise RuntimeError("interrupt after lifecycle transform")

    failing = replace(migration, transform=interrupted)
    failing_runner = SchemaMigrationRunner(
        connection, threading.RLock(),
        tuple(failing if item.version == 3 else item for item in database_schema_migrations()),
    )
    with pytest.raises(SchemaMigrationError, match="turn_lifecycle_schema_unsupported"):
        failing_runner.run_domain("turn-lifecycle")
    assert schema_snapshot(connection) == before_schema
    assert ledger_rows(connection) == before_ledger
    assert runner.run_domain("turn-lifecycle") == (3,)
    connection.close()


def test_current_124_database_adoption_preserves_rows_and_replay(tmp_path: Path) -> None:
    path = tmp_path / "current-124.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture("1.24.0-release-fdf71e2", "full-start"))
    expected = seed_supported_history(connection, "1.24.0-release-fdf71e2", "full-start")
    connection.close()
    store = Store(path)
    try:
        assert read_historical_meaning(store, expected) == expected
        assert [version for version, _name, _domain in ledger_rows(store.db)] == [1, 2, 3, 4, 7]
    finally:
        store.close()


def test_full_start_historical_upgrade_records_all_domains_and_preserves_meaning(tmp_path: Path) -> None:
    """Opening a synthetic full-start fixture claims each deferred domain once."""
    path = tmp_path / "full-start-all-domains.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture("1.24.0-release-fdf71e2", "full-start"))
    expected = seed_supported_history(connection, "1.24.0-release-fdf71e2", "full-start")
    connection.close()
    store = Store(path)
    try:
        from aetherstate.app import create_app
        from aetherstate.config import Config

        app = create_app(Config(), store=store)
        assert app is not None
        assert [version for version, _name, _domain in ledger_rows(store.db)] == [1, 2, 3, 4, 5, 6, 7]
        assert read_historical_meaning(store, expected) == expected
    finally:
        store.close()


def test_historical_readback_rejects_a_deleted_carried_effect_receipt(
    tmp_path: Path,
) -> None:
    """The exact synthetic receipt must be checked rather than echoed from expected."""
    path = tmp_path / "deleted-carried-effect.sqlite3"
    connection = rebuild_schema_fixture(
        path, _fixture("1.24.0-release-fdf71e2", "full-start")
    )
    expected = seed_supported_history(
        connection, "1.24.0-release-fdf71e2", "full-start"
    )
    connection.close()
    store = Store(path)
    try:
        store.db.execute("DELETE FROM effect_receipts WHERE effect_id='history-effect'")
        store.db.commit()
        with pytest.raises(AssertionError, match="effect_receipts"):
            read_historical_meaning(store, expected)
    finally:
        store.close()


@pytest.mark.parametrize("foreign_keys", (0, 1), ids=("off", "on"))
def test_supported_upgrade_preserves_pre_migration_foreign_key_setting(
    tmp_path: Path, foreign_keys: int,
) -> None:
    """Migration ownership must not alter either caller-selected pragma state."""
    path = tmp_path / f"foreign-keys-{foreign_keys}.sqlite3"
    connection = rebuild_schema_fixture(path, _fixture("1.24.0-release-fdf71e2", "full-start"))
    connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
    seed_supported_history(connection, "1.24.0-release-fdf71e2", "full-start")
    runner = SchemaMigrationRunner(
        connection, threading.RLock(), database_schema_migrations()
    )
    assert runner.run_domain("store-core") == (1, 4)
    assert runner.run_domain("worldlex") == (2,)
    assert runner.run_domain("turn-lifecycle") == (3,)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
    connection.close()


@pytest.mark.parametrize("mutation", ("delete", "alter"))
def test_historical_readback_rejects_changed_carried_accepted_message_receipt(
    tmp_path: Path, mutation: str,
) -> None:
    """Every carried accepted-message receipt is part of the literal history oracle."""
    path = tmp_path / f"historical-accepted-message-receipt-{mutation}.sqlite3"
    connection = rebuild_schema_fixture(
        path, _fixture("1.24.0-release-fdf71e2", "full-start")
    )
    expected = seed_supported_history(
        connection, "1.24.0-release-fdf71e2", "full-start"
    )
    assert any(
        record.table == "chat_accepted_message_receipts"
        for record in expected.records
    )
    connection.close()
    store = Store(path)
    try:
        if mutation == "delete":
            store.db.execute("DELETE FROM chat_accepted_message_receipts")
        else:
            store.db.execute(
                "UPDATE chat_accepted_message_receipts SET receipt_fingerprint=?",
                ("history-accepted-receipt-altered",),
            )
        with pytest.raises(
            AssertionError, match="chat_accepted_message_receipts literal history changed"
        ):
            read_historical_meaning(store, expected)
    finally:
        store.close()


def test_historical_readback_rejects_altered_carried_lifecycle_metadata(
    tmp_path: Path,
) -> None:
    """A carried lifecycle's non-envelope metadata is literal upgrade meaning."""
    path = tmp_path / "historical-lifecycle-metadata.sqlite3"
    connection = rebuild_schema_fixture(
        path, _fixture("1.24.0-release-fdf71e2", "full-start")
    )
    expected = seed_supported_history(
        connection, "1.24.0-release-fdf71e2", "full-start"
    )
    connection.close()
    store = Store(path)
    try:
        selector = expected.selector
        assert selector is not None
        store.db.execute(
            "UPDATE semantic_turn_lifecycles SET updated_at=? WHERE lifecycle_key=?",
            (999.0, selector.lifecycle_key),
        )
        with pytest.raises(
            AssertionError, match="semantic_turn_lifecycles literal history changed"
        ):
            read_historical_meaning(store, expected)
    finally:
        store.close()


def test_historical_upgrade_completes_valid_fork_and_rollback_then_reopens(
    tmp_path: Path,
) -> None:
    """A carried terminal lifecycle supports an exact child fork and rollback."""
    path = tmp_path / "historical-selectors.sqlite3"
    connection = rebuild_schema_fixture(
        path, _fixture("1.24.0-release-fdf71e2", "full-start")
    )
    expected = seed_supported_history(
        connection, "1.24.0-release-fdf71e2", "full-start"
    )
    connection.close()
    store = Store(path)
    try:
        selector = expected.selector
        assert selector is not None
        parent_replay = store.turn_lifecycle.replay(selector.lifecycle_key)
        assert parent_replay.payload == selector.payload
        child = store.fork_branch(selector.branch_id, at_pos=2, fork_turn=0)
        child_replay = store.db.execute(
            "SELECT lifecycle_key FROM semantic_turn_lifecycles WHERE branch_id=?",
            (child,),
        ).fetchone()
        assert child_replay is not None
        assert store.turn_lifecycle.replay(child_replay[0]).payload == selector.payload
        store.rollback_to(child, -1)
        assert store.db.execute(
            "SELECT 1 FROM semantic_turn_lifecycles WHERE branch_id=?", (child,)
        ).fetchone() is None
        assert store.turn_lifecycle.replay(selector.lifecycle_key).payload == selector.payload
        assert read_historical_meaning(store, expected) == expected
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert read_historical_meaning(reopened, expected) == expected
        assert reopened.turn_lifecycle.replay(selector.lifecycle_key).payload == selector.payload
    finally:
        reopened.close()
