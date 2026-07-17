"""Local, explicitly approved PlayerLex recognition proposals.

PlayerLex stores Player-approved names, aliases, and bounded authoring patterns against one exact
Semantic Atlas meaning fingerprint.  It is a recognition overlay only: no session text is mined,
and no entry can assign a capability, admit a receipt, settle an outcome, or create world truth.
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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .capability_glossary import (
    CapabilityGlossary,
    GlossaryError,
)
from .semantic_atlas import MAX_CURSOR_LENGTH


ENTRY_SCHEMA = "playerlex-entry/2"
V1_ENTRY_SCHEMA = "playerlex-entry/1"
CORRUPT_ENTRY_SCHEMA = "playerlex-corrupt-entry/1"
PROPOSAL_SCHEMA = "playerlex-recognition-proposal/1"
CANDIDATE_SCHEMA = "playerlex-recognition-candidate/1"
ENTRY_KINDS = frozenset({"name", "alias", "authoring_pattern"})
LEX_IDS = frozenset({"capability", "referent", "scene", "action"})
APPROVAL_METHOD = "local_control_api"
MAX_SURFACE_CHARS = 240
MAX_PROPOSAL_CHARS = 2000
MAX_PATTERN_SLOTS = 4
MAX_CONCEPT_QUERY_CHARS = 120
MAX_CONCEPT_PAGE_SIZE = 100

_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ENTRY_ID_RE = re.compile(r"playerlex_[0-9a-f]{32}\Z")
_STORAGE_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_CORRUPT_LOCATOR_RE = re.compile(r"playerlex_corrupt_([0-9a-f]{32})_([0-9a-f]{64})\Z")
_SLOT_CONTENT_RE = re.compile(r"\{([^{}]*)\}")
_SLOT_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_SLOT_TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_ROW_COLUMNS = (
    "entry_id",
    "schema",
    "kind",
    "surface",
    "normalized_surface",
    "lex_id",
    "concept_id",
    "meaning_fingerprint",
    "approval_revision",
    "approved_at",
    "created_at",
    "updated_at",
    "record_json",
)
_STORAGE_TOKEN = "_playerlex_storage_token"
_SELECT_COLUMNS = (
    f"storage_token AS {_STORAGE_TOKEN}, "
    "entry_id, schema, kind, surface, normalized_surface, lex_id, concept_id, "
    "meaning_fingerprint, approval_revision, approved_at, created_at, updated_at, record_json"
)
_RECORD_KEYS = frozenset(
    {
        "schema",
        "entry_id",
        "kind",
        "surface",
        "normalized_surface",
        "lex_id",
        "pattern_slots",
        "concept",
        "provenance",
        "created_at",
        "updated_at",
    }
)
_CONCEPT_KEYS = frozenset(
    {
        "lex_id",
        "concept_id",
        "label",
        "definition",
        "concept_kind",
        "domain_shelves",
        "meaning_fingerprint",
    }
)
_PROVENANCE_KEYS = frozenset({"approval", "approved_via", "approved_at", "approval_revision"})

_V1_ROW_COLUMNS = tuple(column for column in _ROW_COLUMNS if column != "lex_id")
_V1_RECORD_KEYS = _RECORD_KEYS - {"lex_id"}
_V1_CONCEPT_KEYS = frozenset(
    {
        "schema",
        "concept_id",
        "concept_kind",
        "domain_shelves",
        "meaning_facets",
        "meaning_fingerprint",
    }
)
_V1_MEANING_FACET_KEYS = frozenset({"semantic_role", "target_cardinality", "spatial_extent", "world_scope"})

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS playerlex_entries (
        storage_token TEXT PRIMARY KEY NOT NULL CHECK (
            length(storage_token) = 32
            AND storage_token NOT GLOB '*[^0-9a-f]*'
        ),
        entry_id TEXT UNIQUE,
        schema TEXT NOT NULL CHECK (schema = 'playerlex-entry/2'),
        kind TEXT NOT NULL CHECK (kind IN ('name', 'alias', 'authoring_pattern')),
        surface TEXT NOT NULL,
        normalized_surface TEXT NOT NULL,
        lex_id TEXT NOT NULL CHECK (lex_id IN ('capability', 'referent', 'scene', 'action')),
        concept_id TEXT NOT NULL,
        meaning_fingerprint TEXT NOT NULL,
        approval_revision INTEGER NOT NULL CHECK (approval_revision >= 1),
        approved_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        record_json TEXT NOT NULL,
        UNIQUE (kind, normalized_surface, lex_id, concept_id),
        CHECK (length(surface) BETWEEN 1 AND 240),
        CHECK (length(normalized_surface) BETWEEN 1 AND 240),
        CHECK (
            length(meaning_fingerprint) = 71
            AND substr(meaning_fingerprint, 1, 7) = 'sha256:'
            AND substr(meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playerlex_retired_storage_tokens (
        storage_token TEXT PRIMARY KEY NOT NULL CHECK (
            length(storage_token) = 32
            AND storage_token NOT GLOB '*[^0-9a-f]*'
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS playerlex_surface_idx
    ON playerlex_entries(normalized_surface, kind, lex_id, concept_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_retired_storage_token
    BEFORE INSERT ON playerlex_entries
    WHEN EXISTS (
        SELECT 1 FROM playerlex_retired_storage_tokens
        WHERE storage_token = NEW.storage_token
    )
    BEGIN
        SELECT RAISE(ABORT, 'retired PlayerLex storage token');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_retire_storage_token
    AFTER DELETE ON playerlex_entries
    BEGIN
        INSERT INTO playerlex_retired_storage_tokens(storage_token)
        VALUES(OLD.storage_token);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_storage_token_update
    BEFORE UPDATE OF storage_token ON playerlex_entries
    BEGIN
        SELECT RAISE(ABORT, 'immutable PlayerLex storage token');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_identity_replacement
    BEFORE INSERT ON playerlex_entries
    WHEN EXISTS (
        SELECT 1 FROM playerlex_entries
        WHERE storage_token = NEW.storage_token
    )
    OR (
        NEW.entry_id IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM playerlex_entries
            WHERE entry_id = NEW.entry_id
        )
    )
    OR EXISTS (
        SELECT 1 FROM playerlex_entries
        WHERE kind = NEW.kind
          AND normalized_surface = NEW.normalized_surface
          AND lex_id = NEW.lex_id
          AND concept_id = NEW.concept_id
    )
    OR EXISTS (
        SELECT 1 FROM playerlex_entries
        WHERE rowid = NEW.rowid
    )
    BEGIN
        SELECT RAISE(ABORT, 'existing PlayerLex identity cannot be replaced');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_retired_storage_token_update
    BEFORE UPDATE ON playerlex_retired_storage_tokens
    BEGIN
        SELECT RAISE(ABORT, 'immutable PlayerLex retired storage token');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_retired_storage_token_delete
    BEFORE DELETE ON playerlex_retired_storage_tokens
    BEGIN
        SELECT RAISE(ABORT, 'immutable PlayerLex retired storage token');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS playerlex_reject_retired_storage_token_replacement
    BEFORE INSERT ON playerlex_retired_storage_tokens
    WHEN EXISTS (
        SELECT 1 FROM playerlex_retired_storage_tokens
        WHERE storage_token = NEW.storage_token
    )
    OR EXISTS (
        SELECT 1 FROM playerlex_retired_storage_tokens
        WHERE rowid = NEW.rowid
    )
    BEGIN
        SELECT RAISE(ABORT, 'retired PlayerLex identity cannot be replaced');
    END
    """,
)
_EXPECTED_PERSISTENT_SCHEMA_OBJECTS = frozenset(
    {
        ("table", "playerlex_entries", "playerlex_entries"),
        (
            "table",
            "playerlex_retired_storage_tokens",
            "playerlex_retired_storage_tokens",
        ),
        ("index", "playerlex_surface_idx", "playerlex_entries"),
        ("index", "sqlite_autoindex_playerlex_entries_1", "playerlex_entries"),
        ("index", "sqlite_autoindex_playerlex_entries_2", "playerlex_entries"),
        ("index", "sqlite_autoindex_playerlex_entries_3", "playerlex_entries"),
        (
            "index",
            "sqlite_autoindex_playerlex_retired_storage_tokens_1",
            "playerlex_retired_storage_tokens",
        ),
        (
            "trigger",
            "playerlex_reject_retired_storage_token",
            "playerlex_entries",
        ),
        ("trigger", "playerlex_retire_storage_token", "playerlex_entries"),
        (
            "trigger",
            "playerlex_reject_storage_token_update",
            "playerlex_entries",
        ),
        (
            "trigger",
            "playerlex_reject_identity_replacement",
            "playerlex_entries",
        ),
        (
            "trigger",
            "playerlex_reject_retired_storage_token_update",
            "playerlex_retired_storage_tokens",
        ),
        (
            "trigger",
            "playerlex_reject_retired_storage_token_delete",
            "playerlex_retired_storage_tokens",
        ),
        (
            "trigger",
            "playerlex_reject_retired_storage_token_replacement",
            "playerlex_retired_storage_tokens",
        ),
    }
)
_EXPECTED_TABLE_COLUMNS = (
    ("storage_token", "TEXT", 1, None, 1),
    ("entry_id", "TEXT", 0, None, 0),
    ("schema", "TEXT", 1, None, 0),
    ("kind", "TEXT", 1, None, 0),
    ("surface", "TEXT", 1, None, 0),
    ("normalized_surface", "TEXT", 1, None, 0),
    ("lex_id", "TEXT", 1, None, 0),
    ("concept_id", "TEXT", 1, None, 0),
    ("meaning_fingerprint", "TEXT", 1, None, 0),
    ("approval_revision", "INTEGER", 1, None, 0),
    ("approved_at", "REAL", 1, None, 0),
    ("created_at", "REAL", 1, None, 0),
    ("updated_at", "REAL", 1, None, 0),
    ("record_json", "TEXT", 1, None, 0),
)
_EXPECTED_RETIRED_COLUMNS = (("storage_token", "TEXT", 1, None, 1),)
_V1_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS playerlex_entries (
    storage_token TEXT PRIMARY KEY NOT NULL CHECK (
        length(storage_token) = 32
        AND storage_token NOT GLOB '*[^0-9a-f]*'
    ),
    entry_id TEXT UNIQUE,
    schema TEXT NOT NULL CHECK (schema = 'playerlex-entry/1'),
    kind TEXT NOT NULL CHECK (kind IN ('name', 'alias', 'authoring_pattern')),
    surface TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    meaning_fingerprint TEXT NOT NULL,
    approval_revision INTEGER NOT NULL CHECK (approval_revision >= 1),
    approved_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE (kind, normalized_surface, concept_id),
    CHECK (length(surface) BETWEEN 1 AND 240),
    CHECK (length(normalized_surface) BETWEEN 1 AND 240),
    CHECK (
        length(meaning_fingerprint) = 71
        AND substr(meaning_fingerprint, 1, 7) = 'sha256:'
        AND substr(meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
    )
)
"""
_V1_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS playerlex_surface_idx
ON playerlex_entries(normalized_surface, kind, concept_id)
"""
_V1_IDENTITY_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS playerlex_reject_identity_replacement
BEFORE INSERT ON playerlex_entries
WHEN EXISTS (
    SELECT 1 FROM playerlex_entries
    WHERE storage_token = NEW.storage_token
)
OR (
    NEW.entry_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM playerlex_entries
        WHERE entry_id = NEW.entry_id
    )
)
OR EXISTS (
    SELECT 1 FROM playerlex_entries
    WHERE kind = NEW.kind
      AND normalized_surface = NEW.normalized_surface
      AND concept_id = NEW.concept_id
)
OR EXISTS (
    SELECT 1 FROM playerlex_entries
    WHERE rowid = NEW.rowid
)
BEGIN
    SELECT RAISE(ABORT, 'existing PlayerLex identity cannot be replaced');
