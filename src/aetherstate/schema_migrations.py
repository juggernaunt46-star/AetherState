"""Ordered, content-free SQLite schema migration support."""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, NoReturn, cast


MigrationCheck = Callable[[sqlite3.Connection], bool]
MigrationTransform = Callable[[sqlite3.Connection], None]


def _normalize_sql_outside_quotes(sql: str) -> str:
    """Canonicalize SQLite tokens without erasing comments or operator semantics."""
    return _join_sql_tokens(_sql_tokens(sql))


def _sql_tokens(sql: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = len(sql) if closing < 0 else closing + 2
            continue
        if character in {"X", "x"} and index + 1 < len(sql) and sql[index + 1] == "'":
            quoted, index = _quoted_sql_token(sql, index + 1, "'")
            tokens.append(("blob", _ascii_lower(character) + quoted))
            continue
        if character in {"'", '"', "`"}:
            token, index = _quoted_sql_token(sql, index, character)
            kind = (
                "single_quoted"
                if character == "'"
                else "double_quoted"
                if character == '"'
                else "identifier"
            )
            tokens.append((kind, token))
            continue
        if character == "[":
            closing = sql.find("]", index + 1)
            end = len(sql) if closing < 0 else closing + 1
            tokens.append(("identifier", sql[index:end]))
            index = end
            continue
        if _is_sql_identifier_start(character):
            end = index + 1
            while end < len(sql) and _is_sql_identifier_continue(sql[end]):
                end += 1
            tokens.append(("word", _ascii_lower(sql[index:end])))
            index = end
            continue
        if character.isdigit() or (character == "." and index + 1 < len(sql) and sql[index + 1].isdigit()):
            end = _numeric_sql_token_end(sql, index)
            tokens.append(("word", _ascii_lower(sql[index:end])))
            index = end
            continue
        if character in {"(", ")", ",", ";"}:
            tokens.append(("punctuation", character))
            index += 1
            continue
        operator = next(
            (candidate for candidate in ("->>", "->", "||", "<<", ">>", "<=", ">=", "<>", "!=", "==", ":=") if sql.startswith(candidate, index)),
            character,
        )
        tokens.append(("operator", operator))
        index += len(operator)
    return tokens


def _is_sql_identifier_start(character: str) -> bool:
    return character == "_" or _is_ascii_alpha(character) or ord(character) >= 128


def _is_sql_identifier_continue(character: str) -> bool:
    return _is_sql_identifier_start(character) or character.isdigit() or character == "$"


def _is_ascii_alpha(character: str) -> bool:
    return "A" <= character <= "Z" or "a" <= character <= "z"


def _ascii_lower(text: str) -> str:
    return "".join(chr(ord(character) + 32) if "A" <= character <= "Z" else character for character in text)


def sqlite_ascii_fold(value: object) -> str:
    """Match SQLite's ASCII-only identifier case folding without Unicode overmatch."""
    return _ascii_lower(value) if isinstance(value, str) else ""


def _numeric_sql_token_end(sql: str, start: int) -> int:
    if sql[start:start + 2].lower() == "0x":
        return _consume_sql_digits(sql, start + 2, _is_hex_digit)
    index = start
    if sql[index] == ".":
        index = _consume_sql_digits(sql, index + 1, str.isdigit)
    else:
        index = _consume_sql_digits(sql, index, str.isdigit)
        if index < len(sql) and sql[index] == ".":
            index = _consume_sql_digits(sql, index + 1, str.isdigit)
    if index < len(sql) and sql[index] in {"e", "E"}:
        exponent = index + 1
        if exponent < len(sql) and sql[exponent] in {"+", "-"}:
            exponent += 1
        exponent_end = _consume_sql_digits(sql, exponent, str.isdigit)
        if exponent_end > exponent:
            index = exponent_end
    return index


def _consume_sql_digits(sql: str, start: int, predicate: Callable[[str], bool]) -> int:
    index = start
    while index < len(sql):
        if predicate(sql[index]):
            index += 1
            continue
        if (
            sql[index] == "_"
            and index > start
            and index + 1 < len(sql)
            and predicate(sql[index - 1])
            and predicate(sql[index + 1])
        ):
            index += 1
            continue
        break
    return index


def _is_hex_digit(character: str) -> bool:
    return character.isdigit() or "a" <= character.lower() <= "f"


def _quoted_sql_token(sql: str, start: int, delimiter: str) -> tuple[str, int]:
    index = start + 1
    while index < len(sql):
        if sql[index] == delimiter:
            if delimiter in {"'", '"', "`"} and index + 1 < len(sql) and sql[index + 1] == delimiter:
                index += 2
                continue
            return sql[start : index + 1], index + 1
        index += 1
    return sql[start:], len(sql)


_SQLITE_TABLE_REFERENCE_KEYWORDS = {
    "from",
    "into",
    "join",
    "on",
    "references",
    "table",
    "update",
}


def _sql_references_identifier(sql: object, identifier: str) -> bool:
    if not isinstance(sql, str):
        return False
    target = sqlite_ascii_fold(identifier)
    tokens = _sql_tokens(sql)
    for index, (kind, token) in enumerate(tokens):
        if kind == "word" and token == target:
            return True
        if kind == "identifier" and sqlite_ascii_fold(_unquote_sql_identifier(token)) == target:
            return True
        if (
            kind in {"single_quoted", "double_quoted"}
            and sqlite_ascii_fold(_unquote_sql_identifier(token)) == target
            and _quoted_table_reference_context(tokens, index)
        ):
            return True
    return False


def _unquote_sql_identifier(token: str) -> str:
    if token.startswith("[") and token.endswith("]"):
        return token[1:-1]
    if len(token) >= 2 and token[0] in {"'", '"', "`"} and token[-1] == token[0]:
        delimiter = token[0]
        return token[1:-1].replace(delimiter * 2, delimiter)
    return token


def _quoted_table_reference_context(tokens: list[tuple[str, str]], index: int) -> bool:
    for kind, token in reversed(tokens[max(0, index - 3) : index]):
        if kind == "word":
            return token in _SQLITE_TABLE_REFERENCE_KEYWORDS
        if kind == "operator" and token == ".":
            continue
        if kind in {"identifier", "double_quoted", "single_quoted"}:
            continue
        break
    return False


def _join_sql_tokens(tokens: list[tuple[str, str]]) -> str:
    normalized: list[str] = []
    previous: tuple[str, str] | None = None
    for token in tokens:
        if previous is not None and _sql_token_needs_space(previous, token):
            normalized.append(" ")
        normalized.append(token[1])
        previous = token
    return "".join(normalized)


def _sql_token_needs_space(previous: tuple[str, str], current: tuple[str, str]) -> bool:
    if current[0] == "punctuation" and current[1] in {")", ",", ";"}:
        return False
    if previous[0] == "punctuation" and previous[1] in {"(", ","}:
        return False
    return not (current[0] == "punctuation" and current[1] == "(")


REGISTRY_INVALID = "migration_registry_invalid"
LEDGER_SCHEMA_INVALID = "migration_ledger_schema_invalid"
LEDGER_IDENTITY_INVALID = "migration_ledger_identity_invalid"
LEDGER_UNKNOWN_VERSION = "migration_ledger_unknown_version"
LEDGER_ROW_INVALID = "migration_ledger_row_invalid"
DOMAIN_INVALID = "migration_domain_invalid"
TRANSACTION_ACTIVE = "migration_transaction_active"
CLEANUP_SCHEMA_INVALID = "migration_cleanup_schema_invalid"
CLEANUP_IDENTITY_INVALID = "migration_cleanup_identity_invalid"

_LEDGER = "aetherstate_schema_migrations"
_LEDGER_AUTOINDEX = f"sqlite_autoindex_{_LEDGER}_1"
_PENDING_CLEANUP = "aetherstate_schema_pending_cleanup"
_PENDING_CLEANUP_AUTOINDEX = f"sqlite_autoindex_{_PENDING_CLEANUP}_1"
_LEDGER_SCHEMA_SQL = """CREATE TABLE aetherstate_schema_migrations(
  version INTEGER PRIMARY KEY CHECK(typeof(version)='integer' AND version > 0),
  name TEXT NOT NULL UNIQUE CHECK(name <> ''),
  domain TEXT NOT NULL CHECK(domain <> ''),
  applied_at REAL NOT NULL CHECK(typeof(applied_at) IN ('integer','real') AND applied_at > 0)
)"""
_CREATE_LEDGER_SQL = _LEDGER_SCHEMA_SQL.replace("CREATE TABLE ", "CREATE TABLE main.", 1)
_NORMALIZED_LEDGER_SQL = _normalize_sql_outside_quotes(_LEDGER_SCHEMA_SQL)
_PENDING_CLEANUP_SCHEMA_SQL = """CREATE TABLE aetherstate_schema_pending_cleanup(
  domain TEXT NOT NULL CHECK(domain <> ''),
  version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version > 0),
  PRIMARY KEY(domain, version)
)"""
_CREATE_PENDING_CLEANUP_SQL = _PENDING_CLEANUP_SCHEMA_SQL.replace(
    "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS main.", 1
)
_NORMALIZED_PENDING_CLEANUP_SQL = _normalize_sql_outside_quotes(_PENDING_CLEANUP_SCHEMA_SQL)


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    domain: str
    failure_code: str
    applies: MigrationCheck
    is_current: MigrationCheck
    transform: MigrationTransform
    postcondition: MigrationCheck
    requires_cleanup: bool = False


class SchemaMigrationError(RuntimeError):
    """A content-free migration outcome suitable for durable callers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LedgerProblem(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class _CleanupProblem(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class SchemaMigrationRunner:
    """Run one globally ordered migration registry against caller-owned SQLite state."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: Any,
        migrations: Iterable[SchemaMigration],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._migrations = tuple(migrations)
        self._clock = clock

    def run_domain(self, domain: str) -> tuple[int, ...]:
        """Apply every unledgered migration in one known domain, in registry order."""
        with self._lock:
            registry = self._validated_registry()
            applied = self._public_ledger(registry)
            selected = self._selected_domain(domain, registry)
            committed: list[int] = []
            for migration in selected:
                if migration.version in applied:
                    self._require_postcondition(migration)
                    continue
                if self._run_unledgered(migration, registry):
                    applied.add(migration.version)
                    committed.append(migration.version)
            return tuple(committed)

    def applied(self) -> tuple[tuple[int, str, str], ...]:
        """Return the ordered, fully validated ledger identities without mutation."""
        with self._lock:
            registry = self._validated_registry()
            applied = self._public_ledger(registry)
            by_version = {migration.version: migration for migration in registry}
            return tuple(
                (version, by_version[version].name, by_version[version].domain)
                for version in sorted(applied)
            )

    def cleanup_pending(self, domain: str, version: int) -> bool:
        """Return whether one already-committed migration still needs WAL cleanup."""
        with self._lock:
            self._validate_cleanup_identity(domain, version)
            try:
                if not self._cleanup_table_exists():
                    return False
                return self._connection.execute(
                    f"SELECT 1 FROM main.{_PENDING_CLEANUP} WHERE domain=? AND version=?",
                    (domain, version),
                ).fetchone() is not None
            except _CleanupProblem as problem:
                raise SchemaMigrationError(problem.code) from None
            except sqlite3.Error:
                raise SchemaMigrationError(CLEANUP_SCHEMA_INVALID) from None

    def mark_cleanup_pending(self, domain: str, version: int) -> None:
        """Durably require a post-migration WAL cleanup before service availability."""
        with self._lock:
            if self._connection.in_transaction:
                raise SchemaMigrationError(TRANSACTION_ACTIVE)
            self._validate_cleanup_identity(domain, version)
            started = False
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                started = True
                self._create_cleanup_table_if_missing()
                self._connection.execute(
                    f"INSERT OR IGNORE INTO main.{_PENDING_CLEANUP}(domain, version) VALUES(?,?)",
                    (domain, version),
                )
                self._connection.execute("COMMIT")
            except _CleanupProblem as problem:
                if started:
                    self._rollback_started_transaction()
                raise SchemaMigrationError(problem.code) from None
            except sqlite3.Error:
                if started:
                    self._rollback_started_transaction()
                raise SchemaMigrationError(CLEANUP_SCHEMA_INVALID) from None
            else:
                return

    def clear_cleanup_pending(self, domain: str, version: int) -> None:
        """Clear one cleanup marker only after its caller completed the checkpoint."""
        with self._lock:
            if self._connection.in_transaction:
                raise SchemaMigrationError(TRANSACTION_ACTIVE)
            self._validate_cleanup_identity(domain, version)
            started = False
            try:
                if not self._cleanup_table_exists():
                    return
                self._connection.execute("BEGIN IMMEDIATE")
                started = True
                self._connection.execute(
                    f"DELETE FROM main.{_PENDING_CLEANUP} WHERE domain=? AND version=?",
                    (domain, version),
                )
                self._connection.execute("COMMIT")
            except _CleanupProblem as problem:
                if started:
                    self._rollback_started_transaction()
                raise SchemaMigrationError(problem.code) from None
            except sqlite3.Error:
                if started:
                    self._rollback_started_transaction()
                raise SchemaMigrationError(CLEANUP_SCHEMA_INVALID) from None
            else:
                return

    def _validate_cleanup_identity(self, domain: str, version: int) -> None:
        if not self._trimmed_text(domain) or not self._positive_integer(version):
            raise SchemaMigrationError(CLEANUP_IDENTITY_INVALID)
        owners = tuple(
            migration
            for migration in self._validated_registry()
            if migration.domain == domain
            and migration.version == version
            and migration.requires_cleanup
        )
        if len(owners) != 1:
            raise SchemaMigrationError(CLEANUP_IDENTITY_INVALID)

    def _cleanup_table_exists(self) -> bool:
        temp_objects = tuple(
            row
            for row in (
                tuple(candidate)
                for candidate in self._connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_temp_schema "
                    "ORDER BY type, name"
                )
            )
            if self._cleanup_object_matches(row)
        )
        if temp_objects:
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        objects = tuple(
            row
            for row in (
                tuple(candidate)
                for candidate in self._connection.execute(
                    "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                    "ORDER BY type, name"
                )
            )
            if self._cleanup_object_matches(row)
        )
        if not objects:
            return False
        expected_objects = {
            ("table", _PENDING_CLEANUP, _PENDING_CLEANUP),
            ("index", _PENDING_CLEANUP_AUTOINDEX, _PENDING_CLEANUP),
        }
        if {(row[0], row[1], row[2]) for row in objects} != expected_objects:
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        table_sql = next(row[3] for row in objects if row[0] == "table")
        if not isinstance(table_sql, str) or (
            _normalize_sql_outside_quotes(table_sql) != _NORMALIZED_PENDING_CLEANUP_SQL
        ):
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        autoindex_sql = next(row[3] for row in objects if row[0] == "index")
        if autoindex_sql is not None:
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        columns = tuple(
            tuple(row) for row in self._connection.execute(
                f"PRAGMA main.table_xinfo({_PENDING_CLEANUP})"
            )
        )
        if tuple((row[1], row[2], row[3], row[5]) for row in columns) != (
            ("domain", "TEXT", 1, 1),
            ("version", "INTEGER", 1, 2),
        ):
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        indexes = tuple(
            tuple(row) for row in self._connection.execute(
                f"PRAGMA main.index_list({_PENDING_CLEANUP})"
            )
        )
        if indexes != ((0, _PENDING_CLEANUP_AUTOINDEX, 1, "pk", 0),):
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        index_columns = tuple(
            tuple(row) for row in self._connection.execute(
                f"PRAGMA main.index_xinfo({_PENDING_CLEANUP_AUTOINDEX})"
            )
        )
        if index_columns != (
            (0, 0, "domain", 0, "BINARY", 1),
            (1, 1, "version", 0, "BINARY", 1),
            (2, -1, None, 0, "BINARY", 0),
        ):
            raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)
        return True

    @staticmethod
    def _cleanup_object_matches(row: tuple[object, ...]) -> bool:
        marker = sqlite_ascii_fold(_PENDING_CLEANUP)
        name = sqlite_ascii_fold(row[1])
        table_name = sqlite_ascii_fold(row[2])
        return (
            name == marker
            or table_name == marker
            or name.startswith(f"{marker}_")
            or _sql_references_identifier(row[3], _PENDING_CLEANUP)
        )

    def _create_cleanup_table_if_missing(self) -> None:
        if not self._cleanup_table_exists():
            self._connection.execute(_CREATE_PENDING_CLEANUP_SQL)
            if not self._cleanup_table_exists():
                raise _CleanupProblem(CLEANUP_SCHEMA_INVALID)

    def _validated_registry(self) -> tuple[SchemaMigration, ...]:
        previous_version = 0
        names: set[str] = set()
        versions: set[int] = set()
        domains: set[str] = set()
        validated: list[SchemaMigration] = []
        for migration in self._migrations:
            if not isinstance(migration, SchemaMigration):
                raise SchemaMigrationError(REGISTRY_INVALID)
            if not self._positive_integer(migration.version):
                raise SchemaMigrationError(REGISTRY_INVALID)
            if migration.version <= previous_version or migration.version in versions:
                raise SchemaMigrationError(REGISTRY_INVALID)
            if not all(
                self._trimmed_text(value)
                for value in (migration.name, migration.domain, migration.failure_code)
            ):
                raise SchemaMigrationError(REGISTRY_INVALID)
            if migration.name in names:
                raise SchemaMigrationError(REGISTRY_INVALID)
            if not all(
                callable(value)
                for value in (
                    migration.applies,
                    migration.is_current,
                    migration.transform,
                    migration.postcondition,
                )
            ) or not isinstance(migration.requires_cleanup, bool):
                raise SchemaMigrationError(REGISTRY_INVALID)
            previous_version = migration.version
            versions.add(migration.version)
            names.add(migration.name)
            domains.add(migration.domain)
            validated.append(migration)
        if not validated or not domains:
            raise SchemaMigrationError(REGISTRY_INVALID)
        return tuple(validated)

    def _selected_domain(
        self, domain: str, registry: tuple[SchemaMigration, ...]
    ) -> tuple[SchemaMigration, ...]:
        if not self._trimmed_text(domain):
            raise SchemaMigrationError(DOMAIN_INVALID)
        selected = tuple(migration for migration in registry if migration.domain == domain)
        if not selected:
            raise SchemaMigrationError(DOMAIN_INVALID)
        return selected

    def _validated_ledger(self, registry: tuple[SchemaMigration, ...]) -> set[int]:
        rows = self._ledger_rows()
        by_version = {migration.version: migration for migration in registry}
        applied: set[int] = set()
        for version, name, domain, applied_at in rows:
            if not self._positive_integer(version) or not self._trimmed_text(name) or not self._trimmed_text(domain):
                raise _LedgerProblem(LEDGER_ROW_INVALID)
            if not self._positive_clock(applied_at):
                raise _LedgerProblem(LEDGER_ROW_INVALID)
            version_number = cast(int, version)
            migration = by_version.get(version_number)
            if migration is None:
                raise _LedgerProblem(LEDGER_UNKNOWN_VERSION)
            if migration.name != name or migration.domain != domain:
                raise _LedgerProblem(LEDGER_IDENTITY_INVALID)
            applied.add(version_number)
        return applied

    def _ledger_rows(self) -> tuple[tuple[object, object, object, object], ...]:
        temp_objects = tuple(
            tuple(row)
            for row in self._connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_temp_schema "
                "ORDER BY type, name"
            )
            if self._ledger_object_matches(tuple(row))
        )
        if temp_objects:
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        objects = tuple(
            tuple(row)
            for row in self._connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
                "ORDER BY type, name"
            )
            if self._ledger_object_matches(tuple(row))
        )
        if not objects:
            return ()
        tables = [row for row in objects if row[0] == "table" and row[1] == _LEDGER]
        if len(tables) != 1:
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        table_sql = tables[0][3]
        if not isinstance(table_sql, str) or _normalize_sql_outside_quotes(table_sql) != _NORMALIZED_LEDGER_SQL:
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        for row in objects:
            if row[0] == "table" and row[1] == _LEDGER:
                continue
            if row[0] == "index" and row[1] == _LEDGER_AUTOINDEX and row[3] is None:
                continue
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        columns = tuple(tuple(row) for row in self._connection.execute(f"PRAGMA main.table_xinfo({_LEDGER})"))
        expected_columns = (
            ("version", "INTEGER", 0, 1),
            ("name", "TEXT", 1, 0),
            ("domain", "TEXT", 1, 0),
            ("applied_at", "REAL", 1, 0),
        )
        actual_columns = tuple((row[1], row[2], row[3], row[5]) for row in columns)
        if actual_columns != expected_columns:
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        indexes = tuple(tuple(row) for row in self._connection.execute(f"PRAGMA main.index_list({_LEDGER})"))
        if indexes != ((0, _LEDGER_AUTOINDEX, 1, "u", 0),):
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        index_columns = tuple(
            tuple(row) for row in self._connection.execute(f"PRAGMA main.index_xinfo({_LEDGER_AUTOINDEX})")
        )
        if index_columns != ((0, 1, "name", 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)):
            raise _LedgerProblem(LEDGER_SCHEMA_INVALID)
        return tuple(
            self._connection.execute(
                f"SELECT version, name, domain, applied_at FROM main.{_LEDGER} ORDER BY version"
            )
        )

    @staticmethod
    def _ledger_object_matches(row: tuple[object, ...]) -> bool:
        ledger = sqlite_ascii_fold(_LEDGER)
        return (
            sqlite_ascii_fold(row[1]) == ledger
            or sqlite_ascii_fold(row[2]) == ledger
        )

    def _run_unledgered(
        self, migration: SchemaMigration, registry: tuple[SchemaMigration, ...]
    ) -> bool:
        current, applies = self._preflight(migration)
        if not current and not applies:
            self._raise_migration(migration)
        if self._connection.in_transaction:
            raise SchemaMigrationError(TRANSACTION_ACTIVE)
        started = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            started = True
            refreshed_applied = self._validated_ledger(registry)
            if migration.version in refreshed_applied:
                self._require_postcondition(migration)
                self._connection.execute("COMMIT")
                return False
            if migration.requires_cleanup:
                self._cleanup_table_exists()
            current = self._check(migration.is_current, migration)
            applies = self._check(migration.applies, migration)
            if current:
                if not self._check(migration.postcondition, migration):
                    self._raise_migration(migration)
            else:
                if not applies:
                    self._raise_migration(migration)
                migration.transform(self._connection)
                if not self._check(migration.postcondition, migration) or not self._check(
                    migration.is_current, migration
                ):
                    self._raise_migration(migration)
            self._validated_ledger(registry)
            self._create_ledger_if_missing()
            if migration.requires_cleanup:
                self._create_cleanup_table_if_missing()
                self._connection.execute(
                    f"INSERT OR IGNORE INTO main.{_PENDING_CLEANUP}(domain, version) VALUES(?,?)",
                    (migration.domain, migration.version),
                )
            applied_at = self._clock()
            if not self._positive_clock(applied_at):
                self._raise_migration(migration)
            self._connection.execute(
                f"INSERT INTO main.{_LEDGER}(version, name, domain, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.domain, applied_at),
            )
            self._connection.execute("COMMIT")
            return True
        except _LedgerProblem as problem:
            if started:
                self._rollback_started_transaction()
            raise SchemaMigrationError(problem.code) from None
        except _CleanupProblem as problem:
            if started:
                self._rollback_started_transaction()
            raise SchemaMigrationError(problem.code) from None
        except Exception:
            if started:
                self._rollback_started_transaction()
            self._raise_migration(migration)

    def _public_ledger(self, registry: tuple[SchemaMigration, ...]) -> set[int]:
        try:
            return self._validated_ledger(registry)
        except _LedgerProblem as problem:
            raise SchemaMigrationError(problem.code) from None
        except sqlite3.Error:
            raise SchemaMigrationError(LEDGER_SCHEMA_INVALID) from None

    def _preflight(self, migration: SchemaMigration) -> tuple[bool, bool]:
        try:
            return (
                bool(migration.is_current(self._connection)),
                bool(migration.applies(self._connection)),
            )
        except Exception:
            self._raise_migration(migration)

    def _require_postcondition(self, migration: SchemaMigration) -> None:
        if not self._check(migration.postcondition, migration):
            self._raise_migration(migration)

    def _check(self, check: MigrationCheck, migration: SchemaMigration) -> bool:
        try:
            return bool(check(self._connection))
        except Exception:
            self._raise_migration(migration)

    def _create_ledger_if_missing(self) -> None:
        if not self._ledger_exists():
            self._connection.execute(_CREATE_LEDGER_SQL)

    def _ledger_exists(self) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name = ?", (_LEDGER,)
        ).fetchone() is not None

    def _rollback_started_transaction(self) -> None:
        if self._connection.in_transaction:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    @staticmethod
    def _positive_integer(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _trimmed_text(value: object) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    @staticmethod
    def _positive_clock(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    @staticmethod
    def _raise_migration(migration: SchemaMigration) -> NoReturn:
        raise SchemaMigrationError(migration.failure_code) from None
