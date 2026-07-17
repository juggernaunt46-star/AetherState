"""Explicit local Player Lessons for narration behavior and intent interpretation.

Narration lessons are removable prompt preferences. Intent lessons may choose one exact meaning
that the current Player turn already recognized, before contextual binding, but cannot add a
meaning or grant authority. Player Lessons are not directives, mechanics, world facts, PlayerLex
approvals, or replay authority. Definitions remain separate from content-free frozen receipts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .semantic import SemanticTurn
from .semantic_fabric import load_default_semantic_fabric


LESSON_SCHEMA = "player-lesson/1"
TEST_SCHEMA = "player-lesson-test/1"
SELECTION_SCHEMA = "player-lesson-selection/1"
SELECTION_ITEM_SCHEMA = "player-lesson-selection-item/1"
INTENT_LESSON_SCHEMA = "player-lesson-intent/1"
INTENT_TEST_SCHEMA = "player-lesson-intent-test/1"
INTENT_SELECTION_SCHEMA = "player-lesson-intent-selection/1"
INTENT_SELECTION_ITEM_SCHEMA = "player-lesson-intent-selection-item/1"
INTENT_APPLICATION_SCHEMA = "player-lesson-intent-application/1"

EFFECT_TYPES = frozenset({"narration_behavior", "intent_interpretation"})
LESSON_SCOPES = frozenset({"every_rpg_turn", "exploration", "combat_opening", "combat_exchange"})
NARRATION_MODES = frozenset({"exploration", "combat_opening", "combat_exchange"})
LEX_IDS = frozenset({"capability", "referent", "scene", "action"})
INTENT_LEX_SLOTS = {"action": "action", "referent": "target"}
INTENT_APPLICATION_REASONS = frozenset(
    {
        "exact_binding_applied",
        "action_ambiguity_resolved",
        "target_ambiguity_resolved",
        "input_unambiguous",
        "current_input_conflict",
        "candidate_absent",
        "candidate_ambiguous",
        "lesson_conflict",
        "explicit_multi_target",
        "binding_failed",
    }
)
INTENT_APPLIED_REASONS = frozenset(
    {
        "exact_binding_applied",
        "action_ambiguity_resolved",
        "target_ambiguity_resolved",
    }
)
APPROVAL_METHOD = "local_control_api"
MAX_LESSONS = 64
MAX_TITLE_CHARS = 120
MAX_INSTRUCTION_CHARS = 1000
MAX_SAMPLE_CHARS = 2000
MAX_SELECTED_LESSONS = 5

_LESSON_ID_RE = re.compile(r"lesson_[0-9a-f]{32}\Z")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INPUT_HASH_RE = re.compile(r"[0-9a-f]{16}\Z")
_PLAYERLEX_APPROVAL_RE = re.compile(r"playerlex\.([0-9a-f]{32})\.r([1-9][0-9]*)\Z")
_STABLE_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

_LESSON_COLUMNS = (
    "lesson_id",
    "effect_type",
    "title",
    "scope",
    "do_text",
    "avoid_text",
    "anchor_entry_id",
    "anchor_lex_id",
    "anchor_concept_id",
    "anchor_meaning_fingerprint",
    "enabled",
    "revision",
    "fingerprint",
    "approved_via",
    "approved_at",
    "created_at",
    "updated_at",
)
_RECEIPT_COLUMNS = (
    "branch_id",
    "turn_index",
    "input_hash",
    "narration_mode",
    "frozen_selected_count",
    "inherited_from_branch_id",
    "selected_at",
)
_ITEM_COLUMNS = (
    "branch_id",
    "turn_index",
    "position",
    "lesson_id",
    "lesson_revision",
    "lesson_fingerprint",
    "reason",
    "scope",
    "anchor_entry_id",
    "anchor_lex_id",
    "anchor_concept_id",
    "anchor_meaning_fingerprint",
    "delivered",
    "delivered_at",
)
_INTENT_RECEIPT_COLUMNS = _RECEIPT_COLUMNS
_INTENT_ITEM_COLUMNS = (
    "branch_id",
    "turn_index",
    "position",
    "lesson_id",
    "lesson_revision",
    "lesson_fingerprint",
    "reason",
    "scope",
    "anchor_entry_id",
    "anchor_lex_id",
    "anchor_concept_id",
    "anchor_meaning_fingerprint",
    "intent_slot",
    "source_start",
    "source_end",
    "approval_source_id",
)
_INTENT_APPLICATION_COLUMNS = (
    "branch_id",
    "turn_index",
    "position",
    "lesson_id",
    "lesson_revision",
    "lesson_fingerprint",
    "applied",
    "reason",
    "frame_id",
    "selected_value",
    "meaning_binding_ref",
    "frame_fingerprint",
    "applied_at",
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS player_lessons (
        lesson_id TEXT PRIMARY KEY NOT NULL CHECK (
            length(lesson_id) = 39
            AND substr(lesson_id, 1, 7) = 'lesson_'
            AND substr(lesson_id, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        effect_type TEXT NOT NULL CHECK (effect_type = 'narration_behavior'),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
        scope TEXT NOT NULL CHECK (
            scope IN ('every_rpg_turn', 'exploration', 'combat_opening', 'combat_exchange')
        ),
        do_text TEXT NOT NULL CHECK (length(do_text) <= 1000),
        avoid_text TEXT NOT NULL CHECK (length(avoid_text) <= 1000),
        anchor_entry_id TEXT,
        anchor_lex_id TEXT,
        anchor_concept_id TEXT,
        anchor_meaning_fingerprint TEXT,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        fingerprint TEXT NOT NULL CHECK (
            length(fingerprint) = 71
            AND substr(fingerprint, 1, 7) = 'sha256:'
            AND substr(fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        approved_via TEXT NOT NULL CHECK (approved_via = 'local_control_api'),
        approved_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (length(do_text) > 0 OR length(avoid_text) > 0),
        CHECK (
            (anchor_entry_id IS NULL
             AND anchor_lex_id IS NULL
             AND anchor_concept_id IS NULL
             AND anchor_meaning_fingerprint IS NULL)
            OR
            (anchor_entry_id IS NOT NULL
             AND anchor_lex_id IN ('capability', 'referent', 'scene', 'action')
             AND anchor_concept_id IS NOT NULL
             AND length(anchor_concept_id) BETWEEN 1 AND 128
             AND anchor_meaning_fingerprint IS NOT NULL
             AND length(anchor_meaning_fingerprint) = 71
             AND substr(anchor_meaning_fingerprint, 1, 7) = 'sha256:'
             AND substr(anchor_meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*')
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_lesson_selection_receipts (
        branch_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
        input_hash TEXT NOT NULL CHECK (length(input_hash) BETWEEN 1 AND 256),
        narration_mode TEXT NOT NULL CHECK (
            narration_mode IN ('exploration', 'combat_opening', 'combat_exchange')
        ),
        frozen_selected_count INTEGER NOT NULL CHECK (
            frozen_selected_count BETWEEN 0 AND 5
        ),
        inherited_from_branch_id TEXT,
        selected_at REAL NOT NULL,
        PRIMARY KEY (branch_id, turn_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_lesson_selection_items (
        branch_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
        position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 4),
        lesson_id TEXT NOT NULL,
        lesson_revision INTEGER NOT NULL CHECK (lesson_revision >= 1),
        lesson_fingerprint TEXT NOT NULL CHECK (
            length(lesson_fingerprint) = 71
            AND substr(lesson_fingerprint, 1, 7) = 'sha256:'
            AND substr(lesson_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        reason TEXT NOT NULL CHECK (reason IN ('scope_match', 'scope_and_anchor_match')),
        scope TEXT NOT NULL CHECK (
            scope IN ('every_rpg_turn', 'exploration', 'combat_opening', 'combat_exchange')
        ),
        anchor_entry_id TEXT,
        anchor_lex_id TEXT,
        anchor_concept_id TEXT,
        anchor_meaning_fingerprint TEXT,
        delivered INTEGER NOT NULL CHECK (delivered IN (0, 1)),
        delivered_at REAL,
        PRIMARY KEY (branch_id, turn_index, position),
        UNIQUE (branch_id, turn_index, lesson_id),
        CHECK (
            (anchor_entry_id IS NULL
             AND anchor_lex_id IS NULL
             AND anchor_concept_id IS NULL
             AND anchor_meaning_fingerprint IS NULL)
            OR
            (anchor_entry_id IS NOT NULL
             AND anchor_lex_id IN ('capability', 'referent', 'scene', 'action')
             AND anchor_concept_id IS NOT NULL
             AND anchor_meaning_fingerprint IS NOT NULL
             AND length(anchor_meaning_fingerprint) = 71
             AND substr(anchor_meaning_fingerprint, 1, 7) = 'sha256:'
             AND substr(anchor_meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*')
        ),
        CHECK (
            (delivered = 0 AND delivered_at IS NULL)
            OR (delivered = 1 AND delivered_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS player_lesson_receipt_lesson_idx
    ON player_lesson_selection_items(lesson_id, branch_id, turn_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS player_lesson_receipt_time_idx
    ON player_lesson_selection_receipts(selected_at DESC, branch_id, turn_index)
    """,
)

