"""Transactional persistence for the WorldLex Translation Memory.

WorldLex definitions are compiler-produced, content-addressed records.  This module does not
interpret or grant capabilities; it only establishes stable world identities and admits an exact,
append-only definition lineage into an existing SQLite database.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .capability_glossary import (
    CAPABILITY_KINDS,
    COMPILER_VERSION,
    DEFINITION_SCHEMA,
    GlossaryError,
    content_fingerprint,
)


WORLD_ID_PATTERN = re.compile(r"world_[0-9a-f]{32}\Z")
FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
OWNER_SCOPES = frozenset({"world", "actor", "enemy_blueprint"})


class WorldLexError(RuntimeError):
    """Base error for WorldLex persistence."""


class WorldIdentityError(WorldLexError):
    """A world identity is unknown, malformed, or conflicts with its stored lineage."""


class DefinitionValidationError(WorldLexError):
    """A definition is not a valid compiler-produced storage record."""


class DefinitionLineageError(WorldLexError):
    """A definition revision does not extend its immediately previous revision."""


class CrossWorldDefinitionError(DefinitionLineageError):
    """A definition or parent fingerprint attempts to cross a world boundary."""


class DefinitionConflictError(WorldLexError):
    """An immutable definition identity is already occupied by different content."""


@dataclass(frozen=True)
class WorldLineage:
    """The immutable identity edge for one world."""

    world_id: str
    parent_world_id: str | None
    created_at: float


@dataclass(frozen=True)
class DefinitionAppendResult:
    """The admitted record and whether this call inserted it."""

    inserted: bool
    record: dict[str, Any]


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS worldlex_world_lineages (
        world_id TEXT PRIMARY KEY,
        parent_world_id TEXT REFERENCES worldlex_world_lineages(world_id),
        created_at REAL NOT NULL,
        CHECK (
            length(world_id) = 38
            AND substr(world_id, 1, 6) = 'world_'
            AND substr(world_id, 7) NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            parent_world_id IS NULL OR (
                length(parent_world_id) = 38
                AND substr(parent_world_id, 1, 6) = 'world_'
                AND substr(parent_world_id, 7) NOT GLOB '*[^0-9a-f]*'
                AND parent_world_id <> world_id
            )
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worldlex_capability_definitions (
        world_id TEXT NOT NULL REFERENCES worldlex_world_lineages(world_id),
        definition_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        fingerprint TEXT NOT NULL UNIQUE,
        parent_fingerprint TEXT,
        kind TEXT NOT NULL,
        owner_scope TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        schema TEXT NOT NULL,
        compiler_version TEXT NOT NULL,
        record_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (world_id, definition_id, revision),
        CHECK (schema = 'capability-definition/1'),
        CHECK (compiler_version = 'capability-compiler/1'),
        CHECK (kind IN ('skill', 'ability', 'spell', 'augment', 'cyberware', 'enemy_move')),
        CHECK (owner_scope IN ('world', 'actor', 'enemy_blueprint')),
        CHECK (
            length(world_id) = 38
            AND substr(world_id, 1, 6) = 'world_'
            AND substr(world_id, 7) NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            length(fingerprint) = 71
            AND substr(fingerprint, 1, 7) = 'sha256:'
            AND substr(fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            parent_fingerprint IS NULL OR (
                length(parent_fingerprint) = 71
                AND substr(parent_fingerprint, 1, 7) = 'sha256:'
                AND substr(parent_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
            )
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS worldlex_definition_lineage_idx
    ON worldlex_capability_definitions(world_id, definition_id, revision DESC)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_world_no_replacement
    BEFORE INSERT ON worldlex_world_lineages
    WHEN EXISTS (
        SELECT 1 FROM worldlex_world_lineages WHERE world_id = NEW.world_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex world identities are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_world_no_update
    BEFORE UPDATE ON worldlex_world_lineages
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex world identities are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_world_no_delete
    BEFORE DELETE ON worldlex_world_lineages
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex world identities are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_definition_no_replacement
    BEFORE INSERT ON worldlex_capability_definitions
    WHEN EXISTS (
        SELECT 1
        FROM worldlex_capability_definitions
        WHERE fingerprint = NEW.fingerprint
           OR (
               world_id = NEW.world_id
               AND definition_id = NEW.definition_id
               AND revision = NEW.revision
           )
    )
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex definition revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_definition_no_update
    BEFORE UPDATE ON worldlex_capability_definitions
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex definition revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS worldlex_definition_no_delete
    BEFORE DELETE ON worldlex_capability_definitions
    BEGIN
        SELECT RAISE(ABORT, 'WorldLex definition revisions are immutable');
    END
    """,
)


