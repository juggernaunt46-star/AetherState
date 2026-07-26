"""Declarative Store-core schema migrations.

The Store owns these objects; optional domain schema remains outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from .schema_migrations import (
    SchemaMigration,
    _normalize_sql_outside_quotes,
    sqlite_ascii_fold,
)


STORE_CORE_DOMAIN = "store-core"
STORE_CORE_VERSION = 1
STORE_CHAT_LINEAGE_VERSION = 4
STORE_CORE_FAILURE = "store_core_schema_invalid"
STORE_CHAT_LINEAGE_FAILURE = "store_chat_lineage_invalid"


# Each reviewed DDL statement remains independently executable under BEGIN IMMEDIATE.
STORE_CORE_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, external_id TEXT UNIQUE, anchor_hash TEXT, frontend TEXT DEFAULT 'unknown', active_branch TEXT, frozen INTEGER DEFAULT 0, created_at REAL, last_seen REAL)",
    "CREATE TABLE IF NOT EXISTS branches(branch_id TEXT PRIMARY KEY, session_id TEXT, parent_branch TEXT, forked_at INTEGER, status TEXT DEFAULT 'live', head_turn INTEGER DEFAULT -1)",
    "CREATE TABLE IF NOT EXISTS turns(branch_id TEXT, turn_index INTEGER, user_hash TEXT, assistant_hash TEXT, chain_hash TEXT, klass TEXT, gen_type TEXT, swipe_count INTEGER DEFAULT 0, settled INTEGER DEFAULT 0, extraction TEXT DEFAULT 'pending', PRIMARY KEY(branch_id, turn_index))",
    "CREATE TABLE IF NOT EXISTS ops_journal(id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_lo INTEGER, turn_hi INTEGER, ops TEXT, source TEXT, ts REAL)",
    "CREATE TABLE IF NOT EXISTS effect_receipts(branch_id TEXT, effect_id TEXT, turn_index INTEGER, family TEXT, target TEXT, direction TEXT, delta INTEGER, payload_hash TEXT, owner TEXT, source TEXT, status TEXT DEFAULT 'committed', ts REAL, PRIMARY KEY(branch_id, effect_id))",
    "CREATE INDEX IF NOT EXISTS idx_effect_claim ON effect_receipts(branch_id, turn_index, family, target, direction, owner)",
    "CREATE TABLE IF NOT EXISTS mechanic_settlement_receipts(branch_id TEXT, settlement_ref TEXT, turn_index INTEGER, contract_id TEXT, frame_ref TEXT, meaning_ref TEXT, outcome TEXT, outcome_quality TEXT, requirement_fingerprint TEXT, request_fingerprint TEXT, accepted_group_fingerprint TEXT, receipt_fingerprint TEXT, receipt_json TEXT, source TEXT, status TEXT DEFAULT 'committed', ts REAL, PRIMARY KEY(branch_id, settlement_ref))",
    "CREATE INDEX IF NOT EXISTS idx_mechanic_settlement_turn ON mechanic_settlement_receipts(branch_id, turn_index, contract_id)",
    "CREATE TABLE IF NOT EXISTS semantic_bootstrap_proofs(session_id TEXT PRIMARY KEY, branch_id TEXT UNIQUE, turn_index INTEGER, proof_fingerprint TEXT, post_ledger_hash TEXT, journal_high_water_after INTEGER, proof_json TEXT, committed_at REAL)",
    "CREATE TABLE IF NOT EXISTS creator_seed_receipts(session_id TEXT, seed_fingerprint TEXT, branch_id TEXT, seed_json TEXT, world_source_json TEXT, player_source_json TEXT, world_requested INTEGER, player_requested INTEGER, world_id TEXT, player_id TEXT, admitted_turn INTEGER, applied_ops INTEGER, migrated INTEGER DEFAULT 0, receipt_fingerprint TEXT, committed_at REAL, PRIMARY KEY(session_id, seed_fingerprint))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_creator_seed_receipt_session ON creator_seed_receipts(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_creator_seed_receipt_fingerprint ON creator_seed_receipts(seed_fingerprint)",
    "CREATE TABLE IF NOT EXISTS chat_core_receipts(session_id TEXT PRIMARY KEY, branch_id TEXT, journal_op_id INTEGER UNIQUE, core_fingerprint TEXT, world_fingerprint TEXT, card_envelope_fingerprint TEXT, character_actor_id TEXT, persona_actor_id TEXT, admitted_turn INTEGER, admission_fingerprint TEXT, receipt_fingerprint TEXT, committed_at REAL)",
    "CREATE TABLE IF NOT EXISTS chat_continuity_seed_receipts(session_id TEXT, record_fingerprint TEXT, branch_id TEXT, family TEXT, record_json TEXT, admitted_turn INTEGER, journal_op_id INTEGER, receipt_fingerprint TEXT, committed_at REAL, lifecycle_source TEXT DEFAULT '', response_occurrence_id TEXT DEFAULT '', PRIMARY KEY(session_id, record_fingerprint))",
    "CREATE INDEX IF NOT EXISTS idx_chat_continuity_seed_receipt_branch ON chat_continuity_seed_receipts(branch_id, admitted_turn, family)",
    "CREATE TABLE IF NOT EXISTS chat_user_text_receipts(branch_id TEXT, turn_index INTEGER, source_message_fingerprint TEXT, journal_op_id INTEGER, committed_at REAL, PRIMARY KEY(branch_id, turn_index, source_message_fingerprint))",
    "CREATE INDEX IF NOT EXISTS idx_chat_user_text_receipts_turn ON chat_user_text_receipts(branch_id, turn_index, source_message_fingerprint)",
    "CREATE TABLE IF NOT EXISTS claim_records(branch_id TEXT, claim_id TEXT, origin_branch TEXT, session_id TEXT, world_id TEXT, turn_index INTEGER, source TEXT, fingerprint TEXT, record_json TEXT, status TEXT DEFAULT 'committed', ts REAL, PRIMARY KEY(branch_id, claim_id))",
    "CREATE INDEX IF NOT EXISTS idx_claim_records_turn ON claim_records(branch_id, turn_index, source)",
    "CREATE TABLE IF NOT EXISTS world_event_records(branch_id TEXT, event_id TEXT, origin_branch TEXT, session_id TEXT, world_id TEXT, turn_index INTEGER, kind TEXT, relation_target TEXT, source TEXT, fingerprint TEXT, record_json TEXT, status TEXT DEFAULT 'committed', ts REAL, PRIMARY KEY(branch_id, event_id))",
    "CREATE INDEX IF NOT EXISTS idx_world_event_records_turn ON world_event_records(branch_id, turn_index, kind)",
    "CREATE TABLE IF NOT EXISTS checkpoints(branch_id TEXT, turn_index INTEGER, state TEXT, PRIMARY KEY(branch_id, turn_index))",
    "CREATE TABLE IF NOT EXISTS branch_msgs(branch_id TEXT, pos INTEGER, role TEXT, content_hash TEXT, chain_hash TEXT, PRIMARY KEY(branch_id, pos))",
    "CREATE TABLE IF NOT EXISTS slices(session_id TEXT PRIMARY KEY, for_turn INTEGER, components TEXT, created REAL)",
    "CREATE TABLE IF NOT EXISTS turn_texts(branch_id TEXT, turn_index INTEGER, user_text TEXT, assistant_text TEXT, PRIMARY KEY(branch_id, turn_index))",
    "CREATE TABLE IF NOT EXISTS caps(base_url TEXT, model TEXT, rung INTEGER, probed_at REAL, failures INTEGER DEFAULT 0, native TEXT DEFAULT '', anyof INTEGER DEFAULT -1, PRIMARY KEY(base_url, model))",
    "CREATE TABLE IF NOT EXISTS discovery(branch_id TEXT, name TEXT, turns TEXT DEFAULT '[]', status TEXT DEFAULT 'counting', PRIMARY KEY(branch_id, name))",
    "CREATE TABLE IF NOT EXISTS memories(memory_id TEXT PRIMARY KEY, session_id TEXT, branch_id TEXT, tier TEXT, text TEXT, participants TEXT DEFAULT '[]', location_id TEXT, tags TEXT DEFAULT '[]', importance INTEGER DEFAULT 3, created_turn INTEGER, last_accessed_turn INTEGER DEFAULT 0, parent_id TEXT, scene_index INTEGER DEFAULT 0, embedding_ref INTEGER, source_journal_op_refs TEXT DEFAULT '[]')",
    "CREATE INDEX IF NOT EXISTS idx_memories_branch ON memories(branch_id, parent_id)",
    "CREATE TABLE IF NOT EXISTS recall(session_id TEXT PRIMARY KEY, for_turn INTEGER, lines TEXT, created REAL)",
    "CREATE TABLE IF NOT EXISTS recall_records(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, branch_id TEXT, for_turn INTEGER, source_turn INTEGER, lifecycle_source TEXT, response_occurrence_id TEXT DEFAULT '', source_message_fingerprint TEXT DEFAULT '', journal_op_refs TEXT DEFAULT '[]', lines TEXT DEFAULT '[]', created REAL)",
    "CREATE INDEX IF NOT EXISTS idx_recall_records_lineage ON recall_records(branch_id, for_turn, source_turn, lifecycle_source, response_occurrence_id)",
    "CREATE TABLE IF NOT EXISTS lint(id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_index INTEGER, rule TEXT, severity TEXT, subjects TEXT, detail TEXT, evidence TEXT, ts REAL)",
    "CREATE INDEX IF NOT EXISTS idx_lint_branch ON lint(branch_id, turn_index)",
    "CREATE TABLE IF NOT EXISTS hints(id INTEGER PRIMARY KEY AUTOINCREMENT, session_ext TEXT, event TEXT, message_index INTEGER, ts REAL)",
    "CREATE TABLE IF NOT EXISTS notes(session_id TEXT PRIMARY KEY, for_turn INTEGER, text TEXT, created REAL)",
    "CREATE TABLE IF NOT EXISTS embeddings(memory_id TEXT PRIMARY KEY, vec BLOB, dim INTEGER)",
    "CREATE TABLE IF NOT EXISTS director(id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_index INTEGER, beat_id TEXT, scene_index INTEGER, ts REAL)",
    "CREATE INDEX IF NOT EXISTS idx_director_branch ON director(branch_id, turn_index)",
    "CREATE TABLE IF NOT EXISTS presets(preset_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, doc TEXT, created REAL, updated REAL, UNIQUE(kind, name))",
)

STORE_MIGRATION_COLUMNS = (
    ("caps", "native", "TEXT DEFAULT ''"), ("caps", "anyof", "INTEGER DEFAULT -1"),
    ("sessions", "genesis", "TEXT DEFAULT ''"), ("sessions", "genesis_epoch", "INTEGER DEFAULT 0"),
    ("creator_seed_receipts", "receipt_fingerprint", "TEXT DEFAULT ''"), ("sessions", "mode", "TEXT DEFAULT 'enriched'"),
    ("sessions", "label", "TEXT DEFAULT ''"), ("sessions", "narrator_speaker", "TEXT DEFAULT ''"),
    ("sessions", "experience_mode", "TEXT DEFAULT ''"), ("sessions", "experience_mode_source", "TEXT DEFAULT ''"),
    ("sessions", "experience_mode_locked_turn", "INTEGER"), ("sessions", "core_fingerprint", "TEXT DEFAULT ''"),
    ("sessions", "character_actor_id", "TEXT DEFAULT ''"), ("sessions", "persona_actor_id", "TEXT DEFAULT ''"),
    ("world_event_records", "source", "TEXT DEFAULT ''"), ("turns", "accepted_response_occurrence_id", "TEXT DEFAULT ''"),
    ("ops_journal", "lifecycle_source", "TEXT DEFAULT ''"), ("ops_journal", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("effect_receipts", "lifecycle_source", "TEXT DEFAULT ''"), ("effect_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("mechanic_settlement_receipts", "lifecycle_source", "TEXT DEFAULT ''"), ("mechanic_settlement_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("claim_records", "lifecycle_source", "TEXT DEFAULT ''"), ("claim_records", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("world_event_records", "lifecycle_source", "TEXT DEFAULT ''"), ("world_event_records", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("chat_continuity_seed_receipts", "lifecycle_source", "TEXT DEFAULT ''"), ("chat_continuity_seed_receipts", "response_occurrence_id", "TEXT DEFAULT ''"),
    ("memories", "visibility", "TEXT DEFAULT ''"), ("memories", "scoped_actors", "TEXT DEFAULT '[]'"),
    ("memories", "journal_op_id", "INTEGER"), ("memories", "journal_op_ref", "TEXT DEFAULT ''"),
    ("memories", "source_message_fingerprint", "TEXT DEFAULT ''"), ("memories", "lifecycle_source", "TEXT DEFAULT ''"),
    ("memories", "response_occurrence_id", "TEXT DEFAULT ''"), ("memories", "source_journal_op_refs", "TEXT DEFAULT '[]'"),
)

STORE_LIFECYCLE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ops_journal_lifecycle ON ops_journal(branch_id, lifecycle_source, response_occurrence_id, turn_hi)",
    "CREATE INDEX IF NOT EXISTS idx_claim_records_lifecycle ON claim_records(branch_id, lifecycle_source, response_occurrence_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_memories_lifecycle ON memories(branch_id, lifecycle_source, response_occurrence_id, created_turn)",
    "CREATE INDEX IF NOT EXISTS idx_effect_receipts_lifecycle ON effect_receipts(branch_id, lifecycle_source, response_occurrence_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_mechanic_settlement_receipts_lifecycle ON mechanic_settlement_receipts(branch_id, lifecycle_source, response_occurrence_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_world_event_records_lifecycle ON world_event_records(branch_id, lifecycle_source, response_occurrence_id, turn_index)",
    "CREATE INDEX IF NOT EXISTS idx_chat_continuity_seed_receipts_lifecycle ON chat_continuity_seed_receipts(branch_id, lifecycle_source, response_occurrence_id, admitted_turn)",
)

CHAT_LINEAGE_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS chat_accepted_message_receipts(branch_id TEXT, turn_index INTEGER, lifecycle_source TEXT, response_occurrence_id TEXT DEFAULT '', source_message_fingerprint TEXT, receipt_fingerprint TEXT, committed_at REAL, PRIMARY KEY(branch_id, turn_index, lifecycle_source))",
    "CREATE INDEX IF NOT EXISTS idx_chat_accepted_message_receipts_response ON chat_accepted_message_receipts(branch_id, turn_index, lifecycle_source, response_occurrence_id)",
)

_CORE_TABLES = frozenset({
    "sessions", "branches", "turns", "ops_journal", "effect_receipts", "mechanic_settlement_receipts",
    "semantic_bootstrap_proofs", "creator_seed_receipts", "chat_core_receipts", "chat_continuity_seed_receipts",
    "chat_user_text_receipts", "claim_records", "world_event_records", "checkpoints", "branch_msgs", "slices",
    "turn_texts", "caps", "discovery", "memories", "recall", "recall_records", "lint", "hints", "notes",
    "embeddings", "director", "presets",
})
_LINEAGE_TABLE = "chat_accepted_message_receipts"
_KNOWN_INDEXES = frozenset(re.findall(r"(?:INDEX IF NOT EXISTS) ([A-Za-z_][A-Za-z0-9_]*)", "\n".join(STORE_CORE_STATEMENTS + STORE_LIFECYCLE_INDEXES + CHAT_LINEAGE_STATEMENTS)))
_LINEAGE_OWNED_NAMES = frozenset(
    {_LINEAGE_TABLE, "idx_chat_accepted_message_receipts_response"}
)
_CORE_OWNED_NAMES = (_CORE_TABLES | _KNOWN_INDEXES) - _LINEAGE_OWNED_NAMES
_STORE_OWNED_NAMES = _CORE_OWNED_NAMES | _LINEAGE_OWNED_NAMES
STORE_CORE_HISTORICAL_PROJECTION_HASHES = {
    "1.0.0-release-2cd07ef": "80a846feb8259b3f3229e4aceca7949fbc6808063bb005d04f58b16cb1d1b3aa",
    "1.1.0-release-ed63e38": "c8f91b337ac4fbd944f19c1083679d317390a9bc9f02bf296105920aa45d8131",
    "1.22.0-release-9091614": "9ee5ccb509520cca648f16dd62412caa517df44653fba8d7df33b87ff8c075e0",
    "1.23.0-release-1f4aad0": "481afde03725392a0886831138f476d9e0c5b4cb50d0f7e1acb26fa20b18d2eb",
    "1.23.0-final-34dfe8f": "20993f869f60ddc3833a4b18fb62ec101563490b0095b4f35fa580f2fe956a9e",
    "1.24.0-release-fdf71e2": "e8e8fbc7834083bd5dae8d373ba814e22f2055588771b27299940b1a9489f22b",
}
STORE_CORE_CURRENT_PROJECTION_HASH = "c9adc3593ed5aa509a078fbccab888cc8c243801b1266c0387bac331bda08004"
def _lifecycle_index_name(statement: str) -> str:
    match = re.search(r"INDEX IF NOT EXISTS ([A-Za-z_][A-Za-z0-9_]*)", statement)
    if match is None:
        raise ValueError("reviewed lifecycle index is malformed")
    return match.group(1)


STORE_LIFECYCLE_INDEX_NAMES = tuple(_lifecycle_index_name(statement) for statement in STORE_LIFECYCLE_INDEXES)
_FIXTURE_LAYOUTS = (
    ("1.0.0-release-2cd07ef", "idx_director_branch idx_lint_branch idx_memories_branch|branch_msgs branches caps checkpoints director discovery embeddings hints lint memories notes ops_journal recall sessions slices turn_texts turns", (0, 1, 2, 5, 6)),
    ("1.1.0-release-ed63e38", "idx_director_branch idx_lint_branch idx_memories_branch|branch_msgs branches caps checkpoints director discovery embeddings hints lint memories notes ops_journal presets recall sessions slices turn_texts turns", (0, 1, 2, 5, 6)),
    ("1.22.0-release-9091614", "idx_director_branch idx_effect_claim idx_lint_branch idx_mechanic_settlement_turn idx_memories_branch|branch_msgs branches caps checkpoints director discovery effect_receipts embeddings hints lint mechanic_settlement_receipts memories notes ops_journal presets recall semantic_bootstrap_proofs sessions slices turn_texts turns", (0, 1, 2, 5, 6, 7)),
    ("1.23.0-release-1f4aad0", "idx_claim_records_turn idx_director_branch idx_effect_claim idx_lint_branch idx_mechanic_settlement_turn idx_memories_branch idx_world_event_records_turn|branch_msgs branches caps checkpoints claim_records director discovery effect_receipts embeddings hints lint mechanic_settlement_receipts memories notes ops_journal presets recall semantic_bootstrap_proofs sessions slices turn_texts turns world_event_records", (0, 1, 2, 5, 6, 7, 14)),
    ("1.23.0-final-34dfe8f", "idx_claim_records_turn idx_creator_seed_receipt_fingerprint idx_creator_seed_receipt_session idx_director_branch idx_effect_claim idx_lint_branch idx_mechanic_settlement_turn idx_memories_branch idx_world_event_records_turn|branch_msgs branches caps checkpoints claim_records creator_seed_receipts director discovery effect_receipts embeddings hints lint mechanic_settlement_receipts memories notes ops_journal presets recall semantic_bootstrap_proofs sessions slices turn_texts turns world_event_records", (0, 1, 2, 3, 4, 5, 6, 7, 14)),
    ("1.24.0-release-fdf71e2", "idx_chat_continuity_seed_receipt_branch idx_chat_continuity_seed_receipts_lifecycle idx_chat_user_text_receipts_turn idx_claim_records_lifecycle idx_claim_records_turn idx_creator_seed_receipt_fingerprint idx_creator_seed_receipt_session idx_director_branch idx_effect_claim idx_effect_receipts_lifecycle idx_lint_branch idx_mechanic_settlement_receipts_lifecycle idx_mechanic_settlement_turn idx_memories_branch idx_memories_lifecycle idx_ops_journal_lifecycle idx_recall_records_lineage idx_world_event_records_lifecycle idx_world_event_records_turn|branch_msgs branches caps chat_continuity_seed_receipts chat_core_receipts chat_user_text_receipts checkpoints claim_records creator_seed_receipts director discovery effect_receipts embeddings hints lint mechanic_settlement_receipts memories notes ops_journal presets recall recall_records semantic_bootstrap_proofs sessions slices turn_texts turns world_event_records", tuple(range(36))),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _owned_schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema ORDER BY type, name"
        )
        if not sqlite_ascii_fold(row[1]).startswith("sqlite_")
        and (
            sqlite_ascii_fold(row[1]) in _CORE_OWNED_NAMES
            or sqlite_ascii_fold(row[2]) in _CORE_TABLES
        )
    )


def _owned_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple((str(kind), str(name), str(table)) for kind, name, table, _ in _owned_schema_rows(connection))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return any(
        str(row[0]) == "table" and str(row[1]) == name
        for row in connection.execute("SELECT type, name FROM main.sqlite_schema")
    )


def _main_owned_case_collision(connection: sqlite3.Connection) -> bool:
    owned = set(_STORE_OWNED_NAMES)
    tables = set(_CORE_TABLES) | {_LINEAGE_TABLE}
    return any(
        (
            sqlite_ascii_fold(row[1]) in owned
            and str(row[1]) != sqlite_ascii_fold(row[1])
        )
        or (
            sqlite_ascii_fold(row[2]) in tables
            and str(row[2]) != sqlite_ascii_fold(row[2])
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name FROM main.sqlite_schema"
        )
    )


def _temp_owned_collision(connection: sqlite3.Connection, names: set[str]) -> bool:
    folded = {sqlite_ascii_fold(name) for name in names}
    return any(
        sqlite_ascii_fold(row[0]) in folded
        or sqlite_ascii_fold(row[1]) in folded
        for row in connection.execute(
            "SELECT name, tbl_name FROM sqlite_temp_schema"
        )
    )


def _schema_sql(
    connection: sqlite3.Connection, object_type: str, name: str
) -> object | None:
    for row in connection.execute(
        "SELECT type, name, sql FROM main.sqlite_schema"
    ):
        if str(row[0]) == object_type and str(row[1]) == name:
            return row[2]
    return None


def _named_index_columns(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
    return tuple(
        str(row[2]) for row in connection.execute(
            f'PRAGMA main.index_xinfo("{name}")'
        ) if int(row[5]) == 1
    )


def _layout_keys(layout: str) -> frozenset[tuple[str, str]]:
    indexes, tables = layout.split("|", 1)
    return frozenset([("index", value) for value in indexes.split()] + [("table", value) for value in tables.split()])


def _normalized_sql(sql: object) -> object:
    return None if sql is None else _normalize_sql_outside_quotes(str(sql))


def _strip_added_sql(sql: object, triples: tuple[tuple[str, str, str], ...]) -> object:
    normalized = _normalized_sql(sql)
    if not isinstance(normalized, str):
        return normalized
    for _table, column, declaration in triples:
        normalized = re.sub(rf',\s*(?:"{re.escape(column)}"|{re.escape(column)})\s+{re.escape(_normalize_sql_outside_quotes(declaration))}(?=\s*[,\)])', "", normalized)
    return normalized


def _migration_xinfo(triple: tuple[str, str, str]) -> tuple[object, ...]:
    _table, column, declaration = triple
    normalized = _normalize_sql_outside_quotes(declaration)
    if normalized == "integer":
        return (column, "INTEGER", 0, None, 0, 0)
    type_name, _, default = normalized.partition(" default ")
    return (column, type_name.upper(), 0, default or None, 0, 0)


def _projection(connection: sqlite3.Connection, keys: frozenset[tuple[str, str]] | None = None, added: tuple[tuple[str, str, str], ...] = ()) -> tuple[tuple[object, ...], ...]:
    out: list[tuple[object, ...]] = []
    additions: dict[str, list[tuple[str, str, str]]] = {}
    for triple in added:
        additions.setdefault(triple[0], []).append(triple)
    for kind, name, table, sql in _owned_schema_rows(connection):
        if keys is not None and (str(kind), str(name)) not in keys:
            continue
        if kind != "table":
            out.append(("schema", kind, name, table, _normalized_sql(sql)))
            continue
        table_added = tuple(additions.get(str(name), ()))
        out.append(("schema", kind, name, table, _strip_added_sql(sql, table_added)))
        known_added = {triple[1]: triple for triple in table_added}
        for row in connection.execute(f"PRAGMA main.table_xinfo({_quote_identifier(str(name))})"):
            values = tuple(row)
            matched = known_added.get(str(values[1]))
            if matched is not None:
                if tuple(values[1:]) != _migration_xinfo(matched):
                    return ()
                continue
            out.append(("table_xinfo", name, *values))
        for row in sorted((tuple(row) for row in connection.execute(f"PRAGMA main.index_list({_quote_identifier(str(name))})")), key=lambda item: str(item[1])):
            index_name = str(row[1])
            if keys is not None and ("index", index_name) not in keys and not index_name.startswith("sqlite_autoindex"):
                continue
            out.append(("index_list", name, index_name, *row[2:]))
            for index_row in connection.execute(f"PRAGMA main.index_xinfo({_quote_identifier(index_name)})"):
                out.append(("index_xinfo", index_name, *tuple(index_row)))
    return tuple(out)


def _projection_hash(projection: tuple[tuple[object, ...], ...]) -> str:
    return hashlib.sha256(repr(projection).encode("utf-8")).hexdigest()


def _current_extra_is_exact(kind: str, name: str, sql: object) -> bool:
    statement = next((item for item in STORE_CORE_STATEMENTS + STORE_LIFECYCLE_INDEXES if re.search(rf"(?:TABLE|INDEX) IF NOT EXISTS {re.escape(name)}(?:\(|\s)", item)), None)
    if kind not in {"table", "index"} or statement is None:
        return False
    expected = _normalize_sql_outside_quotes(statement.replace(" IF NOT EXISTS", ""))
    if kind == "table":
        additions = tuple(
            triple for triple in STORE_MIGRATION_COLUMNS
            if triple[0] == name
            and re.search(rf"(?:^|[,(])\s*{re.escape(triple[1])}\s", expected) is None
        )
        return _strip_added_sql(sql, additions) == expected
    return _normalized_sql(sql) == expected


def _core_applicable(connection: sqlite3.Connection) -> bool:
    if _main_owned_case_collision(connection) or _temp_owned_collision(
        connection, set(_STORE_OWNED_NAMES)
    ):
        return False
    objects = _owned_schema_rows(connection)
    if not objects or _projection_hash(_projection(connection)) == STORE_CORE_CURRENT_PROJECTION_HASH:
        return True
    for name, layout, present in _FIXTURE_LAYOUTS:
        keys = _layout_keys(layout)
        extras = [row for row in objects if (str(row[0]), str(row[1])) not in keys]
        if any(not _current_extra_is_exact(str(kind), str(object_name), sql) for kind, object_name, _table, sql in extras):
            continue
        added = tuple(triple for index, triple in enumerate(STORE_MIGRATION_COLUMNS) if index not in present)
        candidate = _projection(connection, keys, added)
        if candidate and _projection_hash(candidate) == STORE_CORE_HISTORICAL_PROJECTION_HASHES[name]:
            return True
    return False


def _core_current(connection: sqlite3.Connection) -> bool:
    if _main_owned_case_collision(connection) or _temp_owned_collision(
        connection, set(_STORE_OWNED_NAMES)
    ):
        return False
    if _projection_hash(_projection(connection)) == STORE_CORE_CURRENT_PROJECTION_HASH:
        return True
    if not all(_table_exists(connection, name) for name in _CORE_TABLES):
        return False
    if any(
        column not in {str(row[1]) for row in connection.execute(f"PRAGMA main.table_xinfo({_quote_identifier(table)})")}
        for table, column, _declaration in STORE_MIGRATION_COLUMNS
    ):
        return False
    names = {str(row[1]) for row in _owned_schema_rows(connection) if row[0] == "index"}
    return set(STORE_LIFECYCLE_INDEX_NAMES) <= names and _core_applicable(connection)


def _transform_core(connection: sqlite3.Connection) -> None:
    if not _core_applicable(connection):
        raise ValueError("invalid core schema")
    for statement in STORE_CORE_STATEMENTS:
        connection.execute(statement)
    for table, column, declaration in STORE_MIGRATION_COLUMNS:
        names = {str(row[1]) for row in connection.execute(f'PRAGMA main.table_xinfo("{table}")')}
        if column not in names:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')
    for statement in STORE_LIFECYCLE_INDEXES:
        connection.execute(statement)


def _receipt_fingerprint(authority: dict[str, object]) -> str:
    payload = json.dumps(authority, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(b"aetherstate-chat-accepted-message-receipt/1\0" + payload.encode("utf-8")).hexdigest()


def _insert_receipt(connection: sqlite3.Connection, branch_id: str, turn: int, lifecycle: str, response_id: str, fingerprint: str) -> None:
    authority = {"schema": "aetherstate-chat-accepted-message-receipt/1", "branch_id": branch_id, "turn_index": turn, "lifecycle_source": lifecycle, "response_occurrence_id": response_id, "source_message_fingerprint": fingerprint}
    receipt = _receipt_fingerprint(authority)
    connection.execute("INSERT OR IGNORE INTO chat_accepted_message_receipts(branch_id, turn_index, lifecycle_source, response_occurrence_id, source_message_fingerprint, receipt_fingerprint, committed_at) VALUES(?,?,?,?,?,?,?)", (branch_id, turn, lifecycle, response_id, fingerprint, receipt, time.time()))
    prior = connection.execute("SELECT response_occurrence_id, source_message_fingerprint, receipt_fingerprint FROM chat_accepted_message_receipts WHERE branch_id=? AND turn_index=? AND lifecycle_source=?", (branch_id, turn, lifecycle)).fetchone()
    if prior is None or tuple(prior) != (response_id, fingerprint, receipt):
        raise ValueError("receipt conflict")


def _backfill_receipts(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT t.branch_id, t.turn_index, t.accepted_response_occurrence_id, x.user_text, x.assistant_text FROM turns AS t JOIN turn_texts AS x ON x.branch_id=t.branch_id AND x.turn_index=t.turn_index WHERE COALESCE(t.accepted_response_occurrence_id, '')<>''"):
        branch_id, turn, response_id, user_text, assistant_text = str(row[0]), int(row[1]), str(row[2] or ""), str(row[3] or ""), str(row[4] or "")
        if re.fullmatch(r"response:[0-9a-f]{64}", response_id) is None:
            continue
        if user_text:
            _insert_receipt(connection, branch_id, turn, "user_text", "", "sha256:" + hashlib.sha256(user_text.encode("utf-8")).hexdigest())
        if assistant_text:
            fingerprint = "sha256:" + hashlib.sha256(assistant_text.encode("utf-8")).hexdigest()
            for lifecycle in ("assistant_response", "deferred_extraction"):
                _insert_receipt(connection, branch_id, turn, lifecycle, response_id, fingerprint)


def _memory_lineage_candidates(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[tuple[int, int], ...] | None:
    """Return every matching journal operation, or None for malformed legacy state."""
    try:
        branch_id, turn = str(row[1] or ""), int(row[6])
        lifecycle, response_id = str(row[10] or ""), str(row[11] or "")
        wanted_participants = sorted(str(value) for value in json.loads(row[3] or "[]"))
        wanted_tags = sorted(str(value) for value in json.loads(row[4] or "[]"))
        candidates: list[tuple[int, int]] = []
        for journal in connection.execute(
            "SELECT id, ops FROM ops_journal WHERE branch_id=? AND turn_hi=? "
            "AND lifecycle_source=? AND COALESCE(response_occurrence_id, '')=?",
            (branch_id, turn, lifecycle, response_id),
        ):
            try:
                operations = json.loads(journal[1])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(operations, list):
                continue
            for index, operation in enumerate(operations):
                if (
                    isinstance(operation, dict)
                    and operation.get("op") == "memory_event"
                    and str(operation.get("text") or "") == str(row[2] or "")
                    and sorted(str(value) for value in operation.get("participants") or [])
                    == wanted_participants
                    and sorted(str(value) for value in operation.get("tags") or []) == wanted_tags
                    and int(operation.get("importance", 3)) == int(row[5])
                ):
                    candidates.append((int(journal[0]), index))
        return tuple(candidates)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _memory_lineage_proof(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[int, int, str] | None:
    """Return the sole journal operation and receipt authority for one legacy memory."""
    candidates = _memory_lineage_candidates(connection, row)
    if candidates is None or len(candidates) != 1:
        return None
    receipt = connection.execute(
        "SELECT source_message_fingerprint FROM chat_accepted_message_receipts "
        "WHERE branch_id=? AND turn_index=? AND lifecycle_source=?",
        (str(row[1] or ""), int(row[6]), str(row[10] or "")),
    ).fetchone()
    if receipt is None:
        return None
    journal_id, index = candidates[0]
    return journal_id, index, str(receipt[0] or "")


def _memory_lineage_is_ambiguous(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    candidates = _memory_lineage_candidates(connection, row)
    return candidates is not None and len(candidates) > 1


def _memory_lineage_rows(connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
    return tuple(connection.execute(
        "SELECT memory_id, branch_id, text, participants, tags, importance, created_turn, "
        "journal_op_id, journal_op_ref, source_message_fingerprint, lifecycle_source, "
        "response_occurrence_id FROM memories WHERE lifecycle_source IN "
        "('user_text','assistant_response','deferred_extraction')"
    ))


def _backfill_memory_lineage(connection: sqlite3.Connection) -> None:
    for row in _memory_lineage_rows(connection):
        proof = _memory_lineage_proof(connection, row)
        if proof is None:
            if _memory_lineage_is_ambiguous(connection, row):
                connection.execute(
                    "UPDATE memories SET journal_op_id=NULL, journal_op_ref='', "
                    "source_message_fingerprint='' WHERE memory_id=?",
                    (str(row[0]),),
                )
            continue
        journal_id, index, fingerprint = proof
        connection.execute(
            "UPDATE memories SET source_message_fingerprint=?, journal_op_id=?, journal_op_ref=? "
            "WHERE memory_id=?",
            (fingerprint, journal_id, f"{journal_id}:{index}", str(row[0])),
        )


def _lineage_schema_current(connection: sqlite3.Connection) -> bool:
    if _main_owned_case_collision(connection) or _temp_owned_collision(
        connection, set(_LINEAGE_OWNED_NAMES)
    ):
        return False
    if not _table_exists(connection, _LINEAGE_TABLE):
        return False
    columns = tuple(
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]), int(row[6]))
        for row in connection.execute(f'PRAGMA main.table_xinfo("{_LINEAGE_TABLE}")')
    )
    if columns != (
        ("branch_id", "TEXT", 0, None, 1, 0),
        ("turn_index", "INTEGER", 0, None, 2, 0),
        ("lifecycle_source", "TEXT", 0, None, 3, 0),
        ("response_occurrence_id", "TEXT", 0, "''", 0, 0),
        ("source_message_fingerprint", "TEXT", 0, None, 0, 0),
        ("receipt_fingerprint", "TEXT", 0, None, 0, 0),
        ("committed_at", "REAL", 0, None, 0, 0),
    ):
        return False
    table_sql = _schema_sql(connection, "table", _LINEAGE_TABLE)
    if table_sql is None or _normalized_sql(table_sql) != _normalized_sql(
        CHAT_LINEAGE_STATEMENTS[0].replace(" IF NOT EXISTS", "")
    ):
        return False
    index_name = "idx_chat_accepted_message_receipts_response"
    index_sql = _schema_sql(connection, "index", index_name)
    if index_sql is None or _normalized_sql(index_sql) != _normalized_sql(
        CHAT_LINEAGE_STATEMENTS[1].replace(" IF NOT EXISTS", "")
    ):
        return False
    if tuple(tuple(row) for row in connection.execute(
        f"PRAGMA main.index_list({_quote_identifier(_LINEAGE_TABLE)})"
    )) != (
        (0, index_name, 0, "c", 0),
        (1, f"sqlite_autoindex_{_LINEAGE_TABLE}_1", 1, "pk", 0),
    ):
        return False
    return tuple(tuple(row) for row in connection.execute(
        f"PRAGMA main.index_xinfo({_quote_identifier(index_name)})"
    )) == (
        (0, 0, "branch_id", 0, "BINARY", 1),
        (1, 1, "turn_index", 0, "BINARY", 1),
        (2, 2, "lifecycle_source", 0, "BINARY", 1),
        (3, 3, "response_occurrence_id", 0, "BINARY", 1),
        (4, -1, None, 0, "BINARY", 0),
    )


def _lineage_data_current(connection: sqlite3.Connection) -> bool:
    if not _lineage_schema_current(connection):
        return False
    for row in connection.execute("SELECT t.branch_id, t.turn_index, t.accepted_response_occurrence_id, x.user_text, x.assistant_text FROM turns AS t JOIN turn_texts AS x ON x.branch_id=t.branch_id AND x.turn_index=t.turn_index WHERE COALESCE(t.accepted_response_occurrence_id, '')<>''"):
        branch_id, turn, response_id, user_text, assistant_text = str(row[0]), int(row[1]), str(row[2] or ""), str(row[3] or ""), str(row[4] or "")
        if re.fullmatch(r"response:[0-9a-f]{64}", response_id) is None:
            continue
        expected: list[tuple[str, str, str]] = []
        if user_text:
            expected.append(("user_text", "", "sha256:" + hashlib.sha256(user_text.encode("utf-8")).hexdigest()))
        if assistant_text:
            fingerprint = "sha256:" + hashlib.sha256(assistant_text.encode("utf-8")).hexdigest()
            expected.extend((("assistant_response", response_id, fingerprint), ("deferred_extraction", response_id, fingerprint)))
        for lifecycle, receipt_response, fingerprint in expected:
            authority = {"schema": "aetherstate-chat-accepted-message-receipt/1", "branch_id": branch_id, "turn_index": turn, "lifecycle_source": lifecycle, "response_occurrence_id": receipt_response, "source_message_fingerprint": fingerprint}
            row = connection.execute("SELECT response_occurrence_id, source_message_fingerprint, receipt_fingerprint FROM chat_accepted_message_receipts WHERE branch_id=? AND turn_index=? AND lifecycle_source=?", (branch_id, turn, lifecycle)).fetchone()
            if row is None or tuple(row) != (receipt_response, fingerprint, _receipt_fingerprint(authority)):
                return False
    for row in _memory_lineage_rows(connection):
        proof = _memory_lineage_proof(connection, row)
        if proof is None:
            if _memory_lineage_is_ambiguous(connection, row) and tuple(row[7:10]) != (None, "", ""):
                return False
            continue
        journal_id, index, fingerprint = proof
        if tuple(row[7:10]) != (journal_id, f"{journal_id}:{index}", fingerprint):
            return False
    return True


def _lineage_current(connection: sqlite3.Connection) -> bool:
    return _lineage_data_current(connection)


def _lineage_applicable(connection: sqlite3.Connection) -> bool:
    return _core_current(connection) and not _lineage_current(connection)


def _transform_lineage(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_chat_accepted_message_receipts_response")
    for statement in CHAT_LINEAGE_STATEMENTS:
        connection.execute(statement)
    _backfill_receipts(connection)
    _backfill_memory_lineage(connection)


def store_schema_migrations() -> tuple[SchemaMigration, ...]:
    return (
        SchemaMigration(STORE_CORE_VERSION, "store-core-1.24-baseline", STORE_CORE_DOMAIN, STORE_CORE_FAILURE, _core_applicable, _core_current, _transform_core, _core_current),
        SchemaMigration(STORE_CHAT_LINEAGE_VERSION, "store-chat-lineage-1.24-baseline", STORE_CORE_DOMAIN, STORE_CHAT_LINEAGE_FAILURE, _lineage_applicable, _lineage_current, _transform_lineage, _lineage_current),
    )
