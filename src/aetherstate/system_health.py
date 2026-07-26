"""Bounded, privacy-safe System Health aggregation and persistence."""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Iterable

from .schema_migrations import (
    SchemaMigration,
    SchemaMigrationRunner,
    _normalize_sql_outside_quotes,
    _sql_references_identifier,
    sqlite_ascii_fold,
)


SYSTEM_HEALTH_DOMAIN = "system-health"
SYSTEM_HEALTH_SCHEMA_VERSION = 7
SYSTEM_HEALTH_SCHEMA_FAILURE = "system_health_schema_invalid"
_TABLE = "aetherstate_system_health"
_AUTOINDEX = f"sqlite_autoindex_{_TABLE}_1"

_SCHEMA_SQL = """CREATE TABLE aetherstate_system_health(
  subsystem TEXT NOT NULL CHECK(subsystem <> ''),
  error_code TEXT NOT NULL CHECK(error_code <> ''),
  classification TEXT NOT NULL CHECK(
    classification IN ('expected_recoverable','unexpected_invariant')
  ),
  severity TEXT NOT NULL CHECK(severity IN ('warning','error')),
  correlation_id TEXT NOT NULL CHECK(correlation_id <> ''),
  occurrence_count INTEGER NOT NULL CHECK(
    typeof(occurrence_count)='integer' AND occurrence_count > 0
  ),
  first_seen_at REAL NOT NULL CHECK(
    typeof(first_seen_at) IN ('integer','real') AND first_seen_at > 0
  ),
  last_seen_at REAL NOT NULL CHECK(
    typeof(last_seen_at) IN ('integer','real') AND last_seen_at >= first_seen_at
  ),
  active INTEGER NOT NULL CHECK(
    typeof(active)='integer' AND active IN (0,1)
  ),
  recovered_at REAL CHECK(
    recovered_at IS NULL OR (
      typeof(recovered_at) IN ('integer','real') AND recovered_at >= last_seen_at
    )
  ),
  PRIMARY KEY(subsystem, error_code, classification)
)"""
_CREATE_SCHEMA_SQL = _SCHEMA_SQL.replace("CREATE TABLE ", "CREATE TABLE main.", 1)
_NORMALIZED_SCHEMA_SQL = _normalize_sql_outside_quotes(_SCHEMA_SQL)

_CORRELATION_ID = re.compile(
    r"(?:[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"corr-[A-Za-z0-9_-]{4,58})\Z"
)
_DEFAULT_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthConditionDefinition:
    subsystem: str
    error_code: str
    classification: str
    severity: str
    recovery_proof: str


@dataclass(frozen=True)
class HealthEvent:
    subsystem: str
    error_code: str
    classification: str
    severity: str
    correlation_id: str
    occurrence_count: int
    first_seen_at: float
    last_seen_at: float
    active: bool
    recovered_at: float | None


SYSTEM_HEALTH_CATALOG = (
    HealthConditionDefinition(
        subsystem="startup",
        error_code="pending_extraction_resume_failed",
        classification="expected_recoverable",
        severity="warning",
        recovery_proof="pending_extraction_resume_succeeded",
    ),
    HealthConditionDefinition(
        subsystem="status",
        error_code="prompt_cache_snapshot_failed",
        classification="expected_recoverable",
        severity="warning",
        recovery_proof="prompt_cache_snapshot_succeeded",
    ),
    HealthConditionDefinition(
        subsystem="status",
        error_code="extraction_snapshot_failed",
        classification="expected_recoverable",
        severity="warning",
        recovery_proof="extraction_snapshot_succeeded",
    ),
    HealthConditionDefinition(
        subsystem="status",
        error_code="status_summary_invariant_failed",
        classification="unexpected_invariant",
        severity="error",
        recovery_proof="status_summary_succeeded",
    ),
)

_DEFINITIONS = {
    (definition.subsystem, definition.error_code): definition
    for definition in SYSTEM_HEALTH_CATALOG
}
_RECOVERY_CONDITIONS = {
    proof: tuple(
        definition
        for definition in SYSTEM_HEALTH_CATALOG
        if definition.recovery_proof == proof
    )
    for proof in {definition.recovery_proof for definition in SYSTEM_HEALTH_CATALOG}
}