END
"""
_V1_EXPECTED_TABLE_COLUMNS = tuple(column for column in _EXPECTED_TABLE_COLUMNS if column[0] != "lex_id")
_V1_SCHEMA_STATEMENTS = (
    _V1_TABLE_SQL,
    _SCHEMA_STATEMENTS[1],
    _V1_INDEX_SQL,
    _SCHEMA_STATEMENTS[3],
    _SCHEMA_STATEMENTS[4],
    _SCHEMA_STATEMENTS[5],
    _V1_IDENTITY_TRIGGER_SQL,
    _SCHEMA_STATEMENTS[7],
    _SCHEMA_STATEMENTS[8],
    _SCHEMA_STATEMENTS[9],
)
_LEGACY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS playerlex_entries (
    entry_id TEXT PRIMARY KEY,
    schema TEXT NOT NULL CHECK (schema = 'playerlex-entry/1'),
    kind TEXT NOT NULL CHECK (kind IN ('name', 'alias', 'authoring_pattern')),
    surface TEXT NOT NULL,
    normalized_surface TEXT NOT NULL,
    concept_id TEXT NOT NULL,
    meaning_fingerprint TEXT NOT NULL,
    approval_revision INTEGER NOT NULL CHECK (approval_revision >= 1),
    approved_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    record_json TEXT NOT NULL,
    UNIQUE (kind, normalized_surface, concept_id),
    CHECK (length(surface) BETWEEN 1 AND 240),
    CHECK (length(normalized_surface) BETWEEN 1 AND 240),
    CHECK (
        length(meaning_fingerprint) = 71
        AND substr(meaning_fingerprint, 1, 7) = 'sha256:'
        AND substr(meaning_fingerprint, 8) NOT GLOB '*[^0-9a-f]*'
    )
)
"""
_LEGACY_TABLE_COLUMNS = (("entry_id", "TEXT", 0, None, 1),) + tuple(
    column for column in _V1_EXPECTED_TABLE_COLUMNS if column[0] != "storage_token"
)[1:]


def _normalized_schema_sql(sql: str) -> str:
    compact = " ".join(sql.strip().rstrip(";").split())
    compact = re.sub(r"\bIF\s+NOT\s+EXISTS\b\s*", "", compact, count=1, flags=re.IGNORECASE)
    return compact.casefold()


def _expected_index_xinfo(*columns: str) -> tuple[tuple[str | None, int, str, int], ...]:
    return tuple((column, 0, "BINARY", 1) for column in columns) + ((None, 0, "BINARY", 0),)


class PlayerLexError(RuntimeError):
    """Base error for local PlayerLex records and proposals."""


class PlayerLexValidationError(PlayerLexError):
    """An approval, correction, or proposal violates the bounded contract."""


class PlayerLexConflictError(PlayerLexError):
    """The exact approved surface/concept mapping already exists."""


class PlayerLexNotFoundError(PlayerLexError):
    """A requested local entry does not exist."""


class PlayerLexRetryableRemovalError(PlayerLexError):
    """A committed removal still needs a successful secure-storage checkpoint."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__("secure PlayerLex removal needs a retry to finish scrubbing local storage")


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalize_surface(surface: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", surface).casefold().split())


def _flex_literal(literal: str) -> str:
    parts = re.split(r"(\s+)", literal)
    return "".join(r"\s+" if part.isspace() else re.escape(part) for part in parts if part)


def _literal_pattern(surface: str) -> re.Pattern[str]:
    folded = unicodedata.normalize("NFKC", surface).casefold()
    return re.compile(_flex_literal(folded), re.UNICODE)


def _token_continuation(char: str) -> bool:
    category = unicodedata.category(char)
    return char.isalnum() or category == "Pc" or category.startswith("M")


def _edge_has_continuation(text: str, index: int, step: int) -> bool:
    wrappers = {"Ps", "Pi"} if step < 0 else {"Pe", "Pf"}
    while 0 <= index < len(text) and unicodedata.category(text[index]) in wrappers:
        index += step
    return 0 <= index < len(text) and _token_continuation(text[index])


def _literal_boundary_ok(text: str, start: int, end: int) -> bool:
    return not (_edge_has_continuation(text, start - 1, -1) or _edge_has_continuation(text, end, 1))


def _folded_text_with_source_spans(text: str) -> tuple[str, list[int], list[int]]:
    """Normalize the whole input and map every folded code point to a stable source interval."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    stable_boundaries = [(0, 0)]
    for source_end in range(1, len(text) + 1):
        folded_prefix = unicodedata.normalize("NFKC", text[:source_end]).casefold()
        if len(folded_prefix) >= stable_boundaries[-1][1] and folded.startswith(folded_prefix):
            stable_boundaries.append((source_end, len(folded_prefix)))
    if stable_boundaries[-1] != (len(text), len(folded)):
        raise PlayerLexValidationError("proposal text cannot be mapped after Unicode normalization")

    starts: list[int] = []
    ends: list[int] = []
    for (source_start, folded_start), (source_end, folded_end) in zip(
        stable_boundaries, stable_boundaries[1:], strict=False
    ):
        starts.extend([source_start] * (folded_end - folded_start))
        ends.extend([source_end] * (folded_end - folded_start))
    if len(starts) != len(folded) or len(ends) != len(folded):
        raise PlayerLexValidationError("proposal text cannot be mapped after Unicode normalization")
    return folded, starts, ends