# The first shipped narration-only schema is retained byte-for-byte so initialization can
# recognize exactly that one predecessor. Unknown or partially changed local schemas still fail
# closed instead of being guessed through a migration.
_V1_SCHEMA_STATEMENTS = _SCHEMA_STATEMENTS

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS player_lessons (
        lesson_id TEXT PRIMARY KEY NOT NULL CHECK (
            length(lesson_id) = 39
            AND substr(lesson_id, 1, 7) = 'lesson_'
            AND substr(lesson_id, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        effect_type TEXT NOT NULL CHECK (
            effect_type IN ('narration_behavior', 'intent_interpretation')
        ),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
        scope TEXT NOT NULL CHECK (
            scope IN ('every_rpg_turn', 'exploration', 'combat_opening', 'combat_exchange')
        ),
        do_text TEXT NOT NULL CHECK (length(do_text) <= 1000),
        avoid_text TEXT NOT NULL CHECK (length(avoid_text) <= 1000),
        anchor_entry_id TEXT,
        anchor_lex_id TEXT,
        anchor_concept_id TEXT,
        anchor_meaning_fingerprint TEXT,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        fingerprint TEXT NOT NULL CHECK (
            length(fingerprint) = 71
            AND substr(fingerprint, 1, 7) = 'sha256:'
            AND substr(fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        approved_via TEXT NOT NULL CHECK (approved_via = 'local_control_api'),
        approved_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        CHECK (
            (effect_type = 'narration_behavior'
             AND (length(do_text) > 0 OR length(avoid_text) > 0))
            OR
            (effect_type = 'intent_interpretation'
             AND length(do_text) > 0
             AND length(avoid_text) > 0
             AND anchor_entry_id IS NOT NULL
             AND anchor_lex_id IN ('action', 'referent'))
        ),
        CHECK (
            (anchor_entry_id IS NULL
             AND anchor_lex_id IS NULL
             AND anchor_concept_id IS NULL
             AND anchor_meaning_fingerprint IS NULL)
            OR
            (anchor_entry_id IS NOT NULL
             AND anchor_lex_id IN ('capability', 'referent', 'scene', 'action')
             AND anchor_concept_id IS NOT NULL
             AND length(anchor_concept_id) BETWEEN 1 AND 128
             AND anchor_meaning_fingerprint IS NOT NULL
             AND length(anchor_meaning_fingerprint) = 71
             AND substr(anchor_meaning_fingerprint, 1, 7) = 'sha256:'
             AND substr(anchor_meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*')
        )
    )
    """,
    *_V1_SCHEMA_STATEMENTS[1:],
    """
    CREATE TABLE IF NOT EXISTS player_lesson_intent_receipts (
        branch_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
        input_hash TEXT NOT NULL CHECK (length(input_hash) BETWEEN 1 AND 256),
        narration_mode TEXT NOT NULL CHECK (
            narration_mode IN ('exploration', 'combat_opening', 'combat_exchange')
        ),
        frozen_selected_count INTEGER NOT NULL CHECK (
            frozen_selected_count BETWEEN 0 AND 5
        ),
        inherited_from_branch_id TEXT,
        selected_at REAL NOT NULL,
        PRIMARY KEY (branch_id, turn_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_lesson_intent_selection_items (
        branch_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
        position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 4),
        lesson_id TEXT NOT NULL,
        lesson_revision INTEGER NOT NULL CHECK (lesson_revision >= 1),
        lesson_fingerprint TEXT NOT NULL CHECK (
            length(lesson_fingerprint) = 71
            AND substr(lesson_fingerprint, 1, 7) = 'sha256:'
            AND substr(lesson_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        reason TEXT NOT NULL CHECK (reason = 'scope_and_exact_anchor_recognition'),
        scope TEXT NOT NULL CHECK (
            scope IN ('every_rpg_turn', 'exploration', 'combat_opening', 'combat_exchange')
        ),
        anchor_entry_id TEXT NOT NULL,
        anchor_lex_id TEXT NOT NULL CHECK (anchor_lex_id IN ('action', 'referent')),
        anchor_concept_id TEXT NOT NULL CHECK (length(anchor_concept_id) BETWEEN 1 AND 128),
        anchor_meaning_fingerprint TEXT NOT NULL CHECK (
            length(anchor_meaning_fingerprint) = 71
            AND substr(anchor_meaning_fingerprint, 1, 7) = 'sha256:'
            AND substr(anchor_meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        intent_slot TEXT NOT NULL CHECK (intent_slot IN ('action', 'target')),
        source_start INTEGER NOT NULL CHECK (source_start >= 0),
        source_end INTEGER NOT NULL CHECK (source_end > source_start),
        approval_source_id TEXT NOT NULL CHECK (length(approval_source_id) BETWEEN 1 AND 256),
        PRIMARY KEY (branch_id, turn_index, position),
        UNIQUE (branch_id, turn_index, lesson_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_lesson_intent_applications (
        branch_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL CHECK (turn_index >= 0),
        position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 4),
        lesson_id TEXT NOT NULL,
        lesson_revision INTEGER NOT NULL CHECK (lesson_revision >= 1),
        lesson_fingerprint TEXT NOT NULL CHECK (
            length(lesson_fingerprint) = 71
            AND substr(lesson_fingerprint, 1, 7) = 'sha256:'
            AND substr(lesson_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        ),
        applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
        reason TEXT NOT NULL CHECK (
            reason IN (
                'exact_binding_applied', 'action_ambiguity_resolved',
                'target_ambiguity_resolved', 'input_unambiguous',
                'current_input_conflict', 'candidate_absent', 'candidate_ambiguous',
                'lesson_conflict', 'explicit_multi_target', 'binding_failed'
            )
        ),
        frame_id TEXT,
        selected_value TEXT,
        meaning_binding_ref TEXT,
        frame_fingerprint TEXT,
        applied_at REAL NOT NULL,
        PRIMARY KEY (branch_id, turn_index, position),
        UNIQUE (branch_id, turn_index, lesson_id),
        CHECK (
            (applied = 1
             AND reason IN (
                 'exact_binding_applied', 'action_ambiguity_resolved',
                 'target_ambiguity_resolved'
             )
             AND frame_id IS NOT NULL
             AND selected_value IS NOT NULL
             AND meaning_binding_ref IS NOT NULL
             AND length(meaning_binding_ref) = 71
             AND substr(meaning_binding_ref, 1, 7) = 'sha256:'
             AND substr(meaning_binding_ref, 8) NOT GLOB '*[^0-9a-f]*'
             AND frame_fingerprint IS NOT NULL
             AND length(frame_fingerprint) = 71
             AND substr(frame_fingerprint, 1, 7) = 'sha256:'
             AND substr(frame_fingerprint, 8) NOT GLOB '*[^0-9a-f]*')
            OR
            (applied = 0
             AND reason NOT IN (
                 'exact_binding_applied', 'action_ambiguity_resolved',
                 'target_ambiguity_resolved'
             )
             AND selected_value IS NULL
             AND meaning_binding_ref IS NULL
             AND frame_fingerprint IS NULL)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS player_lesson_intent_receipt_lesson_idx
    ON player_lesson_intent_selection_items(lesson_id, branch_id, turn_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS player_lesson_intent_receipt_time_idx
    ON player_lesson_intent_receipts(selected_at DESC, branch_id, turn_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS player_lesson_intent_application_lesson_idx
    ON player_lesson_intent_applications(lesson_id, branch_id, turn_index)
    """,
)

_EXPECTED_LESSON_COLUMNS = (
    ("lesson_id", "TEXT", 1, None, 1),
    ("effect_type", "TEXT", 1, None, 0),
    ("title", "TEXT", 1, None, 0),
    ("scope", "TEXT", 1, None, 0),
    ("do_text", "TEXT", 1, None, 0),
    ("avoid_text", "TEXT", 1, None, 0),
    ("anchor_entry_id", "TEXT", 0, None, 0),
    ("anchor_lex_id", "TEXT", 0, None, 0),
    ("anchor_concept_id", "TEXT", 0, None, 0),
    ("anchor_meaning_fingerprint", "TEXT", 0, None, 0),
    ("enabled", "INTEGER", 1, None, 0),
    ("revision", "INTEGER", 1, None, 0),
    ("fingerprint", "TEXT", 1, None, 0),
    ("approved_via", "TEXT", 1, None, 0),
    ("approved_at", "REAL", 1, None, 0),
    ("created_at", "REAL", 1, None, 0),
    ("updated_at", "REAL", 1, None, 0),
)
_EXPECTED_RECEIPT_COLUMNS = (
    ("branch_id", "TEXT", 1, None, 1),
    ("turn_index", "INTEGER", 1, None, 2),
    ("input_hash", "TEXT", 1, None, 0),
    ("narration_mode", "TEXT", 1, None, 0),
    ("frozen_selected_count", "INTEGER", 1, None, 0),
    ("inherited_from_branch_id", "TEXT", 0, None, 0),
    ("selected_at", "REAL", 1, None, 0),
)
_EXPECTED_ITEM_COLUMNS = (
    ("branch_id", "TEXT", 1, None, 1),
    ("turn_index", "INTEGER", 1, None, 2),
    ("position", "INTEGER", 1, None, 3),
    ("lesson_id", "TEXT", 1, None, 0),
    ("lesson_revision", "INTEGER", 1, None, 0),
    ("lesson_fingerprint", "TEXT", 1, None, 0),
    ("reason", "TEXT", 1, None, 0),
    ("scope", "TEXT", 1, None, 0),
    ("anchor_entry_id", "TEXT", 0, None, 0),
    ("anchor_lex_id", "TEXT", 0, None, 0),
    ("anchor_concept_id", "TEXT", 0, None, 0),
    ("anchor_meaning_fingerprint", "TEXT", 0, None, 0),
    ("delivered", "INTEGER", 1, None, 0),
    ("delivered_at", "REAL", 0, None, 0),
)
_EXPECTED_INTENT_RECEIPT_COLUMNS = _EXPECTED_RECEIPT_COLUMNS
_EXPECTED_INTENT_ITEM_COLUMNS = (
    ("branch_id", "TEXT", 1, None, 1),
    ("turn_index", "INTEGER", 1, None, 2),
    ("position", "INTEGER", 1, None, 3),
    ("lesson_id", "TEXT", 1, None, 0),
    ("lesson_revision", "INTEGER", 1, None, 0),
    ("lesson_fingerprint", "TEXT", 1, None, 0),
    ("reason", "TEXT", 1, None, 0),
    ("scope", "TEXT", 1, None, 0),
    ("anchor_entry_id", "TEXT", 1, None, 0),
    ("anchor_lex_id", "TEXT", 1, None, 0),
    ("anchor_concept_id", "TEXT", 1, None, 0),
    ("anchor_meaning_fingerprint", "TEXT", 1, None, 0),
    ("intent_slot", "TEXT", 1, None, 0),
    ("source_start", "INTEGER", 1, None, 0),
    ("source_end", "INTEGER", 1, None, 0),
    ("approval_source_id", "TEXT", 1, None, 0),
)
_EXPECTED_INTENT_APPLICATION_COLUMNS = (
    ("branch_id", "TEXT", 1, None, 1),
    ("turn_index", "INTEGER", 1, None, 2),
    ("position", "INTEGER", 1, None, 3),
    ("lesson_id", "TEXT", 1, None, 0),
    ("lesson_revision", "INTEGER", 1, None, 0),
    ("lesson_fingerprint", "TEXT", 1, None, 0),
    ("applied", "INTEGER", 1, None, 0),
    ("reason", "TEXT", 1, None, 0),
    ("frame_id", "TEXT", 0, None, 0),
    ("selected_value", "TEXT", 0, None, 0),
    ("meaning_binding_ref", "TEXT", 0, None, 0),
    ("frame_fingerprint", "TEXT", 0, None, 0),
    ("applied_at", "REAL", 1, None, 0),
)


class PlayerLessonsError(RuntimeError):
    """Base error for explicit local Player Lessons."""


class PlayerLessonsValidationError(PlayerLessonsError):
    """A lesson request violates the bounded service contract."""


class PlayerLessonsConflictError(PlayerLessonsError):
    """A displayed revision or frozen turn identity changed."""


class PlayerLessonsNotFoundError(PlayerLessonsError):
    """A requested lesson or selection receipt does not exist."""


class PlayerLessonsRetryableRemovalError(PlayerLessonsError):
    """A committed deletion still needs a successful WAL checkpoint."""

    def __init__(self, lesson_id: str) -> None:
        self.lesson_id = lesson_id
        super().__init__("secure Player Lesson removal needs a retry to finish scrubbing local storage")


def _normalized_schema_sql(sql: str) -> str:
    compact = " ".join(sql.strip().rstrip(";").split())
    compact = re.sub(r"\bIF\s+NOT\s+EXISTS\b\s*", "", compact, count=1, flags=re.IGNORECASE)
    return compact.casefold()


def _expected_index_xinfo(
    *columns: tuple[str, int],
) -> tuple[tuple[str | None, int, str, int], ...]:
    return tuple((column, desc, "BINARY", 1) for column, desc in columns) + ((None, 0, "BINARY", 0),)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_plain_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _finite_timestamp(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _clean_text(
    value: object,
    *,
    field: str,
    maximum: int,
    required: bool,
) -> str:
    if not isinstance(value, str):
        raise PlayerLessonsValidationError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise PlayerLessonsValidationError(f"{field} must be non-empty")
    if len(text) > maximum:
        raise PlayerLessonsValidationError(f"{field} must be at most {maximum} characters")
    if any(unicodedata.category(char).startswith("C") and not char.isspace() for char in text):
        raise PlayerLessonsValidationError(f"{field} contains unsupported control characters")
    return text


def _anchor_from_values(values: Mapping[str, Any]) -> dict[str, str] | None:
    fields = (
        values.get("anchor_entry_id"),
        values.get("anchor_lex_id"),
        values.get("anchor_concept_id"),
        values.get("anchor_meaning_fingerprint"),
    )
    if all(value is None for value in fields):
        return None
    if (
        not all(isinstance(value, str) and value for value in fields)
        or fields[1] not in LEX_IDS
        or len(str(fields[2])) > 128
        or _FINGERPRINT_RE.fullmatch(str(fields[3])) is None
    ):
        raise PlayerLessonsError("stored Player Lesson anchor is malformed")
    return {
        "entry_id": str(fields[0]),
        "lex_id": str(fields[1]),
        "concept_id": str(fields[2]),
        "meaning_fingerprint": str(fields[3]),
    }


class PlayerLessons:
    """SQLite-backed local lessons with effect-specific content-free frozen receipts."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        playerlex: Any | None = None,
        lock: Any | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if playerlex is not None and not callable(getattr(playerlex, "list_entries", None)):
            raise TypeError("playerlex must expose list_entries or be None")
        self._connection = connection
        self._playerlex = playerlex
        self._lock = lock if lock is not None else threading.RLock()
        if self._connection.in_transaction:
            raise PlayerLessonsError(
                "Player Lessons initialization requires no active transaction; retry after it closes"
            )
        self._install_schema()

    @contextmanager
    def lifecycle_guard(self) -> Iterator[None]:
        """Serialize one live lesson use with correction, disablement, or removal.

        Pipeline holds this guard from retrieval through content-free receipt persistence and
        request-cache publication. Control mutations take the same guard through cache eviction,
        so the operation that acquires it second must observe the first operation's final state.
        """
        with self._lock:
            yield

    def bind_playerlex(self, playerlex: Any) -> None:
        """Attach a recovered PlayerLex service without restarting the lesson runtime.

        A startup Atlas failure may leave unanchored narration lessons available while PlayerLex
        is absent.  Console recovery must make anchored tests, writes, and live retrieval available
        to the already-running service under the same lifecycle lock used by turn processing.
        """
        if not callable(getattr(playerlex, "list_entries", None)):
            raise TypeError("playerlex must expose list_entries")
        with self._lock:
            self._playerlex = playerlex

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Use a savepoint when selection runs inside the Store truth-gate transaction."""
        with self._lock:
            if self._connection.in_transaction:
                savepoint = "player_lessons_" + uuid.uuid4().hex
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

    def _install_schema(self) -> None:
        with self._lock:
            if self._connection.in_transaction:
                raise PlayerLessonsError(
                    "Player Lessons initialization requires no active transaction; retry after it closes"
                )
            try:
                secure_delete = self._connection.execute("PRAGMA secure_delete=ON").fetchone()
                if secure_delete is None or int(secure_delete[0]) != 1:
                    raise PlayerLessonsError("SQLite secure deletion is unavailable for Player Lessons")
                objects_exist = self._schema_objects_exist()
                migrate_v1 = objects_exist and self._v1_schema_matches()
                if migrate_v1:
                    self._checkpoint_wal()
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    if not objects_exist:
                        for statement in _SCHEMA_STATEMENTS:
                            self._connection.execute(statement)
                    elif migrate_v1:
                        self._migrate_v1_schema()
                    self._verify_schema()
                    self._normalize_receipt_input_hashes()
                except BaseException:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()
                if migrate_v1:
                    self._checkpoint_wal()
            except sqlite3.Error as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise PlayerLessonsError("Player Lessons local storage initialization failed") from exc

    def _schema_objects_exist(self) -> bool:
        return (
            self._connection.execute(
                """
                SELECT 1 FROM (
                    SELECT type, name, tbl_name FROM sqlite_master
                    UNION ALL
                    SELECT type, name, tbl_name FROM sqlite_temp_master
                )
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'player_lesson*' OR tbl_name GLOB 'player_lesson*')
                LIMIT 1
                """
            ).fetchone()
            is not None
        )

    def _normalize_receipt_input_hashes(self) -> None:
        """One-way scrub legacy/convention-only receipt inputs into the closed digest format."""
        for table in (
            "player_lesson_selection_receipts",
            "player_lesson_intent_receipts",
        ):
            if self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is None:
                continue
            rows = self._connection.execute(
                f"SELECT branch_id, turn_index, input_hash FROM {table}"
            ).fetchall()
            for row in rows:
                value = str(row["input_hash"])
                if _INPUT_HASH_RE.fullmatch(value) is not None:
                    continue
                digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()
                self._connection.execute(
                    f"UPDATE {table} SET input_hash=? WHERE branch_id=? AND turn_index=?",
                    (digest, row["branch_id"], row["turn_index"]),
                )

    def _schema_sql(self, object_type: str, name: str) -> str | None:
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            (object_type, name),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        return row[0]

    def _table_columns(self, table: str) -> tuple[tuple[str, str, int, Any, int], ...]:
        return tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        )

    def _index_metadata(
        self, table: str
    ) -> dict[str, tuple[int, str, int, tuple[tuple[str | None, int, str, int], ...]]]:
        result: dict[
            str,
            tuple[int, str, int, tuple[tuple[str | None, int, str, int], ...]],
        ] = {}
        for row in self._connection.execute(f"PRAGMA index_list({table})"):
            name = str(row[1])
            result[name] = (
                int(row[2]),
                str(row[3]),
                int(row[4]),
                tuple(
                    (
                        None if info[2] is None else str(info[2]),
                        int(info[3]),
                        str(info[4]),
                        int(info[5]),
                    )
                    for info in self._connection.execute(
                        """
                        SELECT seqno, cid, name, desc, coll, key
                        FROM pragma_index_xinfo(?)
                        ORDER BY seqno
                        """,
                        (name,),
                    )
                ),
            )
        return result

    def _persistent_schema_objects(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(
            (str(row[0]), str(row[1]), str(row[2]))
            for row in self._connection.execute(
                """
                SELECT type, name, tbl_name FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'player_lesson*' OR tbl_name GLOB 'player_lesson*')
                """
            )
        )

    def _temporary_schema_objects_exist(self) -> bool:
        return (
            self._connection.execute(
                """
                SELECT 1 FROM sqlite_temp_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'player_lesson*' OR tbl_name GLOB 'player_lesson*')
                LIMIT 1
                """
            ).fetchone()
            is not None
        )

    def _v1_schema_matches(self) -> bool:
        """Recognize only the exact narration-only predecessor eligible for migration."""
        expected_objects = frozenset(
            {
                ("table", "player_lessons", "player_lessons"),
                ("table", "player_lesson_selection_receipts", "player_lesson_selection_receipts"),
                ("table", "player_lesson_selection_items", "player_lesson_selection_items"),
                ("index", "sqlite_autoindex_player_lessons_1", "player_lessons"),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_receipts_1",
                    "player_lesson_selection_receipts",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_items_1",
                    "player_lesson_selection_items",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_items_2",
                    "player_lesson_selection_items",
                ),
                ("index", "player_lesson_receipt_lesson_idx", "player_lesson_selection_items"),
                ("index", "player_lesson_receipt_time_idx", "player_lesson_selection_receipts"),
            }
        )
        expected_lesson_indexes = {
            (1, "pk", 0, _expected_index_xinfo(("lesson_id", 0))),
        }
        expected_receipt_indexes = {
            (1, "pk", 0, _expected_index_xinfo(("branch_id", 0), ("turn_index", 0))),
            (
                0,
                "c",
                0,
                _expected_index_xinfo(("selected_at", 1), ("branch_id", 0), ("turn_index", 0)),
            ),
        }
        expected_item_indexes = {
            (
                1,
                "pk",
                0,
                _expected_index_xinfo(("branch_id", 0), ("turn_index", 0), ("position", 0)),
            ),
            (
                1,
                "u",
                0,
                _expected_index_xinfo(("branch_id", 0), ("turn_index", 0), ("lesson_id", 0)),
            ),
            (
                0,
                "c",
                0,
                _expected_index_xinfo(("lesson_id", 0), ("branch_id", 0), ("turn_index", 0)),
            ),
        }
        return bool(
            all(
                _normalized_schema_sql(self._schema_sql("table", name) or "")
                == _normalized_schema_sql(statement)
                for name, statement in zip(
                    (
                        "player_lessons",
                        "player_lesson_selection_receipts",
                        "player_lesson_selection_items",
                    ),
                    _V1_SCHEMA_STATEMENTS[:3],
                    strict=True,
                )
            )
            and _normalized_schema_sql(
                self._schema_sql("index", "player_lesson_receipt_lesson_idx") or ""
            )
            == _normalized_schema_sql(_V1_SCHEMA_STATEMENTS[3])
            and _normalized_schema_sql(
                self._schema_sql("index", "player_lesson_receipt_time_idx") or ""
            )
            == _normalized_schema_sql(_V1_SCHEMA_STATEMENTS[4])
            and self._table_columns("player_lessons") == _EXPECTED_LESSON_COLUMNS
            and self._table_columns("player_lesson_selection_receipts")
            == _EXPECTED_RECEIPT_COLUMNS
            and self._table_columns("player_lesson_selection_items") == _EXPECTED_ITEM_COLUMNS
            and self._persistent_schema_objects() == expected_objects
            and not self._temporary_schema_objects_exist()
            and set(self._index_metadata("player_lessons").values()) == expected_lesson_indexes
            and set(self._index_metadata("player_lesson_selection_receipts").values())
            == expected_receipt_indexes
            and set(self._index_metadata("player_lesson_selection_items").values())
            == expected_item_indexes
        )

    def _migrate_v1_schema(self) -> None:
        rows = self._connection.execute(
            f"SELECT {', '.join(_LESSON_COLUMNS)} FROM player_lessons"
        ).fetchall()
        for row in rows:
            values = self._row_values(row, _LESSON_COLUMNS)
            if values is None or values.get("effect_type") != "narration_behavior":
                raise PlayerLessonsError("Player Lessons v1 migration found a non-narration row")
            self._stored_lesson(row)
        self._connection.execute("ALTER TABLE player_lessons RENAME TO player_lessons_v1_migration")
        self._connection.execute(_SCHEMA_STATEMENTS[0])
        self._connection.execute(
            f"""
            INSERT INTO player_lessons({", ".join(_LESSON_COLUMNS)})
            SELECT {", ".join(_LESSON_COLUMNS)} FROM player_lessons_v1_migration
            """
        )
        self._connection.execute("DROP TABLE player_lessons_v1_migration")
        for statement in _SCHEMA_STATEMENTS[3:]:
            self._connection.execute(statement)

    def _verify_schema(self) -> None:
        lesson_indexes = self._index_metadata("player_lessons")
        receipt_indexes = self._index_metadata("player_lesson_selection_receipts")
        item_indexes = self._index_metadata("player_lesson_selection_items")
        intent_receipt_indexes = self._index_metadata("player_lesson_intent_receipts")
        intent_item_indexes = self._index_metadata("player_lesson_intent_selection_items")
        intent_application_indexes = self._index_metadata("player_lesson_intent_applications")
        expected_objects = frozenset(
            {
                ("table", "player_lessons", "player_lessons"),
                (
                    "table",
                    "player_lesson_selection_receipts",
                    "player_lesson_selection_receipts",
                ),
                (
                    "table",
                    "player_lesson_selection_items",
                    "player_lesson_selection_items",
                ),
                ("index", "sqlite_autoindex_player_lessons_1", "player_lessons"),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_receipts_1",
                    "player_lesson_selection_receipts",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_items_1",
                    "player_lesson_selection_items",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_selection_items_2",
                    "player_lesson_selection_items",
                ),
                (
                    "index",
                    "player_lesson_receipt_lesson_idx",
                    "player_lesson_selection_items",
                ),
                (
                    "index",
                    "player_lesson_receipt_time_idx",
                    "player_lesson_selection_receipts",
                ),
                ("table", "player_lesson_intent_receipts", "player_lesson_intent_receipts"),
                (
                    "table",
                    "player_lesson_intent_selection_items",
                    "player_lesson_intent_selection_items",
                ),
                (
                    "table",
                    "player_lesson_intent_applications",
                    "player_lesson_intent_applications",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_intent_receipts_1",
                    "player_lesson_intent_receipts",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_intent_selection_items_1",
                    "player_lesson_intent_selection_items",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_intent_selection_items_2",
                    "player_lesson_intent_selection_items",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_intent_applications_1",
                    "player_lesson_intent_applications",
                ),
                (
                    "index",
                    "sqlite_autoindex_player_lesson_intent_applications_2",
                    "player_lesson_intent_applications",
                ),
                (
                    "index",
                    "player_lesson_intent_receipt_lesson_idx",
                    "player_lesson_intent_selection_items",
                ),
                (
                    "index",
                    "player_lesson_intent_receipt_time_idx",
                    "player_lesson_intent_receipts",
                ),
                (
                    "index",
                    "player_lesson_intent_application_lesson_idx",
                    "player_lesson_intent_applications",
                ),
            }
        )
        expected_lesson_indexes = {
            (1, "pk", 0, _expected_index_xinfo(("lesson_id", 0))),
        }
        expected_receipt_indexes = {
            (1, "pk", 0, _expected_index_xinfo(("branch_id", 0), ("turn_index", 0))),
            (
                0,
                "c",
                0,
                _expected_index_xinfo(("selected_at", 1), ("branch_id", 0), ("turn_index", 0)),
            ),
        }
        expected_item_indexes = {
            (
                1,
                "pk",
                0,
                _expected_index_xinfo(("branch_id", 0), ("turn_index", 0), ("position", 0)),
            ),
            (
                1,
                "u",
                0,
                _expected_index_xinfo(("branch_id", 0), ("turn_index", 0), ("lesson_id", 0)),
            ),
            (
                0,
                "c",
                0,
                _expected_index_xinfo(("lesson_id", 0), ("branch_id", 0), ("turn_index", 0)),
            ),
        }
        expected_intent_receipt_indexes = expected_receipt_indexes
        expected_intent_item_indexes = expected_item_indexes
        expected_intent_application_indexes = expected_item_indexes
        secure_delete = self._connection.execute("PRAGMA secure_delete").fetchone()
        if (
            any(
                _normalized_schema_sql(self._schema_sql("table", name) or "")
                != _normalized_schema_sql(statement)
                for name, statement in zip(
                    (
                        "player_lessons",
                        "player_lesson_selection_receipts",
                        "player_lesson_selection_items",
                    ),
                    _SCHEMA_STATEMENTS[:3],
                    strict=True,
                )
            )
            or _normalized_schema_sql(self._schema_sql("index", "player_lesson_receipt_lesson_idx") or "")
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[3])
            or _normalized_schema_sql(self._schema_sql("index", "player_lesson_receipt_time_idx") or "")
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[4])
            or any(
                _normalized_schema_sql(self._schema_sql("table", name) or "")
                != _normalized_schema_sql(statement)
                for name, statement in zip(
                    (
                        "player_lesson_intent_receipts",
                        "player_lesson_intent_selection_items",
                        "player_lesson_intent_applications",
                    ),
                    _SCHEMA_STATEMENTS[5:8],
                    strict=True,
                )
            )
            or any(
                _normalized_schema_sql(self._schema_sql("index", name) or "")
                != _normalized_schema_sql(statement)
                for name, statement in zip(
                    (
                        "player_lesson_intent_receipt_lesson_idx",
                        "player_lesson_intent_receipt_time_idx",
                        "player_lesson_intent_application_lesson_idx",
                    ),
                    _SCHEMA_STATEMENTS[8:11],
                    strict=True,
                )
            )
            or self._table_columns("player_lessons") != _EXPECTED_LESSON_COLUMNS
            or self._table_columns("player_lesson_selection_receipts") != _EXPECTED_RECEIPT_COLUMNS
            or self._table_columns("player_lesson_selection_items") != _EXPECTED_ITEM_COLUMNS
            or self._table_columns("player_lesson_intent_receipts")
            != _EXPECTED_INTENT_RECEIPT_COLUMNS
            or self._table_columns("player_lesson_intent_selection_items")
            != _EXPECTED_INTENT_ITEM_COLUMNS
            or self._table_columns("player_lesson_intent_applications")
            != _EXPECTED_INTENT_APPLICATION_COLUMNS
            or self._persistent_schema_objects() != expected_objects
            or self._temporary_schema_objects_exist()
            or set(lesson_indexes.values()) != expected_lesson_indexes
            or set(receipt_indexes.values()) != expected_receipt_indexes
            or set(item_indexes.values()) != expected_item_indexes
            or set(intent_receipt_indexes.values()) != expected_intent_receipt_indexes
            or set(intent_item_indexes.values()) != expected_intent_item_indexes
            or set(intent_application_indexes.values()) != expected_intent_application_indexes
            or secure_delete is None
            or int(secure_delete[0]) != 1
        ):
            raise PlayerLessonsError("Player Lessons local storage failed verification")

    @staticmethod
    def _row_values(
        row: sqlite3.Row | tuple[Any, ...] | None,
        columns: tuple[str, ...],
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return {column: row[column] for column in columns}
        if len(row) != len(columns):
            raise PlayerLessonsError("stored Player Lessons row has an invalid shape")
        return dict(zip(columns, row, strict=True))

    @staticmethod
    def _definition_fingerprint(values: Mapping[str, Any]) -> str:
        if values["effect_type"] == "intent_interpretation":
            anchor = _anchor_from_values(values)
            return _sha256_json(
                {
                    "schema": INTENT_LESSON_SCHEMA,
                    "lesson_id": values["lesson_id"],
                    "effect_type": values["effect_type"],
                    "title": values["title"],
                    "scope": values["scope"],
                    "misunderstanding": values["avoid_text"],
                    "correct_interpretation": values["do_text"],
                    "intent_slot": (
                        None if anchor is None else INTENT_LEX_SLOTS.get(anchor["lex_id"])
                    ),
                    "anchor": anchor,
                    "enabled": bool(values["enabled"]),
                    "revision": values["revision"],
                }
            )
        return _sha256_json(
            {
                "schema": LESSON_SCHEMA,
                "lesson_id": values["lesson_id"],
                "effect_type": values["effect_type"],
                "title": values["title"],
                "scope": values["scope"],
                "do": values["do_text"],
                "avoid": values["avoid_text"],
                "anchor": _anchor_from_values(values),
                "enabled": bool(values["enabled"]),
                "revision": values["revision"],
            }
        )

    def _validate_definition(
        self,
        *,
        effect_type: object,
        title: object,
        scope: object,
        do_text: object | None,
        avoid_text: object | None,
        misunderstanding: object | None,
        correct_interpretation: object | None,
    ) -> dict[str, str]:
        if effect_type not in EFFECT_TYPES:
            raise PlayerLessonsValidationError(
                "effect_type must be narration_behavior or intent_interpretation"
            )
        exact_title = _clean_text(
            title,
            field="title",
            maximum=MAX_TITLE_CHARS,
            required=True,
        )
        if not isinstance(scope, str) or scope not in LESSON_SCOPES:
            raise PlayerLessonsValidationError(
                "scope must be every_rpg_turn, exploration, combat_opening, or combat_exchange"
            )
        if effect_type == "narration_behavior":
            if misunderstanding is not None or correct_interpretation is not None:
                raise PlayerLessonsValidationError(
                    "narration_behavior accepts do and avoid, not intent interpretation fields"
                )
            exact_do = _clean_text(
                do_text,
                field="do",
                maximum=MAX_INSTRUCTION_CHARS,
                required=False,
            )
            exact_avoid = _clean_text(
                avoid_text,
                field="avoid",
                maximum=MAX_INSTRUCTION_CHARS,
                required=False,
            )
            if not exact_do and not exact_avoid:
                raise PlayerLessonsValidationError("at least one of do or avoid must be non-empty")
        else:
            if do_text is not None or avoid_text is not None:
                raise PlayerLessonsValidationError(
                    "intent_interpretation accepts misunderstanding and correct_interpretation"
                )
            exact_avoid = _clean_text(
                misunderstanding,
                field="misunderstanding",
                maximum=MAX_INSTRUCTION_CHARS,
                required=True,
            )
            exact_do = _clean_text(
                correct_interpretation,
                field="correct_interpretation",
                maximum=MAX_INSTRUCTION_CHARS,
                required=True,
            )
        return {
            "effect_type": str(effect_type),
            "title": exact_title,
            "scope": scope,
            "do_text": exact_do,
            "avoid_text": exact_avoid,
        }

    @staticmethod
    def _intent_slot(anchor: Mapping[str, str] | None) -> str:
        if anchor is None:
            raise PlayerLessonsValidationError(
                "intent_interpretation requires one current ActionLex or ReferentLex anchor"
            )
        slot = INTENT_LEX_SLOTS.get(anchor.get("lex_id"))
        if slot is None:
            raise PlayerLessonsValidationError(
                "intent_interpretation anchor must use ActionLex or ReferentLex"
            )
        return slot

    def _playerlex_entries(self) -> list[dict[str, Any]] | None:
        if self._playerlex is None:
            return None
        try:
            entries = self._playerlex.list_entries()
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _anchor_from_playerlex_entry(entry: Mapping[str, Any]) -> dict[str, str] | None:
        concept = entry.get("concept")
        lex_id = entry.get("lex_id")
        if (
            entry.get("status") != "current"
            or not isinstance(entry.get("entry_id"), str)
            or lex_id not in LEX_IDS
            or not isinstance(concept, Mapping)
            or concept.get("lex_id") != lex_id
            or not isinstance(concept.get("concept_id"), str)
            or not concept["concept_id"]
            or len(concept["concept_id"]) > 128
            or not isinstance(concept.get("meaning_fingerprint"), str)
            or _FINGERPRINT_RE.fullmatch(concept["meaning_fingerprint"]) is None
        ):
            return None
        return {
            "entry_id": entry["entry_id"],
            "lex_id": str(lex_id),
            "concept_id": concept["concept_id"],
            "meaning_fingerprint": concept["meaning_fingerprint"],
        }

    def _resolve_new_anchor(self, entry_id: object | None) -> dict[str, str] | None:
        if entry_id is None:
            return None
        if not isinstance(entry_id, str) or not entry_id or len(entry_id) > 256:
            raise PlayerLessonsValidationError("anchor_entry_id must identify one current PlayerLex entry")
        entries = self._playerlex_entries()
        if entries is None:
            raise PlayerLessonsValidationError(
                "PlayerLex is unavailable; an anchored lesson cannot be saved or rebound"
            )
        entry = next((item for item in entries if item.get("entry_id") == entry_id), None)
        anchor = self._anchor_from_playerlex_entry(entry or {})
        if anchor is None:
            raise PlayerLessonsValidationError(
                "anchor_entry_id must identify one exact current PlayerLex meaning"
            )
        return anchor

    def _anchor_state(self, anchor: Mapping[str, str] | None) -> tuple[str, str | None]:
        if anchor is None:
            return "unanchored", None
        entries = self._playerlex_entries()
        if entries is None:
            return "unavailable", "playerlex_unavailable"
        entry = next(
            (item for item in entries if item.get("entry_id") == anchor["entry_id"]),
            None,
        )
        if entry is None:
            return "stale", "anchor_entry_removed"
        if entry.get("status") != "current":
            return "stale", "anchor_entry_not_current"
        current = self._anchor_from_playerlex_entry(entry)
        if current != dict(anchor):
            return "stale", "anchor_binding_changed"
        return "current", None

    def _stored_lesson(self, row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        values = self._row_values(row, _LESSON_COLUMNS)
        if values is None:
            return None
        try:
            if (
                not isinstance(values["lesson_id"], str)
                or _LESSON_ID_RE.fullmatch(values["lesson_id"]) is None
                or values["effect_type"] not in EFFECT_TYPES
                or not isinstance(values["title"], str)
                or not 1 <= len(values["title"]) <= MAX_TITLE_CHARS
                or values["scope"] not in LESSON_SCOPES
                or not isinstance(values["do_text"], str)
                or len(values["do_text"]) > MAX_INSTRUCTION_CHARS
                or not isinstance(values["avoid_text"], str)
                or len(values["avoid_text"]) > MAX_INSTRUCTION_CHARS
                or (
                    values["effect_type"] == "narration_behavior"
                    and not values["do_text"]
                    and not values["avoid_text"]
                )
                or (
                    values["effect_type"] == "intent_interpretation"
                    and (not values["do_text"] or not values["avoid_text"])
                )
                or values["enabled"] not in (0, 1)
                or not _is_plain_int(values["revision"], minimum=1)
                or not isinstance(values["fingerprint"], str)
                or _FINGERPRINT_RE.fullmatch(values["fingerprint"]) is None
                or values["approved_via"] != APPROVAL_METHOD
                or not all(
                    _finite_timestamp(values[key]) for key in ("approved_at", "created_at", "updated_at")
                )
            ):
                raise ValueError("lesson fields")
            anchor = _anchor_from_values(values)
            if values["effect_type"] == "intent_interpretation":
                self._intent_slot(anchor)
            if self._definition_fingerprint(values) != values["fingerprint"]:
                raise ValueError("lesson fingerprint")
        except Exception as exc:
            if isinstance(exc, PlayerLessonsError):
                raise
            raise PlayerLessonsError("stored Player Lesson is malformed") from exc
        return values

    def _present(self, values: Mapping[str, Any]) -> dict[str, Any]:
        anchor = _anchor_from_values(values)
        anchor_status, stale_reason = self._anchor_state(anchor)
        enabled = bool(values["enabled"])
        stale = anchor_status in {"stale", "unavailable"}
        effect_fields = (
            {
                "misunderstanding": values["avoid_text"],
                "correct_interpretation": values["do_text"],
                "intent_slot": self._intent_slot(anchor),
            }
            if values["effect_type"] == "intent_interpretation"
            else {"do": values["do_text"], "avoid": values["avoid_text"]}
        )
        result = {
            "schema": (
                INTENT_LESSON_SCHEMA
                if values["effect_type"] == "intent_interpretation"
                else LESSON_SCHEMA
            ),
            "lesson_id": values["lesson_id"],
            "effect_type": values["effect_type"],
            "title": values["title"],
            "scope": values["scope"],
            **effect_fields,
            "enabled": enabled,
            "revision": values["revision"],
            "fingerprint": values["fingerprint"],
            "anchor": anchor,
            "anchor_status": anchor_status,
            "stale_reason": stale_reason,
            "status": "stale" if stale else ("current" if enabled else "disabled"),
            "selectable": enabled and not stale,
            "provenance": {
                "approval": "explicit_local",
                "approved_via": values["approved_via"],
                "approved_at": values["approved_at"],
            },
            "created_at": values["created_at"],
            "updated_at": values["updated_at"],
        }
        return result

    def _lesson_row(self, lesson_id: str) -> sqlite3.Row | tuple[Any, ...] | None:
        return self._connection.execute(
            f"SELECT {', '.join(_LESSON_COLUMNS)} FROM player_lessons WHERE lesson_id=?",
            (lesson_id,),
        ).fetchone()

    def list_lessons(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT {", ".join(_LESSON_COLUMNS)} FROM player_lessons
                ORDER BY updated_at DESC, lesson_id
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                values = self._stored_lesson(row)
                if values is not None:
                    result.append(self._present(values))
        return result

    def create(
        self,
        *,
        effect_type: object,
        title: object,
        scope: object,
        do_text: object | None = None,
        avoid_text: object | None = None,
        misunderstanding: object | None = None,
        correct_interpretation: object | None = None,
        anchor_entry_id: object | None = None,
    ) -> dict[str, Any]:
        definition = self._validate_definition(
            effect_type=effect_type,
            title=title,
            scope=scope,
            do_text=do_text,
            avoid_text=avoid_text,
            misunderstanding=misunderstanding,
            correct_interpretation=correct_interpretation,
        )
        try:
            with self._write_transaction():
                anchor = self._resolve_new_anchor(anchor_entry_id)
                if definition["effect_type"] == "intent_interpretation":
                    self._intent_slot(anchor)
                now = time.time()
                values: dict[str, Any] = {
                    "lesson_id": "lesson_" + uuid.uuid4().hex,
                    **definition,
                    "anchor_entry_id": None if anchor is None else anchor["entry_id"],
                    "anchor_lex_id": None if anchor is None else anchor["lex_id"],
                    "anchor_concept_id": None if anchor is None else anchor["concept_id"],
                    "anchor_meaning_fingerprint": (None if anchor is None else anchor["meaning_fingerprint"]),
                    "enabled": 1,
                    "revision": 1,
                    "fingerprint": "",
                    "approved_via": APPROVAL_METHOD,
                    "approved_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                values["fingerprint"] = self._definition_fingerprint(values)
                count = self._connection.execute("SELECT count(*) FROM player_lessons").fetchone()
                if count is None or int(count[0]) >= MAX_LESSONS:
                    raise PlayerLessonsValidationError(
                        f"at most {MAX_LESSONS} active or disabled Player Lessons may be stored"
                    )
                self._connection.execute(
                    f"""
                    INSERT INTO player_lessons({", ".join(_LESSON_COLUMNS)})
                    VALUES({", ".join("?" for _ in _LESSON_COLUMNS)})
                    """,
                    tuple(values[column] for column in _LESSON_COLUMNS),
                )
        except sqlite3.IntegrityError as exc:
            raise PlayerLessonsValidationError("Player Lesson violates local storage constraints") from exc
        return self._present(values)

    @staticmethod
    def _validate_version(expected_revision: object, expected_fingerprint: object) -> tuple[int, str]:
        if (
            not _is_plain_int(expected_revision, minimum=1)
            or not isinstance(expected_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(expected_fingerprint) is None
        ):
            raise PlayerLessonsValidationError(
                "expected_revision and expected_fingerprint must identify one displayed lesson version"
            )
        return int(expected_revision), expected_fingerprint

    def correct(
        self,
        lesson_id: str,
        *,
        effect_type: object,
        title: object,
        scope: object,
        do_text: object | None = None,
        avoid_text: object | None = None,
        misunderstanding: object | None = None,
        correct_interpretation: object | None = None,
        anchor_entry_id: object | None = None,
        expected_revision: object,
        expected_fingerprint: object,
    ) -> dict[str, Any]:
        if not isinstance(lesson_id, str) or _LESSON_ID_RE.fullmatch(lesson_id) is None:
            raise PlayerLessonsNotFoundError("unknown Player Lesson")
        expected_revision, expected_fingerprint = self._validate_version(
            expected_revision, expected_fingerprint
        )
        try:
            with self._write_transaction():
                current = self._stored_lesson(self._lesson_row(lesson_id))
                if current is None:
                    raise PlayerLessonsNotFoundError(f"unknown Player Lesson: {lesson_id}")
                if effect_type != current["effect_type"]:
                    raise PlayerLessonsValidationError(
                        "a Player Lesson effect_type cannot change during correction"
                    )
                if current["revision"] != expected_revision or current["fingerprint"] != expected_fingerprint:
                    raise PlayerLessonsConflictError(
                        "Player Lesson changed since it was displayed; reload before revising"
                    )
                if expected_revision >= 2**63 - 1:
                    raise PlayerLessonsValidationError("Player Lesson revision limit reached")
                definition = self._validate_definition(
                    effect_type=effect_type,
                    title=title,
                    scope=scope,
                    do_text=do_text,
                    avoid_text=avoid_text,
                    misunderstanding=misunderstanding,
                    correct_interpretation=correct_interpretation,
                )
                anchor = self._resolve_new_anchor(anchor_entry_id)
                if definition["effect_type"] == "intent_interpretation":
                    self._intent_slot(anchor)
                now = time.time()
                values = {
                    **current,
                    **definition,
                    "anchor_entry_id": None if anchor is None else anchor["entry_id"],
                    "anchor_lex_id": None if anchor is None else anchor["lex_id"],
                    "anchor_concept_id": None if anchor is None else anchor["concept_id"],
                    "anchor_meaning_fingerprint": (None if anchor is None else anchor["meaning_fingerprint"]),
                    "revision": expected_revision + 1,
                    "approved_at": now,
                    "updated_at": now,
                }
                values["fingerprint"] = self._definition_fingerprint(values)
                updated = self._connection.execute(
                    f"""
                    UPDATE player_lessons SET
                        {", ".join(f"{column}=?" for column in _LESSON_COLUMNS if column != "lesson_id")}
                    WHERE lesson_id=? AND revision=? AND fingerprint=?
                    """,
                    (
                        *(values[column] for column in _LESSON_COLUMNS if column != "lesson_id"),
                        lesson_id,
                        expected_revision,
                        expected_fingerprint,
                    ),
                )
                if updated.rowcount != 1:
                    raise PlayerLessonsConflictError(
                        "Player Lesson changed since it was displayed; reload before revising"
                    )
        except sqlite3.IntegrityError as exc:
            raise PlayerLessonsValidationError(
                "Player Lesson correction violates local storage constraints"
            ) from exc
        return self._present(values)

    def set_enabled(
        self,
        lesson_id: str,
        *,
        enabled: object,
        expected_revision: object,
        expected_fingerprint: object,
    ) -> dict[str, Any]:
        if not isinstance(lesson_id, str) or _LESSON_ID_RE.fullmatch(lesson_id) is None:
            raise PlayerLessonsNotFoundError("unknown Player Lesson")
        if not isinstance(enabled, bool):
            raise PlayerLessonsValidationError("enabled must be true or false")
        expected_revision, expected_fingerprint = self._validate_version(
            expected_revision, expected_fingerprint
        )
        with self._write_transaction():
            current = self._stored_lesson(self._lesson_row(lesson_id))
            if current is None:
                raise PlayerLessonsNotFoundError(f"unknown Player Lesson: {lesson_id}")
            if current["revision"] != expected_revision or current["fingerprint"] != expected_fingerprint:
                raise PlayerLessonsConflictError(
                    "Player Lesson changed since it was displayed; reload before changing status"
                )
            if bool(current["enabled"]) == enabled:
                raise PlayerLessonsValidationError("Player Lesson already has the requested enabled state")
            if expected_revision >= 2**63 - 1:
                raise PlayerLessonsValidationError("Player Lesson revision limit reached")
            if enabled:
                anchor = _anchor_from_values(current)
                anchor_status, _reason = self._anchor_state(anchor)
                if anchor_status in {"stale", "unavailable"}:
                    raise PlayerLessonsValidationError(
                        "a stale or unavailable anchored lesson must be rebound before enabling"
                    )
            now = time.time()
            values = {
                **current,
                "enabled": int(enabled),
                "revision": expected_revision + 1,
                "approved_at": now,
                "updated_at": now,
            }
            values["fingerprint"] = self._definition_fingerprint(values)
            updated = self._connection.execute(
                """
                UPDATE player_lessons SET enabled=?, revision=?, fingerprint=?, approved_at=?,
                    updated_at=?
                WHERE lesson_id=? AND revision=? AND fingerprint=?
                """,
                (
                    values["enabled"],
                    values["revision"],
                    values["fingerprint"],
                    values["approved_at"],
                    values["updated_at"],
                    lesson_id,
                    expected_revision,
                    expected_fingerprint,
                ),
            )
            if updated.rowcount != 1:
                raise PlayerLessonsConflictError(
                    "Player Lesson changed since it was displayed; reload before changing status"
                )
        return self._present(values)

    def remove(self, lesson_id: str) -> bool:
        """Idempotently delete lesson content and its receipt items, then scrub DB/WAL."""
        if not isinstance(lesson_id, str) or _LESSON_ID_RE.fullmatch(lesson_id) is None:
            raise PlayerLessonsNotFoundError("unknown Player Lesson")
        with self._lock:
            if self._connection.in_transaction:
                raise PlayerLessonsError(
                    "secure Player Lesson removal requires no active transaction; retry after it closes"
                )
            try:
                secure_delete = self._connection.execute("PRAGMA secure_delete=ON").fetchone()
                if secure_delete is None or int(secure_delete[0]) != 1:
                    raise PlayerLessonsError("SQLite secure deletion is unavailable for Player Lessons")
                self._checkpoint_wal()
            except (sqlite3.Error, PlayerLessonsError) as exc:
                raise PlayerLessonsRetryableRemovalError(lesson_id) from exc
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._connection.execute(
                        "DELETE FROM player_lesson_intent_applications WHERE lesson_id=?",
                        (lesson_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM player_lesson_intent_selection_items WHERE lesson_id=?",
                        (lesson_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM player_lesson_selection_items WHERE lesson_id=?",
                        (lesson_id,),
                    )
                    self._connection.execute(
                        "DELETE FROM player_lessons WHERE lesson_id=?",
                        (lesson_id,),
                    )
                except BaseException:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()
            except sqlite3.Error as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise PlayerLessonsRetryableRemovalError(lesson_id) from exc
            try:
                self._checkpoint_wal()
            except (sqlite3.Error, PlayerLessonsError) as exc:
                raise PlayerLessonsRetryableRemovalError(lesson_id) from exc
        return True

    def _checkpoint_wal(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            return
        result = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise PlayerLessonsError(
                "secure Player Lesson removal could not obtain an exclusive WAL checkpoint"
            )

    @staticmethod
    def _scope_matches(scope: str, narration_mode: str) -> bool:
        return scope == "every_rpg_turn" or scope == narration_mode

    @staticmethod
    def _validate_mode(value: object) -> str:
        if not isinstance(value, str) or value not in NARRATION_MODES:
            raise PlayerLessonsValidationError(
                "narration_mode must be exploration, combat_opening, or combat_exchange"
            )
        return value

    def _compile_sample(self, sample_text: str) -> tuple[Any, Any]:
        fabric = load_default_semantic_fabric()

        def safe_overlay(text: str) -> Mapping[str, Any]:
            if self._playerlex is None or not callable(getattr(self._playerlex, "propose", None)):
                return {
                    "schema": "playerlex-recognition-proposal/1",
                    "match_count": 0,
                    "matches": [],
                    "refused": [],
                }
            try:
                proposal = self._playerlex.propose(text)
            except Exception:
                return {
                    "schema": "playerlex-recognition-proposal/1",
                    "match_count": 0,
                    "matches": [],
                    "refused": [],
                }
            return proposal

        return SemanticTurn(sample_text).compile(fabric, recognition_overlay=safe_overlay), fabric

    @staticmethod
    def _compiled_meaning_identities(compiled: Any, fabric: Any) -> frozenset[tuple[str, str, str]]:
        """Project compiled matches to the same exact Atlas identity consumed by selection."""
        noncapability_meanings = {
            (lex_id, entry.concept_id): entry.meaning_fingerprint
            for lex_id in ("referent", "scene", "action")
            for entry in fabric.entries_for(lex_id)
        }
        identities: set[tuple[str, str, str]] = set()
        for match in compiled.matches:
            fingerprint = None
            if isinstance(match.features, Mapping):
                candidate = match.features.get("meaning_fingerprint")
                if isinstance(candidate, str) and _FINGERPRINT_RE.fullmatch(candidate):
                    fingerprint = candidate
            if fingerprint is None and match.surface_baseline == "playerlex":
                candidate = match.entry_fingerprint
                if isinstance(candidate, str) and _FINGERPRINT_RE.fullmatch(candidate):
                    fingerprint = candidate
            if fingerprint is None:
                fingerprint = noncapability_meanings.get((match.lex_id, match.concept_id))
            if isinstance(fingerprint, str) and _FINGERPRINT_RE.fullmatch(fingerprint):
                identities.add((match.lex_id, match.concept_id, fingerprint))
        return frozenset(identities)

    @staticmethod
    def _compiled_exact_recognitions(compiled: Any, fabric: Any) -> tuple[dict[str, Any], ...]:
        """Project exact PlayerLex recognition evidence without retaining source prose."""
        noncapability_meanings = {
            (lex_id, entry.concept_id): entry.meaning_fingerprint
            for lex_id in ("referent", "scene", "action")
            for entry in fabric.entries_for(lex_id)
        }
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for match in compiled.matches:
            fingerprint = None
            if isinstance(match.features, Mapping):
                candidate = match.features.get("meaning_fingerprint")
                if isinstance(candidate, str) and _FINGERPRINT_RE.fullmatch(candidate):
                    fingerprint = candidate
            if fingerprint is None and match.surface_baseline == "playerlex":
                candidate = match.entry_fingerprint
                if isinstance(candidate, str) and _FINGERPRINT_RE.fullmatch(candidate):
                    fingerprint = candidate
            if fingerprint is None:
                fingerprint = noncapability_meanings.get((match.lex_id, match.concept_id))
            approval_ids = sorted(
                str(source_id)
                for source_id in match.source_ids
                if isinstance(source_id, str) and _PLAYERLEX_APPROVAL_RE.fullmatch(source_id)
            )
            if not isinstance(fingerprint, str) or not approval_ids:
                continue
            for approval_source_id in approval_ids:
                key = (
                    match.lex_id,
                    match.concept_id,
                    fingerprint,
                    int(match.start),
                    int(match.end),
                    approval_source_id,
                )
                rows[key] = {
                    "lex_id": str(match.lex_id),
                    "concept_id": str(match.concept_id),
                    "meaning_fingerprint": fingerprint,
                    "source_start": int(match.start),
                    "source_end": int(match.end),
                    "approval_source_id": approval_source_id,
                }
        return tuple(rows[key] for key in sorted(rows))

    def test_draft(
        self,
        *,
        effect_type: object,
        title: object,
        scope: object,
        do_text: object | None = None,
        avoid_text: object | None = None,
        misunderstanding: object | None = None,
        correct_interpretation: object | None = None,
        anchor_entry_id: object | None = None,
        sample_text: object,
        narration_mode: object,
    ) -> dict[str, Any]:
        definition = self._validate_definition(
            effect_type=effect_type,
            title=title,
            scope=scope,
            do_text=do_text,
            avoid_text=avoid_text,
            misunderstanding=misunderstanding,
            correct_interpretation=correct_interpretation,
        )
        mode = self._validate_mode(narration_mode)
        sample = _clean_text(
            sample_text,
            field="sample_text",
            maximum=MAX_SAMPLE_CHARS,
            required=True,
        )
        # Compile even when the anchor is unavailable: this keeps the test path in parity with the
        # production shared Semantic Fabric while the unavailable anchor still fails closed.
        compiled, fabric = self._compile_sample(sample)
        recognized = self._compiled_meaning_identities(compiled, fabric)
        exact_recognitions = self._compiled_exact_recognitions(compiled, fabric)
        anchor: dict[str, str] | None = None
        anchor_status = "unanchored"
        stale_reason: str | None = None
        if anchor_entry_id is not None:
            try:
                anchor = self._resolve_new_anchor(anchor_entry_id)
            except PlayerLessonsValidationError:
                entries = self._playerlex_entries()
                anchor_status = "unavailable" if entries is None else "stale"
                stale_reason = "playerlex_unavailable" if entries is None else "anchor_entry_not_current"
            else:
                anchor_status, stale_reason = self._anchor_state(anchor)

        intent_slot: str | None = None
        if definition["effect_type"] == "intent_interpretation" and anchor is not None:
            intent_slot = self._intent_slot(anchor)
        elif definition["effect_type"] == "intent_interpretation" and anchor_entry_id is None:
            self._intent_slot(None)

        if not self._scope_matches(definition["scope"], mode):
            matched, reason = False, "scope_mismatch"
        elif anchor_entry_id is None:
            matched, reason = True, "scope_match"
        elif anchor_status == "unavailable":
            matched, reason = False, "anchor_unavailable"
        elif anchor_status != "current" or anchor is None:
            matched, reason = False, "anchor_stale"
        elif definition["effect_type"] == "intent_interpretation":
            anchor_hex = str(anchor["entry_id"]).removeprefix("playerlex_")
            matches = [
                row
                for row in exact_recognitions
                if (
                    row["lex_id"],
                    row["concept_id"],
                    row["meaning_fingerprint"],
                )
                == (
                    anchor["lex_id"],
                    anchor["concept_id"],
                    anchor["meaning_fingerprint"],
                )
                and _PLAYERLEX_APPROVAL_RE.fullmatch(row["approval_source_id"])
                and _PLAYERLEX_APPROVAL_RE.fullmatch(row["approval_source_id"]).group(1)
                == anchor_hex
            ]
            if len(matches) == 1:
                matched, reason = True, "scope_and_exact_anchor_recognition"
            elif matches:
                matched, reason = False, "anchor_recognition_ambiguous"
            else:
                matched, reason = False, "anchor_not_recognized"
        elif (
            anchor["lex_id"],
            anchor["concept_id"],
            anchor["meaning_fingerprint"],
        ) in recognized:
            matched, reason = True, "scope_and_anchor_match"
        else:
            matched, reason = False, "anchor_not_recognized"

        result = {
            "schema": (
                INTENT_TEST_SCHEMA
                if definition["effect_type"] == "intent_interpretation"
                else TEST_SCHEMA
            ),
            "matched": matched,
            "reason": reason,
            "scope": definition["scope"],
            "narration_mode": mode,
            "anchor": anchor,
            "anchor_status": anchor_status,
            "stale_reason": stale_reason,
            "sample_stored": False,
        }
        if definition["effect_type"] == "intent_interpretation":
            result.update(
                {
                    "intent_slot": intent_slot,
                    "stage": "post_recognition_pre_contextual_binding",
                    "application_stage": "after recognition, before contextual binding",
                    "test_kind": "retrieval_relevance_only",
                    "application_evaluated": False,
                }
            )
        return result

    @staticmethod
    def _recognized_meaning_set(values: object) -> frozenset[tuple[str, str, str]]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise PlayerLessonsValidationError("recognized_meanings must be an iterable")
        result: set[tuple[str, str, str]] = set()
        for item in values:
            if isinstance(item, Mapping):
                triple = (
                    item.get("lex_id"),
                    item.get("concept_id"),
                    item.get("meaning_fingerprint"),
                )
            elif isinstance(item, (tuple, list)) and len(item) == 3:
                triple = (item[0], item[1], item[2])
            else:
                raise PlayerLessonsValidationError(
                    "recognized meanings must be exact lex_id, concept_id, meaning_fingerprint triples"
                )
            if (
                triple[0] not in LEX_IDS
                or not isinstance(triple[1], str)
                or not triple[1]
                or len(triple[1]) > 128
                or not isinstance(triple[2], str)
                or _FINGERPRINT_RE.fullmatch(triple[2]) is None
            ):
                raise PlayerLessonsValidationError("recognized meaning identity is invalid")
            result.add((str(triple[0]), triple[1], triple[2]))
        return frozenset(result)

    @staticmethod
    def _validate_receipt_identity(
        branch_id: object,
        turn_index: object,
    ) -> tuple[str, int]:
        if not isinstance(branch_id, str) or not branch_id or len(branch_id) > 256:
            raise PlayerLessonsValidationError("branch_id must be non-empty text")
        if not _is_plain_int(turn_index, minimum=0):
            raise PlayerLessonsValidationError("turn_index must be a non-negative integer")
        return branch_id, int(turn_index)

    @staticmethod
    def _validate_input_hash(value: object) -> str:
        if not isinstance(value, str) or _INPUT_HASH_RE.fullmatch(value) is None:
            raise PlayerLessonsValidationError(
                "user_hash must be the 16-character lowercase action digest"
            )
        return value

    def _receipt_row(self, branch_id: str, turn_index: int) -> sqlite3.Row | tuple[Any, ...] | None:
        return self._connection.execute(
            f"""
            SELECT {", ".join(_RECEIPT_COLUMNS)}
            FROM player_lesson_selection_receipts
            WHERE branch_id=? AND turn_index=?
            """,
            (branch_id, turn_index),
        ).fetchone()

    def _receipt_values(self, row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        values = self._row_values(row, _RECEIPT_COLUMNS)
        if values is None:
            return None
        if (
            not isinstance(values["branch_id"], str)
            or not values["branch_id"]
            or not _is_plain_int(values["turn_index"], minimum=0)
            or not isinstance(values["input_hash"], str)
            or _INPUT_HASH_RE.fullmatch(values["input_hash"]) is None
            or values["narration_mode"] not in NARRATION_MODES
            or not _is_plain_int(values["frozen_selected_count"], minimum=0)
            or values["frozen_selected_count"] > MAX_SELECTED_LESSONS
            or (
                values["inherited_from_branch_id"] is not None
                and not isinstance(values["inherited_from_branch_id"], str)
            )
            or not _finite_timestamp(values["selected_at"])
        ):
            raise PlayerLessonsError("stored Player Lesson selection receipt is malformed")
        return values

    def _fork_turn_ceiling(self, parent_branch: str, forked_at: object) -> int | None:
        """Map transcript position to the copied source-turn ceiling used by Store.fork_branch."""
        if not _is_plain_int(forked_at, minimum=0):
            return None
        ordinal_row = self._connection.execute(
            """
            SELECT count(*) FROM branch_msgs
            WHERE branch_id=? AND pos<? AND role IN ('user', 'text')
            """,
            (parent_branch, int(forked_at)),
        ).fetchone()
        if ordinal_row is None:
            return None
        ordinal = int(ordinal_row[0])
        if ordinal <= 0:
            return -1
        turn_row = self._connection.execute(
            """
            SELECT turn_index FROM turns WHERE branch_id=?
            ORDER BY turn_index LIMIT 1 OFFSET ?
            """,
            (parent_branch, ordinal - 1),
        ).fetchone()
        return None if turn_row is None else int(turn_row[0])

    def _ancestor_receipt_branch(self, branch_id: str, turn_index: int) -> str | None:
        current = branch_id
        visited = {current}
        while True:
            branch = self._connection.execute(
                "SELECT parent_branch, forked_at FROM branches WHERE branch_id=?",
                (current,),
            ).fetchone()
            if branch is None:
                return None
            parent = branch[0]
            if not isinstance(parent, str) or not parent or parent in visited:
                return None
            ceiling = self._fork_turn_ceiling(parent, branch[1])
            if ceiling is None or turn_index > ceiling:
                return None
            if self._receipt_row(parent, turn_index) is not None:
                return parent
            visited.add(parent)
            current = parent

    def _clone_receipt(self, source_branch: str, branch_id: str, turn_index: int) -> None:
        source = self._receipt_values(self._receipt_row(source_branch, turn_index))
        if source is None:
            raise PlayerLessonsError("ancestor Player Lesson receipt disappeared during replay")
        origin = source["inherited_from_branch_id"] or source_branch
        now = time.time()
        self._connection.execute(
            """
            INSERT INTO player_lesson_selection_receipts(
                branch_id, turn_index, input_hash, narration_mode, frozen_selected_count,
                inherited_from_branch_id, selected_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                branch_id,
                turn_index,
                source["input_hash"],
                source["narration_mode"],
                source["frozen_selected_count"],
                origin,
                now,
            ),
        )
        rows = self._connection.execute(
            f"""
            SELECT {", ".join(_ITEM_COLUMNS)} FROM player_lesson_selection_items
            WHERE branch_id=? AND turn_index=? ORDER BY position
            """,
            (source_branch, turn_index),
        ).fetchall()
        for row in rows:
            item = self._row_values(row, _ITEM_COLUMNS)
            if item is None:
                continue
            item.update(
                {
                    "branch_id": branch_id,
                    "turn_index": turn_index,
                    "delivered": 0,
                    "delivered_at": None,
                }
            )
            self._connection.execute(
                f"""
                INSERT INTO player_lesson_selection_items({", ".join(_ITEM_COLUMNS)})
                VALUES({", ".join("?" for _ in _ITEM_COLUMNS)})
                """,
                tuple(item[column] for column in _ITEM_COLUMNS),
            )

    def _ensure_receipt(
        self,
        branch_id: str,
        turn_index: int,
        *,
        input_hash: str | None = None,
        narration_mode: str | None = None,
    ) -> dict[str, Any] | None:
        receipt = self._receipt_values(self._receipt_row(branch_id, turn_index))
        if receipt is None:
            ancestor = self._ancestor_receipt_branch(branch_id, turn_index)
            if ancestor is not None:
                self._clone_receipt(ancestor, branch_id, turn_index)
                receipt = self._receipt_values(self._receipt_row(branch_id, turn_index))
        if (
            receipt is not None
            and input_hash is not None
            and (receipt["input_hash"] != input_hash or receipt["narration_mode"] != narration_mode)
        ):
            raise PlayerLessonsConflictError(
                "Player Lesson receipt is frozen for a different input hash or narration mode"
            )
        return receipt

    def select(
        self,
        *,
        branch_id: object,
        turn_index: object,
        user_hash: object,
        narration_mode: object,
        recognized_meanings: object,
    ) -> dict[str, Any]:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        input_hash = self._validate_input_hash(user_hash)
        mode = self._validate_mode(narration_mode)
        recognized = self._recognized_meaning_set(recognized_meanings)

        try:
            with self._write_transaction():
                receipt = self._ensure_receipt(
                    branch_id,
                    turn_index,
                    input_hash=input_hash,
                    narration_mode=mode,
                )
                if receipt is None:
                    rows = self._connection.execute(
                        f"""
                        SELECT {", ".join(_LESSON_COLUMNS)} FROM player_lessons
                        WHERE enabled=1 AND effect_type='narration_behavior'
                        ORDER BY updated_at DESC, lesson_id
                        """
                    ).fetchall()
                    selected: list[tuple[dict[str, Any], str, dict[str, str] | None]] = []
                    for row in rows:
                        values = self._stored_lesson(row)
                        if values is None or not self._scope_matches(values["scope"], mode):
                            continue
                        anchor = _anchor_from_values(values)
                        anchor_status, _stale_reason = self._anchor_state(anchor)
                        if anchor_status in {"stale", "unavailable"}:
                            continue
                        if (
                            anchor is not None
                            and (
                                anchor["lex_id"],
                                anchor["concept_id"],
                                anchor["meaning_fingerprint"],
                            )
                            not in recognized
                        ):
                            continue
                        reason = "scope_match" if anchor is None else "scope_and_anchor_match"
                        selected.append((values, reason, anchor))
                        if len(selected) == MAX_SELECTED_LESSONS:
                            break

                    selected_at = time.time()
                    self._connection.execute(
                        """
                        INSERT INTO player_lesson_selection_receipts(
                            branch_id, turn_index, input_hash, narration_mode,
                            frozen_selected_count, inherited_from_branch_id, selected_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            branch_id,
                            turn_index,
                            input_hash,
                            mode,
                            len(selected),
                            None,
                            selected_at,
                        ),
                    )
                    for position, (values, reason, anchor) in enumerate(selected):
                        item = {
                            "branch_id": branch_id,
                            "turn_index": turn_index,
                            "position": position,
                            "lesson_id": values["lesson_id"],
                            "lesson_revision": values["revision"],
                            "lesson_fingerprint": values["fingerprint"],
                            "reason": reason,
                            "scope": values["scope"],
                            "anchor_entry_id": None if anchor is None else anchor["entry_id"],
                            "anchor_lex_id": None if anchor is None else anchor["lex_id"],
                            "anchor_concept_id": None if anchor is None else anchor["concept_id"],
                            "anchor_meaning_fingerprint": (
                                None if anchor is None else anchor["meaning_fingerprint"]
                            ),
                            "delivered": 0,
                            "delivered_at": None,
                        }
                        self._connection.execute(
                            f"""
                            INSERT INTO player_lesson_selection_items({", ".join(_ITEM_COLUMNS)})
                            VALUES({", ".join("?" for _ in _ITEM_COLUMNS)})
                            """,
                            tuple(item[column] for column in _ITEM_COLUMNS),
                        )
        except sqlite3.IntegrityError as exc:
            raise PlayerLessonsConflictError(
                "Player Lesson selection raced another frozen receipt; retry rehydration"
            ) from exc
        result = self._rehydrate_direct(branch_id, turn_index)
        if result is None:
            raise PlayerLessonsError("Player Lesson selection receipt was not persisted")
        return result

    def _item_values(self, row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        values = self._row_values(row, _ITEM_COLUMNS)
        if values is None:
            return None
        try:
            anchor = _anchor_from_values(values)
            if (
                not isinstance(values["branch_id"], str)
                or not _is_plain_int(values["turn_index"], minimum=0)
                or not _is_plain_int(values["position"], minimum=0)
                or values["position"] >= MAX_SELECTED_LESSONS
                or not isinstance(values["lesson_id"], str)
                or _LESSON_ID_RE.fullmatch(values["lesson_id"]) is None
                or not _is_plain_int(values["lesson_revision"], minimum=1)
                or not isinstance(values["lesson_fingerprint"], str)
                or _FINGERPRINT_RE.fullmatch(values["lesson_fingerprint"]) is None
                or values["reason"] not in {"scope_match", "scope_and_anchor_match"}
                or values["scope"] not in LESSON_SCOPES
                or values["delivered"] not in (0, 1)
                or (values["delivered"] == 0 and values["delivered_at"] is not None)
                or (values["delivered"] == 1 and not _finite_timestamp(values["delivered_at"]))
                or (anchor is None) != (values["reason"] == "scope_match")
            ):
                raise ValueError("selection item")
        except Exception as exc:
            if isinstance(exc, PlayerLessonsError):
                raise
            raise PlayerLessonsError("stored Player Lesson selection item is malformed") from exc
        return values

    def _rehydrate_direct(self, branch_id: str, turn_index: int) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._receipt_values(self._receipt_row(branch_id, turn_index))
            if receipt is None:
                return None
            item_rows = self._connection.execute(
                f"""
                SELECT {", ".join(_ITEM_COLUMNS)} FROM player_lesson_selection_items
                WHERE branch_id=? AND turn_index=? ORDER BY position
                """,
                (branch_id, turn_index),
            ).fetchall()
            selected: list[dict[str, Any]] = []
            for item_row in item_rows:
                item = self._item_values(item_row)
                if item is None:
                    continue
                lesson = self._stored_lesson(self._lesson_row(item["lesson_id"]))
                if lesson is None:
                    continue
                anchor = _anchor_from_values(lesson)
                item_anchor = _anchor_from_values(item)
                anchor_status, _stale_reason = self._anchor_state(anchor)
                if (
                    lesson["revision"] != item["lesson_revision"]
                    or lesson["fingerprint"] != item["lesson_fingerprint"]
                    or not bool(lesson["enabled"])
                    or lesson["scope"] != item["scope"]
                    or anchor != item_anchor
                    or anchor_status in {"stale", "unavailable"}
                ):
                    continue
                selected.append(
                    {
                        "position": item["position"],
                        "lesson_id": lesson["lesson_id"],
                        "lesson_revision": lesson["revision"],
                        "lesson_fingerprint": lesson["fingerprint"],
                        "effect_type": lesson["effect_type"],
                        "title": lesson["title"],
                        "do": lesson["do_text"],
                        "avoid": lesson["avoid_text"],
                        "reason": item["reason"],
                        "scope": item["scope"],
                        "anchor": anchor,
                        "delivered": bool(item["delivered"]),
                    }
                )
        return {
            "schema": SELECTION_SCHEMA,
            "branch_id": receipt["branch_id"],
            "turn_index": receipt["turn_index"],
            "input_hash": receipt["input_hash"],
            "narration_mode": receipt["narration_mode"],
            "frozen_selected_count": receipt["frozen_selected_count"],
            "available_count": len(selected),
            "omitted_count": receipt["frozen_selected_count"] - len(selected),
            "inherited_from_branch_id": receipt["inherited_from_branch_id"],
            "selected_at": receipt["selected_at"],
            "selected": selected,
        }

    def rehydrate(self, branch_id: object, turn_index: object) -> dict[str, Any] | None:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        with self._write_transaction():
            receipt = self._ensure_receipt(branch_id, turn_index)
        if receipt is None:
            return None
        return self._rehydrate_direct(branch_id, turn_index)

    def mark_delivered(
        self,
        branch_id: object,
        turn_index: object,
        lesson_ids: object,
    ) -> dict[str, Any]:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        if isinstance(lesson_ids, (str, bytes)) or not isinstance(lesson_ids, Iterable):
            raise PlayerLessonsValidationError("lesson_ids must be an iterable")
        ids = tuple(dict.fromkeys(lesson_ids))
        if len(ids) > MAX_SELECTED_LESSONS or any(
            not isinstance(item, str) or _LESSON_ID_RE.fullmatch(item) is None for item in ids
        ):
            raise PlayerLessonsValidationError("lesson_ids must identify at most five selected lessons")
        with self._write_transaction():
            receipt = self._ensure_receipt(branch_id, turn_index)
            if receipt is None:
                raise PlayerLessonsNotFoundError(
                    "no frozen Player Lesson receipt exists for this branch and turn"
                )
            rows = self._connection.execute(
                """
                SELECT lesson_id FROM player_lesson_selection_items
                WHERE branch_id=? AND turn_index=?
                """,
                (branch_id, turn_index),
            ).fetchall()
            known = {str(row[0]) for row in rows}
            if not set(ids).issubset(known):
                raise PlayerLessonsConflictError(
                    "delivery cannot mark a lesson absent from the frozen selection"
                )
            now = time.time()
            for lesson_id in ids:
                self._connection.execute(
                    """
                    UPDATE player_lesson_selection_items
                    SET delivered=1, delivered_at=?
                    WHERE branch_id=? AND turn_index=? AND lesson_id=? AND delivered=0
                    """,
                    (now, branch_id, turn_index, lesson_id),
                )
        result = self._rehydrate_direct(branch_id, turn_index)
        if result is None:
            raise PlayerLessonsError("Player Lesson delivery receipt disappeared")
        return result

    def latest_selections(self, session_id: object | None = None) -> list[dict[str, Any]]:
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id or len(session_id) > 256
        ):
            raise PlayerLessonsValidationError("session_id must be non-empty bounded text")
        where = ""
        parameters: tuple[Any, ...] = ()
        if session_id is not None:
            where = (
                "WHERE EXISTS (SELECT 1 FROM branches AS b WHERE b.branch_id=r.branch_id AND b.session_id=?)"
            )
            parameters = (session_id,)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    {", ".join(f"i.{column}" for column in _ITEM_COLUMNS)},
                    r.input_hash, r.narration_mode, r.selected_at
                FROM player_lesson_selection_items AS i
                JOIN player_lesson_selection_receipts AS r
                  ON r.branch_id=i.branch_id AND r.turn_index=i.turn_index
                {where}
                ORDER BY r.selected_at DESC, r.branch_id, r.turn_index DESC, i.position
                """,
                parameters,
            ).fetchall()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        columns = (*_ITEM_COLUMNS, "input_hash", "narration_mode", "selected_at")
        for row in rows:
            values = self._row_values(row, columns)
            if values is None or values["lesson_id"] in seen:
                continue
            item = self._item_values(tuple(values[column] for column in _ITEM_COLUMNS))
            if item is None:
                continue
            seen.add(item["lesson_id"])
            result.append(
                {
                    "schema": SELECTION_ITEM_SCHEMA,
                    "branch_id": item["branch_id"],
                    "turn_index": item["turn_index"],
                    "input_hash": values["input_hash"],
                    "narration_mode": values["narration_mode"],
                    "selected_at": values["selected_at"],
                    "lesson_id": item["lesson_id"],
                    "lesson_revision": item["lesson_revision"],
                    "lesson_fingerprint": item["lesson_fingerprint"],
                    "reason": item["reason"],
                    "scope": item["scope"],
                    "anchor": _anchor_from_values(item),
                    "delivered": bool(item["delivered"]),
                }
            )
        return result

    @staticmethod
    def _recognized_intent_meanings(values: object) -> tuple[dict[str, Any], ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            raise PlayerLessonsValidationError("recognized_meanings must be an iterable")
        expected = {
            "lex_id",
            "concept_id",
            "meaning_fingerprint",
            "source_start",
            "source_end",
            "approval_source_id",
        }
        rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in values:
            if not isinstance(item, Mapping) or set(item) != expected:
                raise PlayerLessonsValidationError(
                    "intent recognized meanings must contain the exact v1 evidence fields"
                )
            lex_id = item.get("lex_id")
            concept_id = item.get("concept_id")
            meaning_fingerprint = item.get("meaning_fingerprint")
            source_start, source_end = item.get("source_start"), item.get("source_end")
            approval_source_id = item.get("approval_source_id")
            if (
                lex_id not in LEX_IDS
                or not isinstance(concept_id, str)
                or not concept_id
                or len(concept_id) > 128
                or not isinstance(meaning_fingerprint, str)
                or _FINGERPRINT_RE.fullmatch(meaning_fingerprint) is None
                or not _is_plain_int(source_start, minimum=0)
                or not _is_plain_int(source_end, minimum=1)
                or int(source_end) <= int(source_start)
                or not isinstance(approval_source_id, str)
                or _PLAYERLEX_APPROVAL_RE.fullmatch(approval_source_id) is None
            ):
                raise PlayerLessonsValidationError("intent recognized meaning evidence is invalid")
            key = (
                int(source_start),
                int(source_end),
                str(lex_id),
                concept_id,
                meaning_fingerprint,
                approval_source_id,
            )
            rows[key] = {
                "lex_id": str(lex_id),
                "concept_id": concept_id,
                "meaning_fingerprint": meaning_fingerprint,
                "source_start": int(source_start),
                "source_end": int(source_end),
                "approval_source_id": approval_source_id,
            }
        return tuple(rows[key] for key in sorted(rows))

    def _intent_receipt_row(
        self, branch_id: str, turn_index: int
    ) -> sqlite3.Row | tuple[Any, ...] | None:
        return self._connection.execute(
            f"""
            SELECT {", ".join(_INTENT_RECEIPT_COLUMNS)}
            FROM player_lesson_intent_receipts
            WHERE branch_id=? AND turn_index=?
            """,
            (branch_id, turn_index),
        ).fetchone()

    def _intent_receipt_values(
        self, row: sqlite3.Row | tuple[Any, ...] | None
    ) -> dict[str, Any] | None:
        return self._receipt_values(row)

    def _intent_item_values(
        self, row: sqlite3.Row | tuple[Any, ...] | None
    ) -> dict[str, Any] | None:
        values = self._row_values(row, _INTENT_ITEM_COLUMNS)
        if values is None:
            return None
        try:
            anchor = _anchor_from_values(values)
            approval = _PLAYERLEX_APPROVAL_RE.fullmatch(str(values["approval_source_id"]))
            entry_hex = str(values["anchor_entry_id"]).removeprefix("playerlex_")
            if (
                not isinstance(values["branch_id"], str)
                or not values["branch_id"]
                or not _is_plain_int(values["turn_index"], minimum=0)
                or not _is_plain_int(values["position"], minimum=0)
                or values["position"] >= MAX_SELECTED_LESSONS
                or not isinstance(values["lesson_id"], str)
                or _LESSON_ID_RE.fullmatch(values["lesson_id"]) is None
                or not _is_plain_int(values["lesson_revision"], minimum=1)
                or not isinstance(values["lesson_fingerprint"], str)
                or _FINGERPRINT_RE.fullmatch(values["lesson_fingerprint"]) is None
                or values["reason"] != "scope_and_exact_anchor_recognition"
                or values["scope"] not in LESSON_SCOPES
                or anchor is None
                or values["intent_slot"] != INTENT_LEX_SLOTS.get(anchor["lex_id"])
                or not _is_plain_int(values["source_start"], minimum=0)
                or not _is_plain_int(values["source_end"], minimum=1)
                or values["source_end"] <= values["source_start"]
                or approval is None
                or approval.group(1) != entry_hex
            ):
                raise ValueError("intent selection item")
        except Exception as exc:
            if isinstance(exc, PlayerLessonsError):
                raise
            raise PlayerLessonsError("stored intent selection item is malformed") from exc
        return values

    def _intent_application_values(
        self, row: sqlite3.Row | tuple[Any, ...] | None
    ) -> dict[str, Any] | None:
        values = self._row_values(row, _INTENT_APPLICATION_COLUMNS)
        if values is None:
            return None
        applied = values.get("applied")
        reason = values.get("reason")
        binding_values = (
            values.get("selected_value"),
            values.get("meaning_binding_ref"),
            values.get("frame_fingerprint"),
        )
        if (
            not isinstance(values["branch_id"], str)
            or not values["branch_id"]
            or not _is_plain_int(values["turn_index"], minimum=0)
            or not _is_plain_int(values["position"], minimum=0)
            or values["position"] >= MAX_SELECTED_LESSONS
            or not isinstance(values["lesson_id"], str)
            or _LESSON_ID_RE.fullmatch(values["lesson_id"]) is None
            or not _is_plain_int(values["lesson_revision"], minimum=1)
            or not isinstance(values["lesson_fingerprint"], str)
            or _FINGERPRINT_RE.fullmatch(values["lesson_fingerprint"]) is None
            or applied not in (0, 1)
            or reason not in INTENT_APPLICATION_REASONS
            or not _finite_timestamp(values["applied_at"])
            or (
                applied == 1
                and (
                    reason not in INTENT_APPLIED_REASONS
                    or not all(
                        isinstance(value, str) and value
                        for value in (values.get("frame_id"), *binding_values)
                    )
                    or _STABLE_VALUE_RE.fullmatch(str(values["frame_id"])) is None
                    or _STABLE_VALUE_RE.fullmatch(str(values["selected_value"])) is None
                    or _FINGERPRINT_RE.fullmatch(str(values["meaning_binding_ref"])) is None
                    or _FINGERPRINT_RE.fullmatch(str(values["frame_fingerprint"])) is None
                )
            )
            or (
                applied == 0
                and (
                    reason in INTENT_APPLIED_REASONS
                    or any(value is not None for value in binding_values)
                    or (
                        values.get("frame_id") is not None
                        and (
                            not isinstance(values["frame_id"], str)
                            or _STABLE_VALUE_RE.fullmatch(values["frame_id"]) is None
                        )
                    )
                )
            )
        ):
            raise PlayerLessonsError("stored intent application record is malformed")
        return values

    def _intent_ancestor_receipt_branch(self, branch_id: str, turn_index: int) -> str | None:
        current = branch_id
        visited = {current}
        while True:
            branch = self._connection.execute(
                "SELECT parent_branch, forked_at FROM branches WHERE branch_id=?",
                (current,),
            ).fetchone()
            if branch is None:
                return None
            parent = branch[0]
            if not isinstance(parent, str) or not parent or parent in visited:
                return None
            ceiling = self._fork_turn_ceiling(parent, branch[1])
            if ceiling is None or turn_index > ceiling:
                return None
            if self._intent_receipt_row(parent, turn_index) is not None:
                return parent
            visited.add(parent)
            current = parent

    def _clone_intent_receipt(self, source_branch: str, branch_id: str, turn_index: int) -> None:
        source = self._intent_receipt_values(self._intent_receipt_row(source_branch, turn_index))
        if source is None:
            raise PlayerLessonsError("ancestor intent receipt disappeared during replay")
        origin = source["inherited_from_branch_id"] or source_branch
        self._connection.execute(
            """
            INSERT INTO player_lesson_intent_receipts(
                branch_id, turn_index, input_hash, narration_mode, frozen_selected_count,
                inherited_from_branch_id, selected_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                branch_id,
                turn_index,
                source["input_hash"],
                source["narration_mode"],
                source["frozen_selected_count"],
                origin,
                time.time(),
            ),
        )
        item_rows = self._connection.execute(
            f"""
            SELECT {", ".join(_INTENT_ITEM_COLUMNS)}
            FROM player_lesson_intent_selection_items
            WHERE branch_id=? AND turn_index=? ORDER BY position
            """,
            (source_branch, turn_index),
        ).fetchall()
        for row in item_rows:
            item = self._intent_item_values(row)
            if item is None:
                continue
            item = {**item, "branch_id": branch_id}
            self._connection.execute(
                f"""
                INSERT INTO player_lesson_intent_selection_items(
                    {", ".join(_INTENT_ITEM_COLUMNS)}
                ) VALUES({", ".join("?" for _ in _INTENT_ITEM_COLUMNS)})
                """,
                tuple(item[column] for column in _INTENT_ITEM_COLUMNS),
            )
        application_rows = self._connection.execute(
            f"""
            SELECT {", ".join(_INTENT_APPLICATION_COLUMNS)}
            FROM player_lesson_intent_applications
            WHERE branch_id=? AND turn_index=? ORDER BY position
            """,
            (source_branch, turn_index),
        ).fetchall()
        for row in application_rows:
            application = self._intent_application_values(row)
            if application is None:
                continue
            application = {**application, "branch_id": branch_id}
            self._connection.execute(
                f"""
                INSERT INTO player_lesson_intent_applications(
                    {", ".join(_INTENT_APPLICATION_COLUMNS)}
                ) VALUES({", ".join("?" for _ in _INTENT_APPLICATION_COLUMNS)})
                """,
                tuple(application[column] for column in _INTENT_APPLICATION_COLUMNS),
            )

    def _ensure_intent_receipt(
        self,
        branch_id: str,
        turn_index: int,
        *,
        input_hash: str | None = None,
        narration_mode: str | None = None,
    ) -> dict[str, Any] | None:
        receipt = self._intent_receipt_values(self._intent_receipt_row(branch_id, turn_index))
        if receipt is None:
            ancestor = self._intent_ancestor_receipt_branch(branch_id, turn_index)
            if ancestor is not None:
                self._clone_intent_receipt(ancestor, branch_id, turn_index)
                receipt = self._intent_receipt_values(
                    self._intent_receipt_row(branch_id, turn_index)
                )
        if (
            receipt is not None
            and input_hash is not None
            and (receipt["input_hash"] != input_hash or receipt["narration_mode"] != narration_mode)
        ):
            raise PlayerLessonsConflictError(
                "intent receipt is frozen for a different input hash or narration mode"
            )
        return receipt

    def select_intent(
        self,
        *,
        branch_id: object,
        turn_index: object,
        user_hash: object,
        narration_mode: object,
        recognized_meanings: object,
    ) -> dict[str, Any]:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        input_hash = self._validate_input_hash(user_hash)
        mode = self._validate_mode(narration_mode)
        recognized = self._recognized_intent_meanings(recognized_meanings)
        try:
            with self._write_transaction():
                receipt = self._ensure_intent_receipt(
                    branch_id,
                    turn_index,
                    input_hash=input_hash,
                    narration_mode=mode,
                )
                if receipt is None:
                    rows = self._connection.execute(
                        f"""
                        SELECT {", ".join(_LESSON_COLUMNS)} FROM player_lessons
                        WHERE enabled=1 AND effect_type='intent_interpretation'
                        ORDER BY updated_at DESC, lesson_id
                        """
                    ).fetchall()
                    selected: list[tuple[dict[str, Any], dict[str, str], dict[str, Any]]] = []
                    for row in rows:
                        values = self._stored_lesson(row)
                        if values is None or not self._scope_matches(values["scope"], mode):
                            continue
                        anchor = _anchor_from_values(values)
                        anchor_status, _stale_reason = self._anchor_state(anchor)
                        if anchor is None or anchor_status != "current":
                            continue
                        entry_hex = anchor["entry_id"].removeprefix("playerlex_")
                        matches = [
                            candidate
                            for candidate in recognized
                            if (
                                candidate["lex_id"],
                                candidate["concept_id"],
                                candidate["meaning_fingerprint"],
                            )
                            == (
                                anchor["lex_id"],
                                anchor["concept_id"],
                                anchor["meaning_fingerprint"],
                            )
                            and _PLAYERLEX_APPROVAL_RE.fullmatch(
                                candidate["approval_source_id"]
                            ).group(1)
                            == entry_hex
                        ]
                        if len(matches) != 1:
                            continue
                        selected.append((values, anchor, matches[0]))
                        if len(selected) == MAX_SELECTED_LESSONS:
                            break
                    selected_at = time.time()
                    self._connection.execute(
                        """
                        INSERT INTO player_lesson_intent_receipts(
                            branch_id, turn_index, input_hash, narration_mode,
                            frozen_selected_count, inherited_from_branch_id, selected_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (branch_id, turn_index, input_hash, mode, len(selected), None, selected_at),
                    )
                    for position, (values, anchor, recognition) in enumerate(selected):
                        item = {
                            "branch_id": branch_id,
                            "turn_index": turn_index,
                            "position": position,
                            "lesson_id": values["lesson_id"],
                            "lesson_revision": values["revision"],
                            "lesson_fingerprint": values["fingerprint"],
                            "reason": "scope_and_exact_anchor_recognition",
                            "scope": values["scope"],
                            "anchor_entry_id": anchor["entry_id"],
                            "anchor_lex_id": anchor["lex_id"],
                            "anchor_concept_id": anchor["concept_id"],
                            "anchor_meaning_fingerprint": anchor["meaning_fingerprint"],
                            "intent_slot": self._intent_slot(anchor),
                            "source_start": recognition["source_start"],
                            "source_end": recognition["source_end"],
                            "approval_source_id": recognition["approval_source_id"],
                        }
                        self._connection.execute(
                            f"""
                            INSERT INTO player_lesson_intent_selection_items(
                                {", ".join(_INTENT_ITEM_COLUMNS)}
                            ) VALUES({", ".join("?" for _ in _INTENT_ITEM_COLUMNS)})
                            """,
                            tuple(item[column] for column in _INTENT_ITEM_COLUMNS),
                        )
        except sqlite3.IntegrityError as exc:
            raise PlayerLessonsConflictError(
                "intent selection raced another frozen receipt; retry rehydration"
            ) from exc
        result = self._rehydrate_intent_direct(branch_id, turn_index)
        if result is None:
            raise PlayerLessonsError("intent selection receipt was not persisted")
        return result

    def _rehydrate_intent_direct(
        self, branch_id: str, turn_index: int
    ) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._intent_receipt_values(self._intent_receipt_row(branch_id, turn_index))
            if receipt is None:
                return None
            item_rows = self._connection.execute(
                f"""
                SELECT {", ".join(_INTENT_ITEM_COLUMNS)}
                FROM player_lesson_intent_selection_items
                WHERE branch_id=? AND turn_index=? ORDER BY position
                """,
                (branch_id, turn_index),
            ).fetchall()
            application_rows = self._connection.execute(
                f"""
                SELECT {", ".join(_INTENT_APPLICATION_COLUMNS)}
                FROM player_lesson_intent_applications
                WHERE branch_id=? AND turn_index=?
                """,
                (branch_id, turn_index),
            ).fetchall()
            applications = {
                values["position"]: values
                for row in application_rows
                if (values := self._intent_application_values(row)) is not None
            }
            selected: list[dict[str, Any]] = []
            for row in item_rows:
                item = self._intent_item_values(row)
                if item is None:
                    continue
                lesson = self._stored_lesson(self._lesson_row(item["lesson_id"]))
                if lesson is None:
                    continue
                anchor = _anchor_from_values(lesson)
                item_anchor = _anchor_from_values(item)
                anchor_status, _stale_reason = self._anchor_state(anchor)
                if (
                    lesson["effect_type"] != "intent_interpretation"
                    or lesson["revision"] != item["lesson_revision"]
                    or lesson["fingerprint"] != item["lesson_fingerprint"]
                    or not bool(lesson["enabled"])
                    or lesson["scope"] != item["scope"]
                    or anchor != item_anchor
                    or anchor_status != "current"
                ):
                    continue
                application = applications.get(item["position"])
                selection = {
                    "schema": INTENT_SELECTION_ITEM_SCHEMA,
                    "position": item["position"],
                    "lesson_id": item["lesson_id"],
                    "lesson_revision": item["lesson_revision"],
                    "lesson_fingerprint": item["lesson_fingerprint"],
                    "reason": item["reason"],
                    "scope": item["scope"],
                    "anchor": item_anchor,
                    "intent_slot": item["intent_slot"],
                    "source_span": {
                        "start": item["source_start"],
                        "end": item["source_end"],
                    },
                    "approval_source_id": item["approval_source_id"],
                    "applied": None if application is None else bool(application["applied"]),
                    "application_reason": None if application is None else application["reason"],
                }
                if application is not None:
                    selection.update(
                        {
                            "frame_id": application["frame_id"],
                            "selected_value": application["selected_value"],
                            "meaning_binding_ref": application["meaning_binding_ref"],
                            "frame_fingerprint": application["frame_fingerprint"],
                            "applied_at": application["applied_at"],
                        }
                    )
                selected.append(selection)
        return {
            "schema": INTENT_SELECTION_SCHEMA,
            "branch_id": receipt["branch_id"],
            "turn_index": receipt["turn_index"],
            "input_hash": receipt["input_hash"],
            "narration_mode": receipt["narration_mode"],
            "frozen_selected_count": receipt["frozen_selected_count"],
            "available_count": len(selected),
            "omitted_count": receipt["frozen_selected_count"] - len(selected),
            "inherited_from_branch_id": receipt["inherited_from_branch_id"],
            "selected_at": receipt["selected_at"],
            "selected": selected,
        }

    def rehydrate_intent(
        self, branch_id: object, turn_index: object
    ) -> dict[str, Any] | None:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        with self._write_transaction():
            receipt = self._ensure_intent_receipt(branch_id, turn_index)
        if receipt is None:
            return None
        return self._rehydrate_intent_direct(branch_id, turn_index)

    @staticmethod
    def _validate_intent_application_request(value: object) -> dict[str, Any]:
        required = {
            "lesson_id",
            "lesson_revision",
            "lesson_fingerprint",
            "applied",
            "reason",
        }
        optional = {"frame_id", "selected_value", "meaning_binding_ref", "frame_fingerprint"}
        if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - required - optional:
            raise PlayerLessonsValidationError("intent application fields do not match v1")
        lesson_id = value.get("lesson_id")
        lesson_revision = value.get("lesson_revision")
        lesson_fingerprint = value.get("lesson_fingerprint")
        applied = value.get("applied")
        reason = value.get("reason")
        option_values = {key: value.get(key) for key in optional}
        if (
            not isinstance(lesson_id, str)
            or _LESSON_ID_RE.fullmatch(lesson_id) is None
            or not _is_plain_int(lesson_revision, minimum=1)
            or not isinstance(lesson_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(lesson_fingerprint) is None
            or not isinstance(applied, bool)
            or reason not in INTENT_APPLICATION_REASONS
        ):
            raise PlayerLessonsValidationError("intent application identity or result is invalid")
        if applied:
            if reason not in INTENT_APPLIED_REASONS:
                raise PlayerLessonsValidationError("applied intent result has a non-application reason")
            if (
                not isinstance(option_values["frame_id"], str)
                or _STABLE_VALUE_RE.fullmatch(option_values["frame_id"]) is None
                or not isinstance(option_values["selected_value"], str)
                or _STABLE_VALUE_RE.fullmatch(option_values["selected_value"]) is None
                or not isinstance(option_values["meaning_binding_ref"], str)
                or _FINGERPRINT_RE.fullmatch(option_values["meaning_binding_ref"]) is None
                or not isinstance(option_values["frame_fingerprint"], str)
                or _FINGERPRINT_RE.fullmatch(option_values["frame_fingerprint"]) is None
            ):
                raise PlayerLessonsValidationError(
                    "applied intent result requires exact frame, value, and binding fingerprints"
                )
        elif (
            reason in INTENT_APPLIED_REASONS
            or any(
                option_values[key] is not None
                for key in ("selected_value", "meaning_binding_ref", "frame_fingerprint")
            )
            or (
                option_values["frame_id"] is not None
                and (
                    not isinstance(option_values["frame_id"], str)
                    or _STABLE_VALUE_RE.fullmatch(option_values["frame_id"]) is None
                )
            )
        ):
            raise PlayerLessonsValidationError(
                "non-applied intent result cannot retain a selected binding"
            )
        return {
            "lesson_id": lesson_id,
            "lesson_revision": int(lesson_revision),
            "lesson_fingerprint": lesson_fingerprint,
            "applied": int(applied),
            "reason": str(reason),
            **option_values,
        }

    def record_intent_applications(
        self,
        branch_id: object,
        turn_index: object,
        applications: object,
    ) -> dict[str, Any]:
        """Persist the first application result while the selected lesson is still current."""
        return self._record_intent_applications(
            branch_id,
            turn_index,
            applications,
            allow_historical_repair=False,
        )

    def repair_intent_applications(
        self,
        branch_id: object,
        turn_index: object,
        applications: object,
    ) -> dict[str, Any]:
        """Finish a previously attempted content-free receipt from its frozen selection.

        Correction or disablement after the live turn must not erase truthful historical
        observability. Secure removal deletes the frozen item, so it still makes repair impossible.
        """
        return self._record_intent_applications(
            branch_id,
            turn_index,
            applications,
            allow_historical_repair=True,
        )

    def _record_intent_applications(
        self,
        branch_id: object,
        turn_index: object,
        applications: object,
        *,
        allow_historical_repair: bool,
    ) -> dict[str, Any]:
        branch_id, turn_index = self._validate_receipt_identity(branch_id, turn_index)
        if isinstance(applications, (str, bytes)) or not isinstance(applications, Iterable):
            raise PlayerLessonsValidationError("applications must be an iterable")
        requested = [self._validate_intent_application_request(value) for value in applications]
        if len(requested) > MAX_SELECTED_LESSONS or len(
            {value["lesson_id"] for value in requested}
        ) != len(requested):
            raise PlayerLessonsValidationError(
                "applications must identify at most five distinct frozen lessons"
            )
        with self._write_transaction():
            receipt = self._ensure_intent_receipt(branch_id, turn_index)
            if receipt is None:
                raise PlayerLessonsNotFoundError(
                    "no frozen intent receipt exists for this branch and turn"
                )
            item_rows = self._connection.execute(
                f"""
                SELECT {", ".join(_INTENT_ITEM_COLUMNS)}
                FROM player_lesson_intent_selection_items
                WHERE branch_id=? AND turn_index=?
                """,
                (branch_id, turn_index),
            ).fetchall()
            items = {
                item["lesson_id"]: item
                for row in item_rows
                if (item := self._intent_item_values(row)) is not None
            }
            for request in requested:
                item = items.get(request["lesson_id"])
                if item is None or (
                    item["lesson_revision"], item["lesson_fingerprint"]
                ) != (request["lesson_revision"], request["lesson_fingerprint"]):
                    raise PlayerLessonsConflictError(
                        "intent application does not match the frozen selection identity"
                    )
                if not allow_historical_repair:
                    lesson = self._stored_lesson(self._lesson_row(item["lesson_id"]))
                    anchor = None if lesson is None else _anchor_from_values(lesson)
                    anchor_status, _reason = self._anchor_state(anchor)
                    if (
                        lesson is None
                        or lesson["effect_type"] != "intent_interpretation"
                        or lesson["revision"] != item["lesson_revision"]
                        or lesson["fingerprint"] != item["lesson_fingerprint"]
                        or not bool(lesson["enabled"])
                        or anchor_status != "current"
                    ):
                        raise PlayerLessonsConflictError(
                            "intent application lesson changed after its selection was frozen"
                        )
                existing_row = self._connection.execute(
                    f"""
                    SELECT {", ".join(_INTENT_APPLICATION_COLUMNS)}
                    FROM player_lesson_intent_applications
                    WHERE branch_id=? AND turn_index=? AND position=?
                    """,
                    (branch_id, turn_index, item["position"]),
                ).fetchone()
                existing = self._intent_application_values(existing_row)
                if existing is not None:
                    comparable = {
                        key: existing[key]
                        for key in request
                    }
                    if comparable != request:
                        raise PlayerLessonsConflictError(
                            "intent application is immutable once recorded"
                        )
                    continue
                values = {
                    "branch_id": branch_id,
                    "turn_index": turn_index,
                    "position": item["position"],
                    **request,
                    "applied_at": time.time(),
                }
                self._connection.execute(
                    f"""
                    INSERT INTO player_lesson_intent_applications(
                        {", ".join(_INTENT_APPLICATION_COLUMNS)}
                    ) VALUES({", ".join("?" for _ in _INTENT_APPLICATION_COLUMNS)})
                    """,
                    tuple(values[column] for column in _INTENT_APPLICATION_COLUMNS),
                )
        result = self._rehydrate_intent_direct(branch_id, turn_index)
        if result is None:
            raise PlayerLessonsError("intent receipt disappeared after application recording")
        return result

    def latest_intent_applications(
        self, session_id: object | None = None
    ) -> list[dict[str, Any]]:
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id or len(session_id) > 256
        ):
            raise PlayerLessonsValidationError("session_id must be non-empty bounded text")
        where = ""
        parameters: tuple[Any, ...] = ()
        if session_id is not None:
            where = (
                "WHERE EXISTS (SELECT 1 FROM branches AS b "
                "WHERE b.branch_id=a.branch_id AND b.session_id=?)"
            )
            parameters = (session_id,)
        columns = (
            *_INTENT_APPLICATION_COLUMNS,
            "input_hash",
            "narration_mode",
            "selected_at",
            "scope",
            "intent_slot",
            "anchor_entry_id",
            "anchor_lex_id",
            "anchor_concept_id",
            "anchor_meaning_fingerprint",
            "source_start",
            "source_end",
            "approval_source_id",
        )
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    {", ".join(f"a.{column}" for column in _INTENT_APPLICATION_COLUMNS)},
                    r.input_hash, r.narration_mode, r.selected_at,
                    i.scope, i.intent_slot, i.anchor_entry_id, i.anchor_lex_id,
                    i.anchor_concept_id, i.anchor_meaning_fingerprint,
                    i.source_start, i.source_end, i.approval_source_id
                FROM player_lesson_intent_applications AS a
                JOIN player_lesson_intent_receipts AS r
                  ON r.branch_id=a.branch_id AND r.turn_index=a.turn_index
                JOIN player_lesson_intent_selection_items AS i
                  ON i.branch_id=a.branch_id AND i.turn_index=a.turn_index
                 AND i.position=a.position AND i.lesson_id=a.lesson_id
                {where}
                ORDER BY a.applied_at DESC, a.branch_id, a.turn_index DESC, a.position
                """,
                parameters,
            ).fetchall()
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            values = self._row_values(row, columns)
            if values is None or values["lesson_id"] in seen:
                continue
            application = self._intent_application_values(
                tuple(values[column] for column in _INTENT_APPLICATION_COLUMNS)
            )
            if application is None:
                continue
            seen.add(application["lesson_id"])
            result.append(
                {
                    "schema": INTENT_APPLICATION_SCHEMA,
                    "application_stage": "post_recognition_pre_contextual_binding",
                    "branch_id": application["branch_id"],
                    "turn_index": application["turn_index"],
                    "input_hash": values["input_hash"],
                    "narration_mode": values["narration_mode"],
                    "selected_at": values["selected_at"],
                    "lesson_id": application["lesson_id"],
                    "lesson_revision": application["lesson_revision"],
                    "lesson_fingerprint": application["lesson_fingerprint"],
                    "scope": values["scope"],
                    "intent_slot": values["intent_slot"],
                    "anchor": _anchor_from_values(values),
                    "recognition": {
                        "source_start": values["source_start"],
                        "source_end": values["source_end"],
                        "approval_source_id": values["approval_source_id"],
                    },
                    "applied": bool(application["applied"]),
                    "reason": application["reason"],
                    "frame_id": application["frame_id"],
                    "selected_value": application["selected_value"],
                    "meaning_binding_ref": application["meaning_binding_ref"],
                    "frame_fingerprint": application["frame_fingerprint"],
                    "applied_at": application["applied_at"],
                }
            )
        return result


# Singular aliases make typed error imports unsurprising to control-layer callers.
PlayerLessonError = PlayerLessonsError
PlayerLessonValidationError = PlayerLessonsValidationError
PlayerLessonConflictError = PlayerLessonsConflictError
PlayerLessonNotFoundError = PlayerLessonsNotFoundError
PlayerLessonRetryableRemovalError = PlayerLessonsRetryableRemovalError
