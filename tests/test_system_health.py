from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from dataclasses import FrozenInstanceError
from typing import Any, Iterator

import pytest

from aetherstate.schema_migrations import SchemaMigrationError, SchemaMigrationRunner


TABLE = "aetherstate_system_health"
DOMAIN = "system-health"
FAILURE_CODE = "system_health_schema_invalid"
FORBIDDEN_CONTENT = (
    "prompt-secret",
    "response-secret",
    "credential-secret",
    "C:\\Users\\Bean\\private",
    "local-secret",
)


def _api() -> Any:
    try:
        return importlib.import_module("aetherstate.system_health")
    except ModuleNotFoundError:
        pytest.fail("SystemHealth core is not implemented", pytrace=False)


def _sequence(values: list[float]) -> Any:
    iterator: Iterator[float] = iter(values)
    return lambda: next(iterator)


class _CapturingLogger:
    def __init__(self, *, explode: bool = False) -> None:
        self.messages: list[str] = []
        self.explode = explode

    def warning(self, message: str, *args: object) -> None:
        rendered = message % args if args else message
        self.messages.append(rendered)
        if self.explode:
            raise RuntimeError("logger-local-secret")


def _service(
    *,
    clock: Any | None = None,
    correlation_factory: Any | None = None,
    logger: Any | None = None,
    recorder_log_interval_s: float = 60.0,
) -> tuple[sqlite3.Connection, Any]:
    api = _api()
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    lock = threading.RLock()
    runner = SchemaMigrationRunner(
        connection,
        lock,
        api.system_health_schema_migrations(),
        clock=lambda: 1.0,
    )
    kwargs: dict[str, object] = {
        "recorder_log_interval_s": recorder_log_interval_s,
    }
    if clock is not None:
        kwargs["clock"] = clock
    if correlation_factory is not None:
        kwargs["correlation_factory"] = correlation_factory
    if logger is not None:
        kwargs["logger"] = logger
    service = api.SystemHealth(connection, lock, runner, **kwargs)
    return connection, service


def _durable_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            f"SELECT subsystem, error_code, classification, severity, correlation_id, "
            f"occurrence_count, first_seen_at, last_seen_at, active, recovered_at "
            f"FROM {TABLE} ORDER BY subsystem, error_code, classification"
        )
    )