def _literal_matches(
    surface: str,
    text: str,
    folded_view: tuple[str, list[int], list[int]] | None = None,
) -> list[tuple[int, int]]:
    shadow, starts, ends = folded_view or _folded_text_with_source_spans(text)
    pattern = _literal_pattern(surface)
    matches: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    cursor = 0
    while cursor <= len(shadow):
        match = pattern.search(shadow, cursor)
        if match is None:
            break
        folded_start, folded_end = match.span()
        aligned_start = folded_start == 0 or starts[folded_start] != starts[folded_start - 1]
        aligned_end = folded_end == len(shadow) or ends[folded_end - 1] != ends[folded_end]
        if aligned_start and aligned_end and folded_end > folded_start:
            source_start = starts[folded_start]
            source_end = ends[folded_end - 1]
            source_span = (source_start, source_end)
            if source_span not in seen and _literal_boundary_ok(shadow, folded_start, folded_end):
                matches.append(source_span)
                seen.add(source_span)
        cursor = folded_start + 1
    return matches


def _pattern_parts(surface: str) -> tuple[str, ...]:
    if surface.count("{") != surface.count("}"):
        raise PlayerLexValidationError("authoring pattern braces must be balanced")
    raw_slots = _SLOT_CONTENT_RE.findall(surface)
    if len(raw_slots) != surface.count("{") or len(raw_slots) != surface.count("}"):
        raise PlayerLexValidationError("authoring pattern braces must be balanced and unnested")
    if not 1 <= len(raw_slots) <= MAX_PATTERN_SLOTS:
        raise PlayerLexValidationError("authoring patterns require one to four named slots")
    if any(not _SLOT_NAME_RE.fullmatch(slot) for slot in raw_slots):
        raise PlayerLexValidationError(
            "authoring pattern slot names must be lowercase letters, digits, or underscores"
        )
    if len(set(raw_slots)) != len(raw_slots):
        raise PlayerLexValidationError("authoring pattern slot names must be unique")
    tokens = list(_SLOT_TOKEN_RE.finditer(surface))
    for left, right in zip(tokens, tokens[1:], strict=False):
        separator = surface[left.end() : right.start()]
        if not separator.strip():
            raise PlayerLexValidationError(
                "authoring pattern slots require a non-whitespace literal separator"
            )
    literal = _SLOT_TOKEN_RE.sub("", surface)
    if not re.search(r"\w", literal, re.UNICODE):
        raise PlayerLexValidationError("authoring patterns require literal text outside their slots")
    return tuple(raw_slots)


def _authoring_pattern(
    surface: str,
    slots: tuple[str, ...],
    *,
    greedy_partitions: bool = False,
) -> re.Pattern[str]:
    pieces: list[str] = []
    cursor = 0
    tokens = list(_SLOT_TOKEN_RE.finditer(surface))
    for index, token in enumerate(tokens):
        pieces.append(_flex_literal(surface[cursor : token.start()]))
        quantifier = ".+" if greedy_partitions and index < len(tokens) - 1 else ".+?"
        pieces.append(f"(?P<{token.group(1)}>{quantifier})")
        cursor = token.end()
    pieces.append(_flex_literal(surface[cursor:]))
    if tuple(token.group(1) for token in tokens) != slots:
        raise PlayerLexValidationError("authoring pattern slots do not match the approved record")
    return re.compile(r"\A\s*" + "".join(pieces) + r"\s*\Z", re.IGNORECASE | re.UNICODE | re.DOTALL)


def _validate_lex_id(lex_id: object) -> str:
    if not isinstance(lex_id, str) or lex_id not in LEX_IDS:
        raise PlayerLexValidationError("lex_id must be capability, referent, scene, or action")
    return lex_id


def _plain_search_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        results: list[str] = []
        for item in value.values():
            results.extend(_plain_search_text(item))
        return results
    if isinstance(value, (list, tuple)):
        results = []
        for item in value:
            results.extend(_plain_search_text(item))
        return results
    return []