class WorldLexStore:
    """WorldLex repository composed over a caller-owned SQLite connection.

    ``connection`` remains owned by the caller.  Passing AetherState's existing re-entrant store
    lock lets WorldLex writes share the same process-level commit boundary.  If the caller already
    has a transaction open, each operation uses a savepoint and never commits the outer work.
    """

    def __init__(self, connection: sqlite3.Connection, lock: Any | None = None) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self._connection = connection
        self._lock = lock if lock is not None else threading.RLock()
        self._install_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the caller-owned connection for explicit transaction composition."""
        return self._connection

    def _install_schema(self) -> None:
        with self._write_transaction():
            for statement in _SCHEMA_STATEMENTS:
                self._connection.execute(statement)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            if self._connection.in_transaction:
                savepoint = "worldlex_" + uuid.uuid4().hex
                self._connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield
                except BaseException:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                else:
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                return

            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @staticmethod
    def _validate_world_id(world_id: object, field: str = "world_id") -> str:
        if not isinstance(world_id, str) or not WORLD_ID_PATTERN.fullmatch(world_id):
            raise WorldIdentityError(f"{field} must be world_ followed by 32 lowercase hex digits")
        return world_id

    @staticmethod
    def _validate_fingerprint(value: object, field: str) -> str:
        if not isinstance(value, str) or not FINGERPRINT_PATTERN.fullmatch(value):
            raise DefinitionValidationError(f"{field} must be a sha256 content fingerprint")
        return value

    @staticmethod
    def _required_text(record: Mapping[str, Any], field: str, *, maximum: int = 256) -> str:
        value = record.get(field)
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
            raise DefinitionValidationError(
                f"{field} must be a non-empty, trimmed string no longer than {maximum} characters"
            )
        return value

    @staticmethod
    def _snapshot(record: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
        if not isinstance(record, Mapping):
            raise DefinitionValidationError("definition record must be a mapping")
        if any(not isinstance(key, str) for key in record):
            raise DefinitionValidationError("definition record keys must be strings")
        try:
            encoded = json.dumps(
                dict(record),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            snapshot = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise DefinitionValidationError("definition record must contain finite JSON data") from exc
        if not isinstance(snapshot, dict):
            raise DefinitionValidationError("definition record must be a JSON object")
        return snapshot, encoded

    @classmethod
    def _validate_definition(
        cls,
        record: Mapping[str, Any],
        expected_world_id: str | None,
    ) -> tuple[dict[str, Any], str]:
        snapshot, encoded = cls._snapshot(record)
        required_fields = {
            "schema",
            "compiler_version",
            "definition_id",
            "revision",
            "parent_fingerprint",
            "kind",
            "world_id",
            "owner_scope",
            "owner_id",
            "fingerprint",
        }
        missing = sorted(required_fields - snapshot.keys())
        if missing:
            raise DefinitionValidationError(f"definition record is missing fields: {missing}")

        if snapshot["schema"] != DEFINITION_SCHEMA:
            raise DefinitionValidationError(f"schema must be {DEFINITION_SCHEMA}")
        if snapshot["compiler_version"] != COMPILER_VERSION:
            raise DefinitionValidationError(f"compiler_version must be {COMPILER_VERSION}")

        world_id = cls._validate_world_id(snapshot["world_id"])
        if expected_world_id is not None:
            expected_world_id = cls._validate_world_id(expected_world_id, "expected_world_id")
            if world_id != expected_world_id:
                raise CrossWorldDefinitionError(
                    f"definition world {world_id} does not match expected world {expected_world_id}"
                )

        definition_id = cls._required_text(snapshot, "definition_id", maximum=200)
        revision = snapshot["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DefinitionValidationError("revision must be an integer of at least 1")

        kind = snapshot["kind"]
        if not isinstance(kind, str) or kind not in CAPABILITY_KINDS:
            raise DefinitionValidationError(f"kind must be one of {sorted(CAPABILITY_KINDS)}")

        owner_scope = snapshot["owner_scope"]
        if not isinstance(owner_scope, str) or owner_scope not in OWNER_SCOPES:
            raise DefinitionValidationError(f"owner_scope must be one of {sorted(OWNER_SCOPES)}")
        owner_id = cls._required_text(snapshot, "owner_id")
        if owner_scope == "world" and owner_id != world_id:
            raise DefinitionValidationError("world-scoped definitions must use world_id as owner_id")

        fingerprint = cls._validate_fingerprint(snapshot["fingerprint"], "fingerprint")
        parent_fingerprint = snapshot["parent_fingerprint"]
        if revision == 1:
            if parent_fingerprint is not None:
                raise DefinitionLineageError("revision 1 must not have a parent_fingerprint")
        else:
            cls._validate_fingerprint(parent_fingerprint, "parent_fingerprint")

        fingerprint_payload = dict(snapshot)
        del fingerprint_payload["fingerprint"]
        try:
            recomputed = content_fingerprint(fingerprint_payload)
        except GlossaryError as exc:
            raise DefinitionValidationError("definition fingerprint payload is invalid") from exc
        if fingerprint != recomputed:
            raise DefinitionValidationError(
                f"fingerprint does not match canonical definition content: expected {recomputed}"
            )

        # Keep the names alive for static analysis and make the validated envelope explicit.
        assert definition_id and kind and owner_id
        return snapshot, encoded

    def ensure_world_lineage(
        self,
        world_id: str | None = None,
        *,
        parent_world_id: str | None = None,
    ) -> str:
        """Create or idempotently verify one stable world identity.

        New ids use ``world_`` plus 32 lowercase hexadecimal digits.  A parent is an immutable
        lineage edge, must already exist, and must be supplied identically on repeated calls.
        """
        if world_id is None:
            world_id = "world_" + uuid.uuid4().hex
        world_id = self._validate_world_id(world_id)
        if parent_world_id is not None:
            parent_world_id = self._validate_world_id(parent_world_id, "parent_world_id")
            if parent_world_id == world_id:
                raise WorldIdentityError("a world cannot be its own parent")

        with self._write_transaction():
            existing = self._connection.execute(
                "SELECT parent_world_id FROM worldlex_world_lineages WHERE world_id = ?",
                (world_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != parent_world_id:
                    raise WorldIdentityError(
                        f"world {world_id} already has parent_world_id={existing[0]!r}"
                    )
                return world_id

            if parent_world_id is not None:
                parent = self._connection.execute(
                    "SELECT 1 FROM worldlex_world_lineages WHERE world_id = ?",
                    (parent_world_id,),
                ).fetchone()
                if parent is None:
                    raise WorldIdentityError(f"unknown parent world {parent_world_id}")

            self._connection.execute(
                """
                INSERT INTO worldlex_world_lineages(world_id, parent_world_id, created_at)
                VALUES (?, ?, ?)
                """,
                (world_id, parent_world_id, time.time()),
            )
        return world_id

    def has_world_lineage(self, world_id: str) -> bool:
        world_id = self._validate_world_id(world_id)
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT 1 FROM worldlex_world_lineages WHERE world_id = ?", (world_id,)
                ).fetchone()
                is not None
            )

    def get_world_lineage(self, world_id: str) -> WorldLineage | None:
        world_id = self._validate_world_id(world_id)
        with self._lock:
            row = self._connection.execute(
                """
                SELECT world_id, parent_world_id, created_at
                FROM worldlex_world_lineages
                WHERE world_id = ?
                """,
                (world_id,),
            ).fetchone()
        if row is None:
            return None
        return WorldLineage(world_id=row[0], parent_world_id=row[1], created_at=float(row[2]))

    def append_definition(
        self,
        record: Mapping[str, Any],
        *,
        expected_world_id: str | None = None,
    ) -> DefinitionAppendResult:
        """Transactionally admit an exact definition revision.

        Repeating the same canonical record is idempotent.  Reusing its revision identity or
        fingerprint for different content is a conflict.  Later revisions must name the stored,
        immediately previous fingerprint in the same world, definition, kind, and owner scope.
        """
        snapshot, record_json = self._validate_definition(record, expected_world_id)
        world_id = snapshot["world_id"]
        definition_id = snapshot["definition_id"]
        revision = snapshot["revision"]
        fingerprint = snapshot["fingerprint"]

        with self._write_transaction():
            world = self._connection.execute(
                "SELECT 1 FROM worldlex_world_lineages WHERE world_id = ?", (world_id,)
            ).fetchone()
            if world is None:
                raise WorldIdentityError(f"unknown world {world_id}")

            occupied = self._connection.execute(
                """
                SELECT fingerprint, record_json
                FROM worldlex_capability_definitions
                WHERE world_id = ? AND definition_id = ? AND revision = ?
                """,
                (world_id, definition_id, revision),
            ).fetchone()
            if occupied is not None:
                if occupied[0] == fingerprint and occupied[1] == record_json:
                    return DefinitionAppendResult(inserted=False, record=json.loads(occupied[1]))
                raise DefinitionConflictError(
                    f"definition {definition_id!r} revision {revision} is already immutable"
                )

            reused_fingerprint = self._connection.execute(
                """
                SELECT world_id, definition_id, revision
                FROM worldlex_capability_definitions
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            if reused_fingerprint is not None:
                raise DefinitionConflictError(
                    f"fingerprint {fingerprint} already identifies another definition revision"
                )

            latest = self._connection.execute(
                """
                SELECT revision, fingerprint, kind, owner_scope, owner_id
                FROM worldlex_capability_definitions
                WHERE world_id = ? AND definition_id = ?
                ORDER BY revision DESC
                LIMIT 1
                """,
                (world_id, definition_id),
            ).fetchone()

            if revision == 1:
                if latest is not None:
                    raise DefinitionConflictError(
                        f"definition {definition_id!r} already has a revision lineage"
                    )
            else:
                if latest is None:
                    raise DefinitionLineageError(
                        f"definition {definition_id!r} has no revision {revision - 1}"
                    )
                expected_revision = int(latest[0]) + 1
                if revision != expected_revision:
                    raise DefinitionLineageError(
                        f"revision {revision} is not the exact next revision {expected_revision}"
                    )

                parent_fingerprint = snapshot["parent_fingerprint"]
                parent = self._connection.execute(
                    """
                    SELECT world_id, definition_id, revision, kind, owner_scope, owner_id
                    FROM worldlex_capability_definitions
                    WHERE fingerprint = ?
                    """,
                    (parent_fingerprint,),
                ).fetchone()
                if parent is None:
                    raise DefinitionLineageError(
                        f"parent fingerprint {parent_fingerprint} does not exist"
                    )
                if parent[0] != world_id:
                    raise CrossWorldDefinitionError(
                        f"parent fingerprint belongs to world {parent[0]}, not {world_id}"
                    )
                if parent[1] != definition_id:
                    raise DefinitionLineageError("parent fingerprint belongs to another definition")
                if parent[2] != latest[0] or parent_fingerprint != latest[1]:
                    raise DefinitionLineageError(
                        "parent_fingerprint must identify the immediately previous revision"
                    )
                if snapshot["kind"] != latest[2] or parent[3] != latest[2]:
                    raise DefinitionLineageError("kind cannot change within a definition lineage")
                if (
                    snapshot["owner_scope"] != latest[3]
                    or snapshot["owner_id"] != latest[4]
                    or parent[4] != latest[3]
                    or parent[5] != latest[4]
                ):
                    raise DefinitionLineageError(
                        "owner_scope and owner_id cannot change within a definition lineage"
                    )

            self._connection.execute(
                """
                INSERT INTO worldlex_capability_definitions(
                    world_id,
                    definition_id,
                    revision,
                    fingerprint,
                    parent_fingerprint,
                    kind,
                    owner_scope,
                    owner_id,
                    schema,
                    compiler_version,
                    record_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    world_id,
                    definition_id,
                    revision,
                    fingerprint,
                    snapshot["parent_fingerprint"],
                    snapshot["kind"],
                    snapshot["owner_scope"],
                    snapshot["owner_id"],
                    snapshot["schema"],
                    snapshot["compiler_version"],
                    record_json,
                    time.time(),
                ),
            )

        return DefinitionAppendResult(inserted=True, record=json.loads(record_json))

    def get_definition(
        self,
        world_id: str,
        definition_id: str,
        revision: int | None = None,
    ) -> dict[str, Any] | None:
        world_id = self._validate_world_id(world_id)
        definition_id = self._required_text({"definition_id": definition_id}, "definition_id", maximum=200)
        parameters: tuple[Any, ...]
        if revision is None:
            query = """
                SELECT record_json
                FROM worldlex_capability_definitions
                WHERE world_id = ? AND definition_id = ?
                ORDER BY revision DESC
                LIMIT 1
            """
            parameters = (world_id, definition_id)
        else:
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise DefinitionValidationError("revision must be an integer of at least 1")
            query = """
                SELECT record_json
                FROM worldlex_capability_definitions
                WHERE world_id = ? AND definition_id = ? AND revision = ?
            """
            parameters = (world_id, definition_id, revision)
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        return None if row is None else json.loads(row[0])

    def get_by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        fingerprint = self._validate_fingerprint(fingerprint, "fingerprint")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT record_json
                FROM worldlex_capability_definitions
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def list_revisions(self, world_id: str, definition_id: str) -> list[dict[str, Any]]:
        world_id = self._validate_world_id(world_id)
        definition_id = self._required_text({"definition_id": definition_id}, "definition_id", maximum=200)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT record_json
                FROM worldlex_capability_definitions
                WHERE world_id = ? AND definition_id = ?
                ORDER BY revision
                """,
                (world_id, definition_id),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


__all__ = [
    "CrossWorldDefinitionError",
    "DefinitionAppendResult",
    "DefinitionConflictError",
    "DefinitionLineageError",
    "DefinitionValidationError",
    "WorldIdentityError",
    "WorldLexError",
    "WorldLexStore",
    "WorldLineage",
]