def _owned_object(row: tuple[object, ...]) -> bool:
    marker = sqlite_ascii_fold(_TABLE)
    return (
        sqlite_ascii_fold(row[1]) == marker
        or sqlite_ascii_fold(row[2]) == marker
        or _sql_references_identifier(row[3], _TABLE)
    )


def _schema_rows(
    connection: sqlite3.Connection, schema: str
) -> tuple[tuple[object, ...], ...]:
    table = "sqlite_temp_schema" if schema == "temp" else "main.sqlite_schema"
    return tuple(
        row
        for row in (
            tuple(candidate)
            for candidate in connection.execute(
                f"SELECT type, name, tbl_name, sql FROM {table} ORDER BY type, name"
            )
        )
        if _owned_object(row)
    )


def _schema_current(connection: sqlite3.Connection) -> bool:
    if _schema_rows(connection, "temp"):
        return False
    rows = _schema_rows(connection, "main")
    expected = {
        ("table", _TABLE, _TABLE),
        ("index", _AUTOINDEX, _TABLE),
    }
    if {(row[0], row[1], row[2]) for row in rows} != expected:
        return False
    table_sql = next(row[3] for row in rows if row[0] == "table")
    if (
        not isinstance(table_sql, str)
        or _normalize_sql_outside_quotes(table_sql) != _NORMALIZED_SCHEMA_SQL
    ):
        return False
    if next(row[3] for row in rows if row[0] == "index") is not None:
        return False
    columns = tuple(
        (row[1], row[2], row[3], row[5])
        for row in connection.execute(f"PRAGMA main.table_xinfo({_TABLE})")
    )
    if columns != (
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
    ):
        return False
    if tuple(connection.execute(f"PRAGMA main.index_list({_TABLE})")) != (
        (0, _AUTOINDEX, 1, "pk", 0),
    ):
        return False
    return tuple(connection.execute(f"PRAGMA main.index_xinfo({_AUTOINDEX})")) == (
        (0, 0, "subsystem", 0, "BINARY", 1),
        (1, 1, "error_code", 0, "BINARY", 1),
        (2, 2, "classification", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    )


def _schema_applicable(connection: sqlite3.Connection) -> bool:
    return not _schema_rows(connection, "main") and not _schema_rows(connection, "temp")


def _transform_schema(connection: sqlite3.Connection) -> None:
    if not _schema_applicable(connection):
        raise ValueError(SYSTEM_HEALTH_SCHEMA_FAILURE)
    connection.execute(_CREATE_SCHEMA_SQL)


def system_health_schema_migrations() -> tuple[SchemaMigration, ...]:
    return (
        SchemaMigration(
            version=SYSTEM_HEALTH_SCHEMA_VERSION,
            name="system-health-1.24-baseline",
            domain=SYSTEM_HEALTH_DOMAIN,
            failure_code=SYSTEM_HEALTH_SCHEMA_FAILURE,
            applies=_schema_applicable,
            is_current=_schema_current,
            transform=_transform_schema,
            postcondition=_schema_current,
        ),
    )


class SystemHealth:
    """Aggregate declared health conditions without retaining failure payloads."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: Any,
        migration_runner: SchemaMigrationRunner,
        *,
        clock: Callable[[], float] = time.time,
        correlation_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        logger: Any = _DEFAULT_LOGGER,
        recorder_log_interval_s: float = 60.0,
    ) -> None:
        if (
            not isinstance(recorder_log_interval_s, (int, float))
            or isinstance(recorder_log_interval_s, bool)
            or not math.isfinite(recorder_log_interval_s)
            or recorder_log_interval_s < 0
        ):
            raise ValueError("invalid_system_health_log_interval")
        self._connection = connection
        self._lock = lock
        self._migration_runner = migration_runner
        self._clock = clock
        self._correlation_factory = correlation_factory
        self._logger = logger
        self._recorder_log_interval_s = float(recorder_log_interval_s)
        self._operation_lock = threading.RLock()
        self._events: dict[tuple[str, str, str], HealthEvent] = {}
        self._dirty: set[tuple[str, str, str]] = set()
        self._migration_ready = False
        self._durable_available = False
        self._last_recorder_log_at: float | None = None
        self._initialize_durable_state()

    def record_failure(
        self,
        subsystem: str,
        error_code: str,
        *,
        exception: BaseException | None = None,
        correlation_id: str | None = None,
    ) -> str:
        del exception
        definition = _DEFINITIONS.get((subsystem, error_code))
        if definition is None:
            raise ValueError("unknown_system_health_condition")
        with self._operation_lock:
            now = self._now()
            correlation = self._safe_correlation(correlation_id)
            key = self._event_key(definition)
            previous = self._events.get(key)
            if previous is None:
                event = HealthEvent(
                    subsystem=definition.subsystem,
                    error_code=definition.error_code,
                    classification=definition.classification,
                    severity=definition.severity,
                    correlation_id=correlation,
                    occurrence_count=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    active=True,
                    recovered_at=None,
                )
            else:
                event = replace(
                    previous,
                    severity=definition.severity,
                    correlation_id=correlation,
                    occurrence_count=previous.occurrence_count + 1,
                    last_seen_at=max(now, previous.last_seen_at),
                    active=True,
                    recovered_at=None,
                )
            self._events[key] = event
            self._persist_or_fallback((event,), correlation, now)
            return correlation

    def record_success(self, recovery_proof: str) -> None:
        definitions = _RECOVERY_CONDITIONS.get(recovery_proof)
        if definitions is None:
            raise ValueError("unknown_system_health_recovery_proof")
        with self._operation_lock:
            now = self._now()
            recovered: list[HealthEvent] = []
            for definition in definitions:
                key = self._event_key(definition)
                previous = self._events.get(key)
                if previous is None or not previous.active:
                    continue
                event = replace(
                    previous,
                    active=False,
                    recovered_at=max(now, previous.last_seen_at),
                )
                self._events[key] = event
                recovered.append(event)
            if not recovered:
                return
            self._persist_or_fallback(tuple(recovered), None, now)

    def snapshot(self) -> dict[str, object]:
        with self._operation_lock:
            self._refresh_from_durable()
            return self._projection("aetherstate-system-health/1")

    def diagnostic_export(self) -> dict[str, object]:
        with self._operation_lock:
            self._refresh_from_durable()
            return self._projection("aetherstate-system-health-diagnostic/1")

    def _initialize_durable_state(self) -> None:
        try:
            self._migration_runner.run_domain(SYSTEM_HEALTH_DOMAIN)
            with self._lock:
                durable = self._read_durable()
        except Exception as error:
            self._durable_available = False
            self._report_recorder_unavailable(
                error, self._safe_correlation(None), self._now()
            )
            return
        self._merge_durable(durable)
        self._migration_ready = True
        self._durable_available = True

    def _persist_or_fallback(
        self, events: tuple[HealthEvent, ...], correlation: str | None, now: float
    ) -> None:
        keys = {self._event_key(event) for event in events}
        if not self._migration_ready:
            self._dirty.update(keys)
            self._durable_available = False
            return
        try:
            self._persist(events)
        except Exception as error:
            self._dirty.update(keys)
            self._durable_available = False
            self._report_recorder_unavailable(
                error, self._safe_correlation(correlation), now
            )
            return
        self._dirty.difference_update(keys)
        self._durable_available = not self._dirty

    def _persist(self, events: Iterable[HealthEvent]) -> None:
        with self._lock:
            if self._connection.in_transaction:
                raise sqlite3.OperationalError("system health transaction unavailable")
            started = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                started = True
                for event in events:
                    self._connection.execute(
                        f"""INSERT INTO main.{_TABLE}(
                          subsystem, error_code, classification, severity,
                          correlation_id, occurrence_count, first_seen_at,
                          last_seen_at, active, recovered_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(subsystem, error_code, classification) DO UPDATE SET
                          severity=excluded.severity,
                          correlation_id=excluded.correlation_id,
                          occurrence_count=excluded.occurrence_count,
                          first_seen_at=excluded.first_seen_at,
                          last_seen_at=excluded.last_seen_at,
                          active=excluded.active,
                          recovered_at=excluded.recovered_at""",
                        (
                            event.subsystem,
                            event.error_code,
                            event.classification,
                            event.severity,
                            event.correlation_id,
                            event.occurrence_count,
                            event.first_seen_at,
                            event.last_seen_at,
                            int(event.active),
                            event.recovered_at,
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                if started and self._connection.in_transaction:
                    try:
                        self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

    def _refresh_from_durable(self) -> None:
        if not self._migration_ready:
            self._durable_available = False
            return
        try:
            with self._lock:
                durable = self._read_durable()
        except Exception as error:
            self._durable_available = False
            self._report_recorder_unavailable(
                error, self._safe_correlation(None), self._now()
            )
            return
        self._merge_durable(durable)
        self._durable_available = not self._dirty

    def _read_durable(self) -> tuple[HealthEvent, ...]:
        rows = tuple(
            tuple(row)
            for row in self._connection.execute(
                f"""SELECT subsystem, error_code, classification, severity,
                correlation_id, occurrence_count, first_seen_at, last_seen_at,
                active, recovered_at FROM main.{_TABLE}
                ORDER BY subsystem, error_code, classification"""
            )
        )
        return tuple(self._event_from_row(row) for row in rows)

    def _event_from_row(self, row: tuple[object, ...]) -> HealthEvent:
        if len(row) != 10:
            raise ValueError(SYSTEM_HEALTH_SCHEMA_FAILURE)
        subsystem, error_code, classification, severity, correlation = row[:5]
        definition = _DEFINITIONS.get((subsystem, error_code))
        if (
            definition is None
            or classification != definition.classification
            or severity != definition.severity
            or not isinstance(correlation, str)
            or not self._valid_correlation(correlation)
        ):
            raise ValueError(SYSTEM_HEALTH_SCHEMA_FAILURE)
        count, first_seen, last_seen, active, recovered = row[5:]
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not self._valid_time(first_seen)
            or not self._valid_time(last_seen)
            or float(last_seen) < float(first_seen)
            or active not in (0, 1)
            or (
                recovered is not None
                and (
                    not self._valid_time(recovered)
                    or float(recovered) < float(last_seen)
                )
            )
            or (active == 1 and recovered is not None)
        ):
            raise ValueError(SYSTEM_HEALTH_SCHEMA_FAILURE)
        return HealthEvent(
            subsystem=definition.subsystem,
            error_code=definition.error_code,
            classification=definition.classification,
            severity=definition.severity,
            correlation_id=correlation,
            occurrence_count=count,
            first_seen_at=float(first_seen),
            last_seen_at=float(last_seen),
            active=bool(active),
            recovered_at=None if recovered is None else float(recovered),
        )

    def _merge_durable(self, durable: Iterable[HealthEvent]) -> None:
        for event in durable:
            key = self._event_key(event)
            if key in self._dirty:
                continue
            current = self._events.get(key)
            if current is None or event.occurrence_count >= current.occurrence_count:
                self._events[key] = event

    def _projection(self, schema: str) -> dict[str, object]:
        conditions = sorted(
            self._events.values(),
            key=lambda event: (
                event.subsystem,
                event.error_code,
                event.classification,
            ),
        )
        active = [event for event in conditions if event.active]
        state = "none"
        if any(event.classification == "unexpected_invariant" for event in active):
            state = "failed"
        elif active:
            state = "degraded"
        return {
            "schema": schema,
            "state": state,
            "active_condition_count": len(active),
            "total_condition_count": len(conditions),
            "durable_available": self._durable_available,
            "conditions": [asdict(event) for event in conditions],
        }

    def _safe_correlation(self, supplied: str | None) -> str:
        if supplied is not None and self._valid_correlation(supplied):
            return supplied
        try:
            generated = self._correlation_factory()
        except Exception:
            generated = ""
        if isinstance(generated, str) and self._valid_correlation(generated):
            return generated
        return uuid.uuid4().hex

    @staticmethod
    def _valid_correlation(value: str) -> bool:
        return bool(_CORRELATION_ID.fullmatch(value))

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception:
            value = time.time()
        if not self._valid_time(value):
            value = time.time()
        return float(value) if self._valid_time(value) else 1.0

    @staticmethod
    def _valid_time(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    def _report_recorder_unavailable(
        self, error: BaseException, correlation: str, now: float
    ) -> None:
        previous = self._last_recorder_log_at
        if (
            previous is not None
            and now - previous < self._recorder_log_interval_s
        ):
            return
        self._last_recorder_log_at = now
        try:
            self._logger.warning(
                "health_recorder_unavailable exception_class=%s correlation_id=%s",
                type(error).__name__,
                correlation,
            )
        except Exception:
            return

    @staticmethod
    def _event_key(
        value: HealthConditionDefinition | HealthEvent,
    ) -> tuple[str, str, str]:
        return (value.subsystem, value.error_code, value.classification)