class _CapabilityAtlasAdapter:
    """Compatibility view for callers that still construct PlayerLex with CapabilityGlossary."""

    def __init__(self, glossary: CapabilityGlossary) -> None:
        self.capability_glossary = glossary

    def meaning(self, lex_id: str, concept_id: str) -> dict[str, Any]:
        if lex_id != "capability":
            raise KeyError((lex_id, concept_id))
        raw = self.capability_glossary.concepts.get(concept_id)
        if not isinstance(raw, Mapping):
            raise KeyError((lex_id, concept_id))
        classification = self.capability_glossary.concept_classification(concept_id)
        return {
            "lex_id": "capability",
            "concept_id": concept_id,
            "label": str(raw.get("label") or concept_id),
            "definition": str(raw.get("definition") or ""),
            "concept_kind": str(classification["concept_kind"]),
            "domain_shelves": [str(item) for item in classification["domain_shelves"]],
            "meaning_fingerprint": str(classification["meaning_fingerprint"]),
        }

    def search(
        self,
        query: str = "",
        lex_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if lex_id not in {None, "capability"}:
            return {
                "schema": "semantic-atlas-search/1",
                "concepts": [],
                "next_cursor": None,
            }
        if cursor is None:
            offset = 0
        else:
            match = re.fullmatch(r"capability:(0|[1-9][0-9]*)", cursor)
            if match is None:
                raise ValueError("invalid Semantic Atlas cursor")
            offset = int(match.group(1))
        needle = query.casefold().strip()
        matched: list[str] = []
        for concept_id, raw in sorted(self.capability_glossary.concepts.items()):
            if not isinstance(raw, Mapping):
                continue
            haystack = " ".join([concept_id, *_plain_search_text(raw)]).casefold()
            if not needle or needle in haystack:
                matched.append(concept_id)
        page_ids = matched[offset : offset + limit]
        next_offset = offset + len(page_ids)
        return {
            "schema": "semantic-atlas-search/1",
            "concepts": [self.meaning("capability", concept_id) for concept_id in page_ids],
            "next_cursor": (f"capability:{next_offset}" if next_offset < len(matched) else None),
        }


class PlayerLex:
    """SQLite-backed local approvals plus recognition-only proposal matching."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        atlas: Any,
        lock: Any | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if callable(getattr(atlas, "concept_classification", None)) and isinstance(
            getattr(atlas, "concepts", None), Mapping
        ):
            atlas = _CapabilityAtlasAdapter(atlas)
        if not callable(getattr(atlas, "meaning", None)):
            raise TypeError("atlas must expose exact Lex-qualified meaning lookup")
        if not callable(getattr(atlas, "search", None)):
            raise TypeError("atlas must expose bounded Lex-qualified search")
        self._connection = connection
        self._atlas = atlas
        self._lock = lock if lock is not None else threading.RLock()
        if self._connection.in_transaction:
            raise PlayerLexError(
                "PlayerLex initialization requires no active transaction; retry after it closes"
            )
        self._install_schema()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            if self._connection.in_transaction:
                savepoint = "playerlex_" + uuid.uuid4().hex
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
                raise PlayerLexError(
                    "PlayerLex initialization requires no active transaction; retry after it closes"
                )
            try:
                secure_delete = self._connection.execute("PRAGMA secure_delete=ON").fetchone()
                if secure_delete is None or int(secure_delete[0]) != 1:
                    raise PlayerLexError("SQLite secure deletion is unavailable for PlayerLex")
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    if not self._playerlex_schema_objects_exist():
                        for statement in _SCHEMA_STATEMENTS:
                            self._connection.execute(statement)
                    elif self._v1_schema_is_exact():
                        self._migrate_v1_schema()
                    else:
                        self._verify_schema()
                    self._verify_schema()
                except BaseException:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()
            except sqlite3.Error as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise PlayerLexError("PlayerLex local storage initialization failed") from exc

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
        indexes: dict[
            str,
            tuple[int, str, int, tuple[tuple[str | None, int, str, int], ...]],
        ] = {}
        for row in self._connection.execute(f"PRAGMA index_list({table})"):
            name = str(row[1])
            indexes[name] = (
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
        return indexes

    def _playerlex_schema_objects_exist(self) -> bool:
        return (
            self._connection.execute(
                """
                SELECT 1 FROM (
                    SELECT type, name, tbl_name FROM sqlite_master
                    UNION ALL
                    SELECT type, name, tbl_name FROM sqlite_temp_master
                )
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'playerlex_*' OR tbl_name GLOB 'playerlex_*')
                LIMIT 1
                """
            ).fetchone()
            is not None
        )

    def _persistent_playerlex_schema_objects(self) -> frozenset[tuple[str, str, str]]:
        return frozenset(
            (str(row[0]), str(row[1]), str(row[2]))
            for row in self._connection.execute(
                """
                SELECT type, name, tbl_name FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'playerlex_*' OR tbl_name GLOB 'playerlex_*')
                """
            )
        )

    def _trigger_sql(self) -> dict[str, str | None]:
        return {
            (str(row[1]) if row[0] == "main" else f"temp:{row[1]}"): row[2]
            for row in self._connection.execute(
                """
                SELECT 'main', name, sql FROM sqlite_master
                WHERE type='trigger' AND (
                    tbl_name IN ('playerlex_entries', 'playerlex_retired_storage_tokens')
                    OR name GLOB 'playerlex_*'
                )
                UNION ALL
                SELECT 'temp', name, sql FROM sqlite_temp_master
                WHERE type='trigger' AND (
                    tbl_name IN ('playerlex_entries', 'playerlex_retired_storage_tokens')
                    OR name GLOB 'playerlex_*'
                )
                """
            )
        }

    def _temporary_playerlex_schema_objects_exist(self) -> bool:
        return (
            self._connection.execute(
                """
                SELECT 1 FROM sqlite_temp_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                  AND (name GLOB 'playerlex_*' OR tbl_name GLOB 'playerlex_*')
                LIMIT 1
                """
            ).fetchone()
            is not None
        )

    def _v1_schema_is_exact(self) -> bool:
        table_sql = self._schema_sql("table", "playerlex_entries")
        retired_sql = self._schema_sql("table", "playerlex_retired_storage_tokens")
        indexes = self._index_metadata("playerlex_entries")
        retired_indexes = self._index_metadata("playerlex_retired_storage_tokens")
        expected_indexes = {
            (0, "c", 0, _expected_index_xinfo("normalized_surface", "kind", "concept_id")),
            (1, "u", 0, _expected_index_xinfo("entry_id")),
            (1, "u", 0, _expected_index_xinfo("kind", "normalized_surface", "concept_id")),
            (1, "pk", 0, _expected_index_xinfo("storage_token")),
        }
        expected_retired_indexes = {
            (1, "pk", 0, _expected_index_xinfo("storage_token")),
        }
        trigger_sql = self._trigger_sql()
        return (
            table_sql is not None
            and retired_sql is not None
            and _normalized_schema_sql(table_sql) == _normalized_schema_sql(_V1_TABLE_SQL)
            and _normalized_schema_sql(retired_sql) == _normalized_schema_sql(_SCHEMA_STATEMENTS[1])
            and self._table_columns("playerlex_entries") == _V1_EXPECTED_TABLE_COLUMNS
            and self._table_columns("playerlex_retired_storage_tokens") == _EXPECTED_RETIRED_COLUMNS
            and len(indexes) == len(expected_indexes)
            and set(indexes.values()) == expected_indexes
            and len(retired_indexes) == len(expected_retired_indexes)
            and set(retired_indexes.values()) == expected_retired_indexes
            and indexes.get("playerlex_surface_idx")
            == (
                0,
                "c",
                0,
                _expected_index_xinfo("normalized_surface", "kind", "concept_id"),
            )
            and _normalized_schema_sql(self._schema_sql("index", "playerlex_surface_idx") or "")
            == _normalized_schema_sql(_V1_INDEX_SQL)
            and set(trigger_sql)
            == {
                "playerlex_reject_retired_storage_token",
                "playerlex_retire_storage_token",
                "playerlex_reject_storage_token_update",
                "playerlex_reject_identity_replacement",
                "playerlex_reject_retired_storage_token_update",
                "playerlex_reject_retired_storage_token_delete",
                "playerlex_reject_retired_storage_token_replacement",
            }
            and _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_retired_storage_token", "")))
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[3])
            and _normalized_schema_sql(str(trigger_sql.get("playerlex_retire_storage_token", "")))
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[4])
            and _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_storage_token_update", "")))
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[5])
            and _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_identity_replacement", "")))
            == _normalized_schema_sql(_V1_IDENTITY_TRIGGER_SQL)
            and _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_update", ""))
            )
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[7])
            and _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_delete", ""))
            )
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[8])
            and _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_replacement", ""))
            )
            == _normalized_schema_sql(_SCHEMA_STATEMENTS[9])
            and self._persistent_playerlex_schema_objects() == _EXPECTED_PERSISTENT_SCHEMA_OBJECTS
            and not self._temporary_playerlex_schema_objects_exist()
        )

    @staticmethod
    def _v1_record_from_values(values: Mapping[str, Any]) -> dict[str, Any]:
        try:
            record = json.loads(values["record_json"])
            if not isinstance(record, dict) or set(record) != _V1_RECORD_KEYS:
                raise ValueError("record shape")
            concept = record["concept"]
            provenance = record["provenance"]
            if not isinstance(concept, dict) or set(concept) != _V1_CONCEPT_KEYS:
                raise ValueError("concept shape")
            if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
                raise ValueError("provenance shape")
            if (
                values["schema"] != V1_ENTRY_SCHEMA
                or record["schema"] != V1_ENTRY_SCHEMA
                or record["entry_id"] != values["entry_id"]
                or record["kind"] != values["kind"]
                or record["surface"] != values["surface"]
                or record["normalized_surface"] != values["normalized_surface"]
                or concept["schema"] != "aetherstate-concept-facets/1"
                or concept["concept_id"] != values["concept_id"]
                or concept["meaning_fingerprint"] != values["meaning_fingerprint"]
                or provenance["approval"] != "explicit_local"
                or provenance["approved_via"] != APPROVAL_METHOD
                or provenance["approval_revision"] != values["approval_revision"]
                or provenance["approved_at"] != values["approved_at"]
                or record["created_at"] != values["created_at"]
                or record["updated_at"] != values["updated_at"]
            ):
                raise ValueError("column mismatch")
            if (
                not isinstance(record["entry_id"], str)
                or _ENTRY_ID_RE.fullmatch(record["entry_id"]) is None
                or not isinstance(concept["concept_kind"], str)
                or not isinstance(concept["domain_shelves"], list)
                or any(not isinstance(item, str) for item in concept["domain_shelves"])
                or not isinstance(concept["meaning_facets"], dict)
                or set(concept["meaning_facets"]) != _V1_MEANING_FACET_KEYS
                or any(not isinstance(item, str) for item in concept["meaning_facets"].values())
                or not _FINGERPRINT_RE.fullmatch(str(concept["meaning_fingerprint"]))
                or not isinstance(provenance["approval_revision"], int)
                or isinstance(provenance["approval_revision"], bool)
                or provenance["approval_revision"] < 1
            ):
                raise ValueError("field type")
            for timestamp in (
                provenance["approved_at"],
                record["created_at"],
                record["updated_at"],
            ):
                if (
                    not isinstance(timestamp, (int, float))
                    or isinstance(timestamp, bool)
                    or not math.isfinite(float(timestamp))
                ):
                    raise ValueError("timestamp")
            exact_surface, normalized_surface, slots = PlayerLex._surface(record["kind"], record["surface"])
            if (
                exact_surface != record["surface"]
                or normalized_surface != record["normalized_surface"]
                or record["pattern_slots"] != list(slots)
            ):
                raise ValueError("surface derivation")
        except Exception as exc:
            raise PlayerLexError("stored PlayerLex v1 record is malformed") from exc
        return record

    def _migrated_v1_record(self, values: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            record = self._v1_record_from_values(values)
        except PlayerLexError:
            return None
        approved = record["concept"]
        concept_id = str(approved["concept_id"])
        try:
            current = self._classification("capability", concept_id)
        except PlayerLexValidationError:
            current = None
        if current is not None and current["meaning_fingerprint"] == approved["meaning_fingerprint"]:
            glossary = getattr(self._atlas, "capability_glossary", None)
            classifier = getattr(glossary, "concept_classification", None)
            if not callable(classifier):
                return None
            try:
                current_v1 = _json_copy(classifier(concept_id))
            except (GlossaryError, KeyError, TypeError, ValueError):
                return None
            if current_v1 != approved:
                return None
            concept = current
        else:
            concept = {
                "lex_id": "capability",
                "concept_id": concept_id,
                "label": (str(current["label"]) if current is not None else concept_id),
                "definition": (str(current["definition"]) if current is not None else ""),
                "concept_kind": str(approved["concept_kind"]),
                "domain_shelves": [str(item) for item in approved["domain_shelves"]],
                "meaning_fingerprint": str(approved["meaning_fingerprint"]),
            }
        return {
            "schema": ENTRY_SCHEMA,
            "entry_id": record["entry_id"],
            "kind": record["kind"],
            "surface": record["surface"],
            "normalized_surface": record["normalized_surface"],
            "lex_id": "capability",
            "pattern_slots": list(record["pattern_slots"]),
            "concept": _json_copy(concept),
            "provenance": _json_copy(record["provenance"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def _migrate_v1_schema(self) -> None:
        rows = self._connection.execute(
            f"SELECT storage_token AS {_STORAGE_TOKEN}, {', '.join(_V1_ROW_COLUMNS)} "
            "FROM playerlex_entries ORDER BY rowid"
        ).fetchall()
        for trigger_name in (
            "playerlex_reject_retired_storage_token",
            "playerlex_retire_storage_token",
            "playerlex_reject_storage_token_update",
            "playerlex_reject_identity_replacement",
            "playerlex_reject_retired_storage_token_update",
            "playerlex_reject_retired_storage_token_delete",
            "playerlex_reject_retired_storage_token_replacement",
        ):
            self._connection.execute(f"DROP TRIGGER {trigger_name}")
        self._connection.execute("DROP INDEX playerlex_surface_idx")
        self._connection.execute("ALTER TABLE playerlex_entries RENAME TO playerlex_entries_v1")
        for statement in _SCHEMA_STATEMENTS:
            self._connection.execute(statement)
        for row in rows:
            if isinstance(row, sqlite3.Row):
                storage_token = row[_STORAGE_TOKEN]
                values = {column: row[column] for column in _V1_ROW_COLUMNS}
            elif isinstance(row, tuple) and len(row) == len(_V1_ROW_COLUMNS) + 1:
                storage_token = row[0]
                values = dict(zip(_V1_ROW_COLUMNS, row[1:], strict=True))
            else:
                raise PlayerLexError("stored PlayerLex v1 row is incomplete")
            record = self._migrated_v1_record(values)
            if record is None:
                migrated_values = {
                    **values,
                    "schema": ENTRY_SCHEMA,
                    "lex_id": "capability",
                }
            else:
                migrated_values = {
                    "entry_id": record["entry_id"],
                    "schema": ENTRY_SCHEMA,
                    "kind": record["kind"],
                    "surface": record["surface"],
                    "normalized_surface": record["normalized_surface"],
                    "lex_id": "capability",
                    "concept_id": record["concept"]["concept_id"],
                    "meaning_fingerprint": record["concept"]["meaning_fingerprint"],
                    "approval_revision": record["provenance"]["approval_revision"],
                    "approved_at": record["provenance"]["approved_at"],
                    "created_at": record["created_at"],
                    "updated_at": record["updated_at"],
                    "record_json": _canonical_json(record),
                }
            placeholders = ",".join("?" for _ in range(len(_ROW_COLUMNS) + 1))
            self._connection.execute(
                f"""
                INSERT INTO playerlex_entries(storage_token, {", ".join(_ROW_COLUMNS)})
                VALUES({placeholders})
                """,
                (storage_token, *(migrated_values[column] for column in _ROW_COLUMNS)),
            )
        self._connection.execute("DROP TABLE playerlex_entries_v1")

    def _verify_schema(self) -> None:
        table_sql = self._schema_sql("table", "playerlex_entries")
        retired_sql = self._schema_sql("table", "playerlex_retired_storage_tokens")
        indexes = self._index_metadata("playerlex_entries")
        retired_indexes = self._index_metadata("playerlex_retired_storage_tokens")
        expected_indexes = {
            (
                0,
                "c",
                0,
                _expected_index_xinfo("normalized_surface", "kind", "lex_id", "concept_id"),
            ),
            (1, "u", 0, _expected_index_xinfo("entry_id")),
            (
                1,
                "u",
                0,
                _expected_index_xinfo("kind", "normalized_surface", "lex_id", "concept_id"),
            ),
            (1, "pk", 0, _expected_index_xinfo("storage_token")),
        }
        expected_retired_indexes = {
            (1, "pk", 0, _expected_index_xinfo("storage_token")),
        }
        trigger_sql = self._trigger_sql()
        secure_delete = self._connection.execute("PRAGMA secure_delete").fetchone()
        if (
            table_sql is None
            or retired_sql is None
            or _normalized_schema_sql(table_sql) != _normalized_schema_sql(_SCHEMA_STATEMENTS[0])
            or _normalized_schema_sql(retired_sql) != _normalized_schema_sql(_SCHEMA_STATEMENTS[1])
            or self._table_columns("playerlex_entries") != _EXPECTED_TABLE_COLUMNS
            or self._table_columns("playerlex_retired_storage_tokens") != _EXPECTED_RETIRED_COLUMNS
            or self._persistent_playerlex_schema_objects() != _EXPECTED_PERSISTENT_SCHEMA_OBJECTS
            or self._temporary_playerlex_schema_objects_exist()
            or len(indexes) != len(expected_indexes)
            or set(indexes.values()) != expected_indexes
            or len(retired_indexes) != len(expected_retired_indexes)
            or set(retired_indexes.values()) != expected_retired_indexes
            or indexes.get("playerlex_surface_idx")
            != (
                0,
                "c",
                0,
                _expected_index_xinfo("normalized_surface", "kind", "lex_id", "concept_id"),
            )
            or _normalized_schema_sql(self._schema_sql("index", "playerlex_surface_idx") or "")
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[2])
            or set(trigger_sql)
            != {
                "playerlex_reject_retired_storage_token",
                "playerlex_retire_storage_token",
                "playerlex_reject_storage_token_update",
                "playerlex_reject_identity_replacement",
                "playerlex_reject_retired_storage_token_update",
                "playerlex_reject_retired_storage_token_delete",
                "playerlex_reject_retired_storage_token_replacement",
            }
            or _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_retired_storage_token", "")))
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[3])
            or _normalized_schema_sql(str(trigger_sql.get("playerlex_retire_storage_token", "")))
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[4])
            or _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_storage_token_update", "")))
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[5])
            or _normalized_schema_sql(str(trigger_sql.get("playerlex_reject_identity_replacement", "")))
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[6])
            or _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_update", ""))
            )
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[7])
            or _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_delete", ""))
            )
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[8])
            or _normalized_schema_sql(
                str(trigger_sql.get("playerlex_reject_retired_storage_token_replacement", ""))
            )
            != _normalized_schema_sql(_SCHEMA_STATEMENTS[9])
            or secure_delete is None
            or int(secure_delete[0]) != 1
        ):
            raise PlayerLexError("PlayerLex local storage failed verification")

    @staticmethod
    def _surface(kind: object, surface: object) -> tuple[str, str, tuple[str, ...]]:
        if not isinstance(kind, str) or kind not in ENTRY_KINDS:
            raise PlayerLexValidationError("kind must be name, alias, or authoring_pattern")
        if not isinstance(surface, str):
            raise PlayerLexValidationError("surface must be text")
        value = unicodedata.normalize("NFKC", surface).strip()
        if not value:
            raise PlayerLexValidationError("surface must be non-empty")
        if len(value) > MAX_SURFACE_CHARS:
            raise PlayerLexValidationError(f"surface must be at most {MAX_SURFACE_CHARS} characters")
        if any(unicodedata.category(char).startswith("C") and not char.isspace() for char in value):
            raise PlayerLexValidationError("surface contains unsupported control characters")

        slots: tuple[str, ...] = ()
        if kind == "authoring_pattern":
            slots = _pattern_parts(value)
        else:
            if "{" in value or "}" in value:
                raise PlayerLexValidationError("only authoring_pattern entries may contain slots")
            if not re.search(r"\w", value, re.UNICODE):
                raise PlayerLexValidationError("name and alias surfaces require literal text")
        normalized = _normalize_surface(value)
        if not normalized:
            raise PlayerLexValidationError("surface must contain recognizable text")
        return value, normalized, slots

    @staticmethod
    def _validated_concept(
        value: object,
        *,
        expected_lex_id: str | None = None,
        expected_concept_id: str | None = None,
    ) -> dict[str, Any]:
        result = _json_copy(value)
        if (
            not isinstance(result, dict)
            or set(result) != _CONCEPT_KEYS
            or result.get("lex_id") not in LEX_IDS
            or not isinstance(result.get("concept_id"), str)
            or not result["concept_id"]
            or len(result["concept_id"]) > 128
            or not isinstance(result.get("label"), str)
            or not isinstance(result.get("definition"), str)
            or not isinstance(result.get("concept_kind"), str)
            or not result["concept_kind"]
            or not isinstance(result.get("domain_shelves"), list)
            or any(not isinstance(item, str) or not item for item in result["domain_shelves"])
            or not _FINGERPRINT_RE.fullmatch(str(result.get("meaning_fingerprint", "")))
            or (expected_lex_id is not None and result["lex_id"] != expected_lex_id)
            or (expected_concept_id is not None and result["concept_id"] != expected_concept_id)
        ):
            raise PlayerLexValidationError("concept classification violates the Semantic Atlas contract")
        return result

    def _classification(self, lex_id: object, concept_id: object) -> dict[str, Any]:
        exact_lex_id = _validate_lex_id(lex_id)
        if not isinstance(concept_id, str) or not concept_id.strip() or len(concept_id) > 128:
            raise PlayerLexValidationError("concept_id must be non-empty text")
        exact = concept_id.strip()
        try:
            classification = self._atlas.meaning(exact_lex_id, exact)
        except (GlossaryError, KeyError, ValueError) as exc:
            raise PlayerLexValidationError(f"unknown concept: {exact_lex_id}:{exact}") from exc
        return self._validated_concept(
            classification,
            expected_lex_id=exact_lex_id,
            expected_concept_id=exact,
        )

    @staticmethod
    def _semantic_snapshot_matches(approved: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        meaning_keys = _CONCEPT_KEYS - {"label"}
        return all(approved.get(key) == current.get(key) for key in meaning_keys)

    @staticmethod
    def _row_values(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            try:
                return {column: row[column] for column in _ROW_COLUMNS}
            except (IndexError, KeyError) as exc:
                raise PlayerLexError("stored PlayerLex row is incomplete") from exc
        if isinstance(row, tuple):
            if len(row) == len(_ROW_COLUMNS):
                return dict(zip(_ROW_COLUMNS, row, strict=True))
            if len(row) == len(_ROW_COLUMNS) + 1:
                return dict(zip(_ROW_COLUMNS, row[1:], strict=True))
        raise PlayerLexError("stored PlayerLex row is incomplete")

    @staticmethod
    def _storage_token(row: sqlite3.Row | tuple[Any, ...]) -> str:
        try:
            value = row[_STORAGE_TOKEN] if isinstance(row, sqlite3.Row) else row[0]
        except (IndexError, KeyError, TypeError) as exc:
            raise PlayerLexError("stored PlayerLex row has no stable local identity") from exc
        if not isinstance(value, str) or _STORAGE_TOKEN_RE.fullmatch(value) is None:
            raise PlayerLexError("stored PlayerLex row has an invalid local identity")
        return value

    @staticmethod
    def _record_from_row(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
        values = PlayerLex._row_values(row)
        if values is None:
            return None
        try:
            record = json.loads(values["record_json"])
            if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
                raise ValueError("record shape")
            concept = record["concept"]
            provenance = record["provenance"]
            if not isinstance(concept, dict) or set(concept) != _CONCEPT_KEYS:
                raise ValueError("concept shape")
            if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
                raise ValueError("provenance shape")
            if (
                values["schema"] != ENTRY_SCHEMA
                or not isinstance(values["record_json"], str)
                or record["schema"] != ENTRY_SCHEMA
                or record["entry_id"] != values["entry_id"]
                or record["kind"] != values["kind"]
                or record["surface"] != values["surface"]
                or record["normalized_surface"] != values["normalized_surface"]
                or record["lex_id"] != values["lex_id"]
                or concept["lex_id"] != values["lex_id"]
                or concept["concept_id"] != values["concept_id"]
                or concept["meaning_fingerprint"] != values["meaning_fingerprint"]
                or provenance["approval"] != "explicit_local"
                or provenance["approved_via"] != APPROVAL_METHOD
                or provenance["approval_revision"] != values["approval_revision"]
                or provenance["approved_at"] != values["approved_at"]
                or record["created_at"] != values["created_at"]
                or record["updated_at"] != values["updated_at"]
            ):
                raise ValueError("column mismatch")
            if (
                not isinstance(record["entry_id"], str)
                or _ENTRY_ID_RE.fullmatch(record["entry_id"]) is None
                or not isinstance(provenance["approval_revision"], int)
                or isinstance(provenance["approval_revision"], bool)
                or provenance["approval_revision"] < 1
            ):
                raise ValueError("field type")
            PlayerLex._validated_concept(
                concept,
                expected_lex_id=record["lex_id"],
                expected_concept_id=values["concept_id"],
            )
            for timestamp in (
                provenance["approved_at"],
                record["created_at"],
                record["updated_at"],
            ):
                if (
                    not isinstance(timestamp, (int, float))
                    or isinstance(timestamp, bool)
                    or not math.isfinite(float(timestamp))
                ):
                    raise ValueError("timestamp")
            exact_surface, normalized_surface, slots = PlayerLex._surface(record["kind"], record["surface"])
            if (
                exact_surface != record["surface"]
                or normalized_surface != record["normalized_surface"]
                or record["pattern_slots"] != list(slots)
            ):
                raise ValueError("surface derivation")
        except Exception as exc:
            raise PlayerLexError("stored PlayerLex record is malformed") from exc
        return record

    @staticmethod
    def _version_from_values(columns: tuple[str, ...], values: Mapping[str, Any]) -> str:
        digest = hashlib.sha256()
        for key in columns:
            value = values[key]
            if value is None:
                encoded = b"null:"
            elif isinstance(value, bytes):
                encoded = b"bytes:" + value
            elif isinstance(value, float):
                encoded = ("float:" + value.hex()).encode("ascii")
            elif isinstance(value, int):
                encoded = ("int:" + str(value)).encode("ascii")
            elif isinstance(value, str):
                encoded = b"text:" + value.encode("utf-8", errors="surrogatepass")
            else:
                encoded = ("other:" + type(value).__qualname__ + ":" + repr(value)).encode(
                    "utf-8", errors="backslashreplace"
                )
            digest.update(key.encode("ascii"))
            digest.update(b"\0")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _row_version(row: sqlite3.Row | tuple[Any, ...]) -> str:
        values = PlayerLex._row_values(row)
        if values is None:
            raise PlayerLexError("stored PlayerLex row is missing")
        return PlayerLex._version_from_values(_ROW_COLUMNS, values)

    @staticmethod
    def _legacy_v1_row_version(row: sqlite3.Row | tuple[Any, ...]) -> str | None:
        values = PlayerLex._row_values(row)
        if values is None or values.get("lex_id") != "capability":
            return None
        legacy_values = {column: values[column] for column in _V1_ROW_COLUMNS}
        legacy_values["schema"] = V1_ENTRY_SCHEMA
        return PlayerLex._version_from_values(_V1_ROW_COLUMNS, legacy_values)

    @staticmethod
    def _corrupt_version(row: sqlite3.Row | tuple[Any, ...]) -> str:
        return PlayerLex._legacy_v1_row_version(row) or PlayerLex._row_version(row)

    @staticmethod
    def _corrupt_locator(row: sqlite3.Row | tuple[Any, ...]) -> str:
        storage_token = PlayerLex._storage_token(row)
        version = PlayerLex._corrupt_version(row)
        return f"playerlex_corrupt_{storage_token}_{version.removeprefix('sha256:')}"

    def _select_row(self, entry_id: str) -> sqlite3.Row | tuple[Any, ...] | None:
        locator = _CORRUPT_LOCATOR_RE.fullmatch(entry_id)
        if locator is not None:
            row = self._connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM playerlex_entries WHERE storage_token=?",
                (locator.group(1),),
            ).fetchone()
            if row is not None:
                expected = "sha256:" + locator.group(2)
                accepted_versions = {self._row_version(row), self._corrupt_version(row)}
                if expected not in accepted_versions:
                    raise PlayerLexConflictError(
                        "PlayerLex entry changed since it was displayed; reload before continuing"
                    )
            return row
        return self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM playerlex_entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()

    @staticmethod
    def _corrupt_from_row(
        row: sqlite3.Row | tuple[Any, ...],
        *,
        reason: str = "stored_record_invalid",
    ) -> dict[str, Any]:
        values = PlayerLex._row_values(row)
        if values is None:
            raise PlayerLexError("stored PlayerLex row is missing")

        def text_value(key: str) -> str:
            value = values[key]
            return value if isinstance(value, str) else str(value)

        revision = values["approval_revision"]
        version = PlayerLex._corrupt_version(row)
        raw_entry_id = values["entry_id"]
        entry_id = (
            raw_entry_id
            if isinstance(raw_entry_id, str) and _ENTRY_ID_RE.fullmatch(raw_entry_id)
            else PlayerLex._corrupt_locator(row)
        )
        return {
            "schema": CORRUPT_ENTRY_SCHEMA,
            "entry_id": entry_id,
            "kind": text_value("kind"),
            "surface": text_value("surface"),
            "lex_id": text_value("lex_id"),
            "concept_id": text_value("concept_id"),
            "approved_meaning_fingerprint": text_value("meaning_fingerprint"),
            "approval_revision": (
                revision if isinstance(revision, int) and not isinstance(revision, bool) else None
            ),
            "corrupt_version": version,
            "status": "corrupt",
            "corruption_reason": reason,
            "removable": True,
        }

    def _is_corrupt_row(self, row: sqlite3.Row | tuple[Any, ...]) -> bool:
        try:
            record = self._record_from_row(row)
        except PlayerLexError:
            return True
        if record is None:
            return True
        concept = record["concept"]
        try:
            current = self._classification(concept["lex_id"], concept["concept_id"])
        except PlayerLexValidationError:
            return False
        return concept["meaning_fingerprint"] == current[
            "meaning_fingerprint"
        ] and not self._semantic_snapshot_matches(concept, current)

    def _present(
        self,
        record: dict[str, Any],
        row: sqlite3.Row | tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        result = _json_copy(record)
        lex_id = str(result["concept"]["lex_id"])
        concept_id = str(result["concept"]["concept_id"])
        try:
            current = self._classification(lex_id, concept_id)
        except PlayerLexValidationError:
            result["status"] = "missing_concept"
            result["current_meaning_fingerprint"] = None
            result["current_concept"] = None
            result["concept_label"] = str(result["concept"].get("label") or concept_id)
            return result

        approved_fingerprint = str(result["concept"]["meaning_fingerprint"])
        current_fingerprint = str(current["meaning_fingerprint"])
        if approved_fingerprint == current_fingerprint and not self._semantic_snapshot_matches(
            result["concept"], current
        ):
            if row is None:
                raise PlayerLexError("PlayerLex concept snapshot failed server verification")
            return self._corrupt_from_row(row, reason="current_concept_snapshot_invalid")
        result["status"] = "current" if approved_fingerprint == current_fingerprint else "stale"
        result["current_meaning_fingerprint"] = current_fingerprint
        result["current_concept"] = current
        result["concept_label"] = current["label"]
        return result

    def _build_record(
        self,
        *,
        entry_id: str,
        kind: object,
        surface: object,
        lex_id: object,
        concept_id: object,
        approval_revision: int,
        created_at: float,
    ) -> dict[str, Any]:
        exact_surface, normalized_surface, slots = self._surface(kind, surface)
        exact_lex_id = _validate_lex_id(lex_id)
        concept = self._classification(exact_lex_id, concept_id)
        now = time.time()
        return {
            "schema": ENTRY_SCHEMA,
            "entry_id": entry_id,
            "kind": kind,
            "surface": exact_surface,
            "normalized_surface": normalized_surface,
            "lex_id": exact_lex_id,
            "pattern_slots": list(slots),
            "concept": concept,
            "provenance": {
                "approval": "explicit_local",
                "approved_via": APPROVAL_METHOD,
                "approved_at": now,
                "approval_revision": approval_revision,
            },
            "created_at": created_at,
            "updated_at": now,
        }

    def approve(
        self,
        *,
        kind: object,
        surface: object,
        concept_id: object,
        lex_id: object = "capability",
    ) -> dict[str, Any]:
        """Store one explicit local approval against the current exact Atlas meaning."""
        now = time.time()
        record = self._build_record(
            entry_id="playerlex_" + uuid.uuid4().hex,
            kind=kind,
            surface=surface,
            lex_id=lex_id,
            concept_id=concept_id,
            approval_revision=1,
            created_at=now,
        )
        try:
            with self._write_transaction():
                self._connection.execute(
                    """
                    INSERT INTO playerlex_entries(
                        storage_token, entry_id, schema, kind, surface, normalized_surface, lex_id,
                        concept_id, meaning_fingerprint, approval_revision, approved_at, created_at,
                        updated_at, record_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uuid.uuid4().hex,
                        record["entry_id"],
                        ENTRY_SCHEMA,
                        record["kind"],
                        record["surface"],
                        record["normalized_surface"],
                        record["lex_id"],
                        record["concept"]["concept_id"],
                        record["concept"]["meaning_fingerprint"],
                        record["provenance"]["approval_revision"],
                        record["provenance"]["approved_at"],
                        record["created_at"],
                        record["updated_at"],
                        _canonical_json(record),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc) or "cannot be replaced" in str(exc):
                raise PlayerLexConflictError(
                    "this exact PlayerLex surface and Lex-qualified concept are already approved"
                ) from exc
            raise PlayerLexValidationError("PlayerLex record violates local storage constraints") from exc
        return self._present(record)

    def list_entries(self) -> list[dict[str, Any]]:
        """List detached entries, keeping malformed rows visible and removable."""
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT {_SELECT_COLUMNS}
                FROM playerlex_entries
                ORDER BY lower(surface), kind, lex_id, concept_id, entry_id
                """
            ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            try:
                record = self._record_from_row(row)
            except PlayerLexError:
                entries.append(self._corrupt_from_row(row))
                continue
            if record is not None:
                entries.append(self._present(record, row))
        return entries

    def correct(
        self,
        entry_id: str,
        *,
        kind: object | None = None,
        surface: object | None = None,
        concept_id: object | None = None,
        lex_id: object = "capability",
        expected_meaning_fingerprint: object | None = None,
        expected_approval_revision: object | None = None,
        expected_corrupt_version: object | None = None,
    ) -> dict[str, Any]:
        """Replace and reapprove one mapping with explicit optimistic concurrency."""
        if not isinstance(entry_id, str) or not entry_id:
            raise PlayerLexNotFoundError("unknown PlayerLex entry")
        if kind is None or surface is None or concept_id is None:
            raise PlayerLexValidationError(
                "PlayerLex correction requires a complete replacement: kind, surface, and concept_id"
            )
        corrupt_proof = expected_corrupt_version is not None
        normal_proof_supplied = (
            expected_meaning_fingerprint is not None or expected_approval_revision is not None
        )
        if corrupt_proof:
            if normal_proof_supplied or not isinstance(expected_corrupt_version, str):
                raise PlayerLexValidationError(
                    "PlayerLex correction requires exactly one displayed version proof"
                )
            if not _FINGERPRINT_RE.fullmatch(expected_corrupt_version):
                raise PlayerLexValidationError("expected corrupt version is invalid")
        elif (
            not isinstance(expected_meaning_fingerprint, str)
            or not _FINGERPRINT_RE.fullmatch(expected_meaning_fingerprint)
            or not isinstance(expected_approval_revision, int)
            or isinstance(expected_approval_revision, bool)
            or expected_approval_revision < 1
        ):
            raise PlayerLexValidationError(
                "PlayerLex correction requires the expected meaning fingerprint and approval revision"
            )
        try:
            with self._write_transaction():
                row = self._select_row(entry_id)
                values = self._row_values(row)
                if values is None:
                    raise PlayerLexNotFoundError(f"unknown PlayerLex entry: {entry_id}")
                storage_token = self._storage_token(row)
                row_is_corrupt = self._is_corrupt_row(row)
                stored_revision = values["approval_revision"]
                if corrupt_proof:
                    if not row_is_corrupt or self._corrupt_version(row) != expected_corrupt_version:
                        raise PlayerLexConflictError(
                            "PlayerLex entry changed since it was displayed; reload before reapproving"
                        )
                    approval_revision = (
                        stored_revision + 1
                        if isinstance(stored_revision, int)
                        and not isinstance(stored_revision, bool)
                        and 1 <= stored_revision < 2**63 - 1
                        else 1
                    )
                else:
                    if (
                        row_is_corrupt
                        or values["meaning_fingerprint"] != expected_meaning_fingerprint
                        or stored_revision != expected_approval_revision
                    ):
                        raise PlayerLexConflictError(
                            "PlayerLex entry changed since it was displayed; reload before reapproving"
                        )
                    approval_revision = expected_approval_revision + 1
                created_at = values["created_at"]
                if (
                    not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(float(created_at))
                ):
                    created_at = time.time()
                stored_entry_id = values["entry_id"]
                replacement_entry_id = (
                    stored_entry_id
                    if isinstance(stored_entry_id, str) and _ENTRY_ID_RE.fullmatch(stored_entry_id)
                    else "playerlex_" + uuid.uuid4().hex
                )
                record = self._build_record(
                    entry_id=replacement_entry_id,
                    kind=kind,
                    surface=surface,
                    lex_id=lex_id,
                    concept_id=concept_id,
                    approval_revision=approval_revision,
                    created_at=float(created_at),
                )
                updated = self._connection.execute(
                    """
                    UPDATE playerlex_entries SET
                        entry_id=?, schema=?, kind=?, surface=?, normalized_surface=?, lex_id=?,
                        concept_id=?, meaning_fingerprint=?, approval_revision=?, approved_at=?,
                        created_at=?, updated_at=?, record_json=?
                    WHERE storage_token=?
                    """,
                    (
                        record["entry_id"],
                        ENTRY_SCHEMA,
                        record["kind"],
                        record["surface"],
                        record["normalized_surface"],
                        record["lex_id"],
                        record["concept"]["concept_id"],
                        record["concept"]["meaning_fingerprint"],
                        record["provenance"]["approval_revision"],
                        record["provenance"]["approved_at"],
                        record["created_at"],
                        record["updated_at"],
                        _canonical_json(record),
                        storage_token,
                    ),
                )
                if updated.rowcount != 1:
                    raise PlayerLexConflictError(
                        "PlayerLex entry changed since it was displayed; reload before reapproving"
                    )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise PlayerLexConflictError(
                    "this exact PlayerLex surface and Lex-qualified concept are already approved"
                ) from exc
            raise PlayerLexValidationError("PlayerLex correction violates local constraints") from exc
        return self._present(record)

    def remove(self, entry_id: str) -> bool:
        """Idempotently delete approved text and finish scrubbing the database and WAL."""
        if not isinstance(entry_id, str) or not entry_id:
            raise PlayerLexNotFoundError("unknown PlayerLex entry")
        with self._lock:
            if self._connection.in_transaction:
                raise PlayerLexError(
                    "secure PlayerLex removal requires no active transaction; retry after it closes"
                )
            try:
                secure_delete = self._connection.execute("PRAGMA secure_delete=ON").fetchone()
                if secure_delete is None or int(secure_delete[0]) != 1:
                    raise PlayerLexError("SQLite secure deletion is unavailable for PlayerLex")
            except sqlite3.Error as exc:
                raise PlayerLexRetryableRemovalError(entry_id) from exc
            try:
                self._checkpoint_wal()
            except (sqlite3.Error, PlayerLexError) as exc:
                raise PlayerLexRetryableRemovalError(entry_id) from exc
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._select_row(entry_id)
                    if row is not None:
                        self._connection.execute(
                            "DELETE FROM playerlex_entries WHERE storage_token=?",
                            (self._storage_token(row),),
                        )
                except BaseException:
                    self._connection.rollback()
                    raise
                else:
                    self._connection.commit()
            except sqlite3.Error as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise PlayerLexRetryableRemovalError(entry_id) from exc
            try:
                self._checkpoint_wal()
            except (sqlite3.Error, PlayerLexError) as exc:
                raise PlayerLexRetryableRemovalError(entry_id) from exc
        return True

    def _checkpoint_wal(self) -> None:
        journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
            return
        result = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is None or int(result[0]) != 0:
            raise PlayerLexError("secure PlayerLex removal could not obtain an exclusive WAL checkpoint")

    def list_concepts(
        self,
        query: object = "",
        *,
        lex_id: object | None = None,
        limit: int = 50,
        cursor: object | None = None,
        concept_id: object | None = None,
        exact_concept_id: object | None = None,
    ) -> dict[str, Any]:
        """Return one bounded Atlas page or one exact Lex-qualified concept."""
        if not isinstance(query, str) or len(query) > MAX_CONCEPT_QUERY_CHARS:
            raise PlayerLexValidationError(
                f"concept query must be at most {MAX_CONCEPT_QUERY_CHARS} characters"
            )
        exact_lex_id = None if lex_id is None else _validate_lex_id(lex_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CONCEPT_PAGE_SIZE:
            raise PlayerLexValidationError(f"concept limit must be between 1 and {MAX_CONCEPT_PAGE_SIZE}")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH
        ):
            raise PlayerLexValidationError("concept cursor is invalid")
        if concept_id is not None and exact_concept_id is not None:
            raise PlayerLexValidationError("supply only one exact concept lookup")
        exact = concept_id if concept_id is not None else exact_concept_id
        if exact is not None:
            if exact_lex_id is None:
                raise PlayerLexValidationError("exact concept lookup requires lex_id")
            if query.strip() or cursor is not None:
                raise PlayerLexValidationError("exact concept lookup cannot be combined with query or cursor")
            concept = self._classification(exact_lex_id, exact)
            return {
                "schema": "semantic-atlas-search/1",
                "concepts": [concept],
                "next_cursor": None,
            }
        try:
            page = self._atlas.search(
                query=query,
                lex_id=exact_lex_id,
                limit=limit,
                cursor=cursor,
            )
        except (GlossaryError, KeyError, ValueError) as exc:
            raise PlayerLexValidationError("Semantic Atlas search request is invalid") from exc
        if (
            not isinstance(page, Mapping)
            or set(page) != {"schema", "concepts", "next_cursor"}
            or page.get("schema") != "semantic-atlas-search/1"
            or not isinstance(page.get("concepts"), list)
            or len(page["concepts"]) > limit
            or (
                page.get("next_cursor") is not None
                and (
                    not isinstance(page["next_cursor"], str)
                    or not page["next_cursor"]
                    or len(page["next_cursor"]) > MAX_CURSOR_LENGTH
                )
            )
        ):
            raise PlayerLexError("Semantic Atlas search response failed verification")
        concepts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in page["concepts"]:
            concept = self._validated_concept(raw)
            if exact_lex_id is not None and concept["lex_id"] != exact_lex_id:
                raise PlayerLexError("Semantic Atlas search escaped its Lex filter")
            identity = (concept["lex_id"], concept["concept_id"])
            if identity in seen:
                raise PlayerLexError("Semantic Atlas search returned a duplicate concept")
            seen.add(identity)
            concepts.append(concept)
        return {
            "schema": "semantic-atlas-search/1",
            "concepts": concepts,
            "next_cursor": page["next_cursor"],
        }

    @staticmethod
    def _matches(
        entry: dict[str, Any],
        text: str,
        folded_view: tuple[str, list[int], list[int]] | None = None,
    ) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        surface = str(entry["surface"])
        kind = str(entry["kind"])
        if kind in {"name", "alias"}:
            return [
                (
                    {"start": start, "end": end, "text": text[start:end]},
                    [],
                )
                for start, end in _literal_matches(surface, text, folded_view)
            ]

        slots = tuple(str(slot) for slot in entry.get("pattern_slots", ()))
        match = _authoring_pattern(surface, slots).fullmatch(text)
        if match is None:
            return []
        greedy_match = _authoring_pattern(surface, slots, greedy_partitions=True).fullmatch(text)
        if greedy_match is None or any(match.span(slot) != greedy_match.span(slot) for slot in slots):
            return []
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        captures = [
            {
                "slot": slot,
                "start": match.start(slot),
                "end": match.end(slot),
                "text": match.group(slot),
            }
            for slot in slots
        ]
        return [({"start": start, "end": end, "text": text[start:end]}, captures)]

    def propose(self, text: object) -> dict[str, Any]:
        """Match current approvals in memory and return recognition-only candidates."""
        if not isinstance(text, str):
            raise PlayerLexValidationError("proposal text must be a string")
        if len(text) > MAX_PROPOSAL_CHARS:
            raise PlayerLexValidationError(f"proposal text must be at most {MAX_PROPOSAL_CHARS} characters")
        if any(unicodedata.category(char) == "Cs" for char in text):
            raise PlayerLexValidationError("proposal text contains unsupported surrogate code points")
        if not text.strip():
            return {"schema": PROPOSAL_SCHEMA, "match_count": 0, "matches": [], "refused": []}

        matches: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        entries = self.list_entries()
        folded_view = (
            _folded_text_with_source_spans(text)
            if any(entry["status"] != "corrupt" and entry["kind"] in {"name", "alias"} for entry in entries)
            else None
        )
        for entry in entries:
            if entry["status"] == "corrupt":
                refused.append(
                    {
                        "entry_id": entry["entry_id"],
                        "lex_id": entry["lex_id"],
                        "reason": "corrupt_entry",
                    }
                )
                continue
            surfaces = self._matches(entry, text, folded_view)
            if not surfaces:
                continue
            if entry["status"] != "current":
                refused.append(
                    {
                        "entry_id": entry["entry_id"],
                        "lex_id": entry["lex_id"],
                        "reason": (
                            "meaning_fingerprint_changed" if entry["status"] == "stale" else "concept_missing"
                        ),
                        "approved_meaning_fingerprint": entry["concept"]["meaning_fingerprint"],
                        "current_meaning_fingerprint": entry["current_meaning_fingerprint"],
                    }
                )
                continue

            for source_span, captures in surfaces:
                matches.append(
                    {
                        "schema": CANDIDATE_SCHEMA,
                        "entry_id": entry["entry_id"],
                        "entry_revision": entry["provenance"]["approval_revision"],
                        "lex_id": entry["lex_id"],
                        "kind": entry["kind"],
                        "approved_surface": entry["surface"],
                        "concept_label": entry["concept_label"],
                        "concept": _json_copy(entry["concept"]),
                        "source_span": source_span,
                        "captures": captures,
                        "provenance": _json_copy(entry["provenance"]),
                        "recognized": True,
                        "authorized": False,
                        "executable": False,
                        "requires_context_binding": True,
                    }
                )

        matches.sort(
            key=lambda item: (
                item["source_span"]["start"],
                -(item["source_span"]["end"] - item["source_span"]["start"]),
                item["lex_id"],
                item["concept"]["concept_id"],
                item["entry_id"],
            )
        )
        refused.sort(key=lambda item: item["entry_id"])
        return {
            "schema": PROPOSAL_SCHEMA,
            "match_count": len(matches),
            "matches": matches,
            "refused": refused,
        }