def test_migration_v7_creates_exact_bounded_schema_and_rejects_drift() -> None:
    api = _api()
    migration = api.system_health_schema_migrations()

    assert len(migration) == 1
    assert (
        migration[0].version,
        migration[0].name,
        migration[0].domain,
        migration[0].failure_code,
    ) == (7, "system-health-1.24-baseline", DOMAIN, FAILURE_CODE)

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    runner = SchemaMigrationRunner(
        connection, threading.RLock(), migration, clock=lambda: 1.0
    )
    assert runner.run_domain(DOMAIN) == (7,)
    assert runner.run_domain(DOMAIN) == ()

    columns = tuple(
        (row[1], row[2], row[3], row[5])
        for row in connection.execute(f"PRAGMA main.table_xinfo({TABLE})")
    )
    assert columns == (
        ("subsystem", "TEXT", 1, 1),
        ("error_code", "TEXT", 1, 2),
        ("classification", "TEXT", 1, 3),
        ("severity", "TEXT", 1, 0),
        ("correlation_id", "TEXT", 1, 0),
        ("occurrence_count", "INTEGER", 1, 0),
        ("first_seen_at", "REAL", 1, 0),
        ("last_seen_at", "REAL", 1, 0),
        ("active", "INTEGER", 1, 0),
        ("recovered_at", "REAL", 0, 0),
    )
    assert tuple(connection.execute(f"PRAGMA main.index_list({TABLE})")) == (
        (0, f"sqlite_autoindex_{TABLE}_1", 1, "pk", 0),
    )
    schema_sql = connection.execute(
        "SELECT sql FROM main.sqlite_schema WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()[0]
    assert schema_sql
    assert not any(token in schema_sql.lower() for token in FORBIDDEN_CONTENT)

    drifted = sqlite3.connect(":memory:", check_same_thread=False)
    drifted.execute(f"CREATE TABLE {TABLE}(subsystem TEXT PRIMARY KEY, payload TEXT)")
    drifted_runner = SchemaMigrationRunner(
        drifted, threading.RLock(), migration, clock=lambda: 1.0
    )
    with pytest.raises(SchemaMigrationError) as raised:
        drifted_runner.run_domain(DOMAIN)
    assert raised.value.code == FAILURE_CODE
    assert not drifted.in_transaction


def test_catalog_and_event_types_are_immutable_and_bounded() -> None:
    api = _api()

    assert tuple(
        (
            item.subsystem,
            item.error_code,
            item.classification,
            item.severity,
            item.recovery_proof,
        )
        for item in api.SYSTEM_HEALTH_CATALOG
    ) == (
        (
            "startup",
            "pending_extraction_resume_failed",
            "expected_recoverable",
            "warning",
            "pending_extraction_resume_succeeded",
        ),
        (
            "status",
            "prompt_cache_snapshot_failed",
            "expected_recoverable",
            "warning",
            "prompt_cache_snapshot_succeeded",
        ),
        (
            "status",
            "extraction_snapshot_failed",
            "expected_recoverable",
            "warning",
            "extraction_snapshot_succeeded",
        ),
        (
            "status",
            "status_summary_invariant_failed",
            "unexpected_invariant",
            "error",
            "status_summary_succeeded",
        ),
    )
    with pytest.raises(FrozenInstanceError):
        api.SYSTEM_HEALTH_CATALOG[0].severity = "error"

    event = api.HealthEvent(
        subsystem="startup",
        error_code="pending_extraction_resume_failed",
        classification="expected_recoverable",
        severity="warning",
        correlation_id="corr-0001",
        occurrence_count=1,
        first_seen_at=1.0,
        last_seen_at=1.0,
        active=True,
        recovered_at=None,
    )
    with pytest.raises(FrozenInstanceError):
        event.active = False


def test_failure_recovery_and_reoccurrence_use_one_aggregate_row() -> None:
    connection, service = _service(
        clock=_sequence([10.0, 20.0, 30.0, 40.0]),
        correlation_factory=iter(("corr-0001", "corr-0002", "corr-0003")).__next__,
    )

    assert service.record_failure(
        "startup", "pending_extraction_resume_failed"
    ) == "corr-0001"
    assert service.record_failure(
        "startup", "pending_extraction_resume_failed"
    ) == "corr-0002"
    service.record_success("pending_extraction_resume_succeeded")
    assert service.record_failure(
        "startup", "pending_extraction_resume_failed"
    ) == "corr-0003"

    assert _durable_rows(connection) == (
        (
            "startup",
            "pending_extraction_resume_failed",
            "expected_recoverable",
            "warning",
            "corr-0003",
            3,
            10.0,
            40.0,
            1,
            None,
        ),
    )
    condition = service.snapshot()["conditions"][0]
    assert condition == {
        "subsystem": "startup",
        "error_code": "pending_extraction_resume_failed",
        "classification": "expected_recoverable",
        "severity": "warning",
        "correlation_id": "corr-0003",
        "occurrence_count": 3,
        "first_seen_at": 10.0,
        "last_seen_at": 40.0,
        "active": True,
        "recovered_at": None,
    }


def test_recovery_proof_closes_only_its_declared_conditions() -> None:
    connection, service = _service(
        clock=_sequence([1.0, 2.0, 3.0]),
        correlation_factory=iter(("corr-1001", "corr-1002")).__next__,
    )
    service.record_failure("status", "prompt_cache_snapshot_failed")
    service.record_failure("status", "extraction_snapshot_failed")

    service.record_success("prompt_cache_snapshot_succeeded")

    rows = _durable_rows(connection)
    assert tuple((row[1], row[8], row[9]) for row in rows) == (
        ("extraction_snapshot_failed", 1, None),
        ("prompt_cache_snapshot_failed", 0, 3.0),
    )
    snapshot = service.snapshot()
    assert snapshot["state"] == "degraded"
    assert snapshot["active_condition_count"] == 1
    assert snapshot["total_condition_count"] == 2


def test_unknown_conditions_and_proofs_never_grow_catalog_or_storage() -> None:
    connection, service = _service()
    before_catalog = tuple(_api().SYSTEM_HEALTH_CATALOG)

    with pytest.raises(ValueError, match="unknown_system_health_condition"):
        service.record_failure("status", "dynamic-secret-condition")
    with pytest.raises(ValueError, match="unknown_system_health_recovery_proof"):
        service.record_success("dynamic-secret-proof")

    assert tuple(_api().SYSTEM_HEALTH_CATALOG) == before_catalog
    assert _durable_rows(connection) == ()


def test_state_precedence_and_sorted_projection_are_deterministic() -> None:
    _, service = _service(
        clock=_sequence([5.0, 6.0]),
        correlation_factory=iter(("corr-2001", "corr-2002")).__next__,
    )
    assert service.snapshot()["state"] == "none"

    assert (
        service.record_failure("status", "prompt_cache_snapshot_failed")
        == "corr-2001"
    )
    assert service.snapshot()["state"] == "degraded"

    assert (
        service.record_failure("status", "status_summary_invariant_failed")
        == "corr-2002"
    )
    snapshot = service.snapshot()
    assert snapshot["state"] == "failed"
    assert [
        (item["subsystem"], item["error_code"], item["classification"])
        for item in snapshot["conditions"]
    ] == sorted(
        (
            item["subsystem"],
            item["error_code"],
            item["classification"],
        )
        for item in snapshot["conditions"]
    )


def test_recovery_time_never_precedes_the_condition_last_seen_time() -> None:
    connection, service = _service(
        clock=_sequence([20.0, 10.0]),
        correlation_factory=lambda: "corr-2501",
    )
    service.record_failure("status", "prompt_cache_snapshot_failed")

    service.record_success("prompt_cache_snapshot_succeeded")

    row = _durable_rows(connection)[0]
    assert row[7:] == (20.0, 0, 20.0)
    assert service.snapshot()["durable_available"] is True


def test_invalid_correlation_is_replaced_and_exception_content_is_never_retained() -> None:
    logger = _CapturingLogger()
    connection, service = _service(
        clock=lambda: 10.0,
        correlation_factory=lambda: "corr-safe-0001",
        logger=logger,
    )
    secret = " ".join(FORBIDDEN_CONTENT)

    correlation = service.record_failure(
        "status",
        "prompt_cache_snapshot_failed",
        exception=RuntimeError(secret),
        correlation_id="C:\\Users\\Bean\\private\\prompt-secret",
    )

    assert correlation == "corr-safe-0001"
    stored = json.dumps(_durable_rows(connection), sort_keys=True)
    snapshot = json.dumps(service.snapshot(), sort_keys=True)
    diagnostic = json.dumps(service.diagnostic_export(), sort_keys=True)
    logs = "\n".join(logger.messages)
    for forbidden in FORBIDDEN_CONTENT:
        assert forbidden not in stored
        assert forbidden not in snapshot
        assert forbidden not in diagnostic
        assert forbidden not in logs
    assert service.snapshot()["schema"] == "aetherstate-system-health/1"
    assert (
        service.diagnostic_export()["schema"]
        == "aetherstate-system-health-diagnostic/1"
    )


def test_closed_durable_store_preserves_memory_and_all_public_operations() -> None:
    logger = _CapturingLogger()
    connection, service = _service(
        clock=_sequence([10.0, 20.0, 30.0, 40.0]),
        correlation_factory=iter(
            ("corr-3001", "corr-3002", "corr-3003", "corr-3004")
        ).__next__,
        logger=logger,
    )
    connection.close()

    assert (
        service.record_failure("status", "extraction_snapshot_failed")
        == "corr-3001"
    )
    snapshot = service.snapshot()
    service.record_success("extraction_snapshot_succeeded")
    diagnostic = service.diagnostic_export()

    assert snapshot["durable_available"] is False
    assert snapshot["active_condition_count"] == 1
    assert diagnostic["durable_available"] is False
    assert diagnostic["active_condition_count"] == 0
    assert diagnostic["conditions"][0]["recovered_at"] == 30.0
    assert logger.messages


def test_recorder_warning_is_rate_limited_content_free_and_nonrecursive() -> None:
    logger = _CapturingLogger()
    connection, service = _service(
        clock=_sequence([100.0, 110.0, 161.0]),
        correlation_factory=iter(("corr-4001", "corr-4002", "corr-4003")).__next__,
        logger=logger,
        recorder_log_interval_s=60.0,
    )
    connection.close()

    service.record_failure(
        "status",
        "prompt_cache_snapshot_failed",
        exception=RuntimeError("prompt-secret C:\\Users\\Bean\\private"),
    )
    service.record_failure("status", "prompt_cache_snapshot_failed")
    service.record_failure("status", "prompt_cache_snapshot_failed")

    assert len(logger.messages) == 2
    for message in logger.messages:
        assert message.startswith("health_recorder_unavailable ")
        assert "ProgrammingError" in message
        assert "prompt-secret" not in message
        assert "C:\\Users\\Bean\\private" not in message

    exploding_logger = _CapturingLogger(explode=True)
    other_connection, other_service = _service(
        clock=lambda: 200.0,
        correlation_factory=lambda: "corr-5001",
        logger=exploding_logger,
    )
    other_connection.close()
    assert (
        other_service.record_failure("status", "extraction_snapshot_failed")
        == "corr-5001"
    )
    assert len(exploding_logger.messages) == 1
