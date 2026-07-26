"""Content-free SQLite schema fixture and inspection helpers for tests."""
from __future__ import annotations

import sqlite3
import re
import json
from dataclasses import dataclass, replace
from pathlib import Path

from typing import TYPE_CHECKING

from aetherstate.schema_migrations import _normalize_sql_outside_quotes

if TYPE_CHECKING:
    from aetherstate.store import Store


_LEDGER_NAMESPACE_INVALID = "schema_history_ledger_namespace_invalid"


@dataclass(frozen=True)
class HistoricalMeaning:
    """Hand-written synthetic meaning that must survive a supported upgrade."""

    baseline_id: str
    shape: str
    session_id: str = "history-session"
    branch_id: str = "history-branch"
    checkpoint_state: tuple[tuple[str, object], ...] = (("hp", 9), ("mood", "steady"))
    replay_state: tuple[tuple[str, object], ...] = (("hp", 7), ("mood", "steady"))
    effect_id: str | None = None
    settlement_ref: str | None = None
    claim_id: str | None = None
    event_id: str | None = None
    chat_receipt: str | None = None
    world_id: str | None = None
    selector: "HistoricalSelector | None" = None
    records: tuple["HistoricalRecord", ...] = ()


@dataclass(frozen=True)
class HistoricalRecord:
    """One literal carried row and its stable identity selector."""

    table: str
    identity: tuple[str, ...]
    values: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class HistoricalSelector:
    """One terminal lifecycle deliberately carried by the 1.24 full-start fixture."""

    branch_id: str
    lifecycle_key: str
    payload: bytes


_SUPPORTED_BASELINES = frozenset({
    "1.0.0-release-2cd07ef",
    "1.1.0-release-ed63e38",
    "1.22.0-release-9091614",
    "1.23.0-release-1f4aad0",
    "1.23.0-final-34dfe8f",
    "1.24.0-release-fdf71e2",
})


def seed_supported_history(
    connection: sqlite3.Connection, baseline_id: str, shape: str
) -> HistoricalMeaning:
    """Seed only literal synthetic rows that the tracked baseline can represent."""
    if baseline_id not in _SUPPORTED_BASELINES or shape not in {"core", "full-start"}:
        raise ValueError("unsupported historical fixture")
    expected = HistoricalMeaning(
        baseline_id=baseline_id,
        shape=shape,
        effect_id="history-effect" if _table_exists(connection, "effect_receipts") else None,
        settlement_ref="history-settlement" if _table_exists(connection, "mechanic_settlement_receipts") else None,
        claim_id="history-claim" if _table_exists(connection, "claim_records") else None,
        event_id="history-event" if _table_exists(connection, "world_event_records") else None,
        chat_receipt="history-chat-receipt" if _table_exists(connection, "chat_core_receipts") else None,
        world_id="world_11111111111111111111111111111111"
        if _table_exists(connection, "worldlex_world_lineages") else None,
    )
    _insert_known(connection, "sessions", {
        "session_id": expected.session_id, "external_id": "history-external",
        "anchor_hash": "history-anchor", "frontend": "history",
        "active_branch": expected.branch_id, "frozen": 0,
        "created_at": 10.0, "last_seen": 11.0,
    })
    _insert_known(connection, "branches", {
        "branch_id": expected.branch_id, "session_id": expected.session_id,
        "parent_branch": None, "forked_at": 0, "status": "live", "head_turn": 1,
    })
    _insert_known(connection, "turns", {
        "branch_id": expected.branch_id, "turn_index": 0,
        "user_hash": "history-user", "assistant_hash": "history-assistant",
        "chain_hash": "history-chain", "klass": "normal", "gen_type": "normal",
        "swipe_count": 0, "settled": 1, "extraction": "done",
    })
    _insert_known(connection, "checkpoints", {
        "branch_id": expected.branch_id, "turn_index": 0,
        "state": json.dumps(dict(expected.checkpoint_state), separators=(",", ":")),
    })
    _insert_known(connection, "ops_journal", {
        "branch_id": expected.branch_id, "turn_lo": 0, "turn_hi": 1,
        "ops": '[{"hp":7}]', "source": "history", "ts": 12.0,
    })
    _insert_known(connection, "branch_msgs", {
        "branch_id": expected.branch_id, "pos": 0, "role": "user",
        "content_hash": "history-user", "chain_hash": "history-chain",
    })
    _insert_known(connection, "effect_receipts", {
        "branch_id": expected.branch_id, "effect_id": "history-effect", "turn_index": 0,
        "family": "history", "target": "history-target", "direction": "down", "delta": -2,
        "payload_hash": "history-payload", "owner": "history-owner", "source": "history",
        "status": "committed", "ts": 13.0,
    })
    _insert_known(connection, "mechanic_settlement_receipts", {
        "branch_id": expected.branch_id, "settlement_ref": "history-settlement", "turn_index": 0,
        "contract_id": "history-contract", "frame_ref": "history-frame", "meaning_ref": "history-meaning",
        "outcome": "accepted", "outcome_quality": "exact", "requirement_fingerprint": "history-requirement",
        "request_fingerprint": "history-request", "accepted_group_fingerprint": "history-group",
        "receipt_fingerprint": "history-receipt", "receipt_json": "{}", "source": "history",
        "status": "committed", "ts": 14.0,
    })
    _insert_known(connection, "claim_records", {
        "branch_id": expected.branch_id, "claim_id": "history-claim", "origin_branch": expected.branch_id,
        "session_id": expected.session_id, "world_id": "", "turn_index": 0, "source": "history",
        "fingerprint": "history-claim-fingerprint", "record_json": "{}", "status": "committed", "ts": 15.0,
    })
    _insert_known(connection, "world_event_records", {
        "branch_id": expected.branch_id, "event_id": "history-event", "origin_branch": expected.branch_id,
        "session_id": expected.session_id, "world_id": "", "turn_index": 0, "kind": "history",
        "relation_target": "", "source": "history", "fingerprint": "history-event-fingerprint",
        "record_json": "{}", "status": "committed", "ts": 16.0,
    })
    _insert_known(connection, "chat_core_receipts", {
        "session_id": expected.session_id, "branch_id": expected.branch_id, "journal_op_id": 1,
        "core_fingerprint": "history-chat-receipt", "world_fingerprint": "", "card_envelope_fingerprint": "",
        "character_actor_id": "history-character", "persona_actor_id": "history-persona",
        "admitted_turn": 0, "admission_fingerprint": "history-admission",
        "receipt_fingerprint": "history-chat-receipt", "committed_at": 17.0,
    })
    _insert_known(connection, "worldlex_world_lineages", {
        "world_id": "world_11111111111111111111111111111111", "parent_world_id": None,
        "created_at": 18.0,
    })
    _insert_known(connection, "worldlex_capability_definitions", {
        "world_id": "world_11111111111111111111111111111111",
        "definition_id": "history-definition", "revision": 1,
        "fingerprint": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "parent_fingerprint": None, "kind": "skill", "owner_scope": "world",
        "owner_id": "world_11111111111111111111111111111111",
        "schema": "capability-definition/1", "compiler_version": "capability-compiler/1",
        "record_json": "{}", "created_at": 19.0,
    })
    _insert_known(connection, "chat_continuity_seed_receipts", {
        "session_id": expected.session_id, "record_fingerprint": "history-continuity",
        "branch_id": expected.branch_id, "family": "relationship", "record_json": "{}",
        "admitted_turn": 0, "journal_op_id": 1, "receipt_fingerprint": "history-continuity",
        "committed_at": 24.0, "lifecycle_source": "history", "response_occurrence_id": "history-response",
    })
    _insert_known(connection, "chat_user_text_receipts", {
        "branch_id": expected.branch_id, "turn_index": 0,
        "source_message_fingerprint": "history-user", "journal_op_id": 1, "committed_at": 25.0,
    })
    _insert_known(connection, "chat_accepted_message_receipts", {
        "branch_id": expected.branch_id, "turn_index": 0,
        "lifecycle_source": "history-accepted", "response_occurrence_id": "history-response",
        "source_message_fingerprint": "history-user",
        "receipt_fingerprint": "history-accepted-receipt", "committed_at": 26.0,
    })
    selector = _seed_terminal_selector_lifecycle(connection) if _table_exists(
        connection, "semantic_turn_lifecycles"
    ) else None
    connection.commit()
    expected = replace(expected, selector=selector)
    return replace(expected, records=_literal_records(connection, expected))


def read_historical_meaning(store: "Store", expected: HistoricalMeaning) -> HistoricalMeaning:
    """Read the literal seeded meaning after startup without deriving an expectation."""
    session = store.db.execute(
        "SELECT session_id, active_branch FROM sessions WHERE session_id=?", (expected.session_id,)
    ).fetchone()
    checkpoint = store.db.execute(
        "SELECT state FROM checkpoints WHERE branch_id=? AND turn_index=0", (expected.branch_id,)
    ).fetchone()
    if session is None or checkpoint is None or tuple(session) != (expected.session_id, expected.branch_id):
        raise AssertionError("historical common identity changed")
    if tuple(sorted(json.loads(checkpoint[0]).items())) != expected.checkpoint_state:
        raise AssertionError("historical checkpoint changed")
    state = store.state_at(
        expected.branch_id, 1,
        lambda prior, operations: {**prior, **{key: value for op in operations for key, value in op.items()}},
    )
    if tuple(sorted(state.items())) != expected.replay_state:
        raise AssertionError("historical replay changed")
    for record in expected.records:
        names = ", ".join(f'"{name}"' for name, _value in record.values)
        where = " AND ".join(f'"{name}"=?' for name in record.identity)
        values = dict(record.values)
        rows = store.db.execute(
            f'SELECT {names} FROM "{record.table}" WHERE {where}',
            tuple(values[name] for name in record.identity),
        ).fetchall()
        actual = tuple(tuple(row) for row in rows)
        wanted = (tuple(value for _name, value in record.values),)
        if actual != wanted:
            raise AssertionError(f"{record.table} literal history changed")
    return expected


def _literal_records(
    connection: sqlite3.Connection, expected: HistoricalMeaning
) -> tuple[HistoricalRecord, ...]:
    specs = (
        ("sessions", ("session_id",), {
            "session_id": expected.session_id, "external_id": "history-external",
            "anchor_hash": "history-anchor", "frontend": "history",
            "active_branch": expected.branch_id, "frozen": 0, "created_at": 10.0, "last_seen": 11.0,
        }),
        ("branches", ("branch_id",), {
            "branch_id": expected.branch_id, "session_id": expected.session_id,
            "parent_branch": None, "forked_at": 0, "status": "live", "head_turn": 1,
        }),
        ("turns", ("branch_id", "turn_index"), {
            "branch_id": expected.branch_id, "turn_index": 0, "user_hash": "history-user",
            "assistant_hash": "history-assistant", "chain_hash": "history-chain", "klass": "normal",
            "gen_type": "normal", "swipe_count": 0, "settled": 1, "extraction": "done",
        }),
        ("checkpoints", ("branch_id", "turn_index"), {
            "branch_id": expected.branch_id, "turn_index": 0,
            "state": json.dumps(dict(expected.checkpoint_state), separators=(",", ":")),
        }),
        ("ops_journal", ("branch_id", "turn_lo", "turn_hi", "source"), {
            "branch_id": expected.branch_id, "turn_lo": 0, "turn_hi": 1,
            "ops": '[{"hp":7}]', "source": "history", "ts": 12.0,
        }),
        ("branch_msgs", ("branch_id", "pos"), {
            "branch_id": expected.branch_id, "pos": 0, "role": "user",
            "content_hash": "history-user", "chain_hash": "history-chain",
        }),
        ("effect_receipts", ("branch_id", "effect_id"), {
            "branch_id": expected.branch_id, "effect_id": "history-effect", "turn_index": 0,
            "family": "history", "target": "history-target", "direction": "down", "delta": -2,
            "payload_hash": "history-payload", "owner": "history-owner", "source": "history",
            "status": "committed", "ts": 13.0,
        }),
        ("mechanic_settlement_receipts", ("branch_id", "settlement_ref"), {
            "branch_id": expected.branch_id, "settlement_ref": "history-settlement", "turn_index": 0,
            "contract_id": "history-contract", "frame_ref": "history-frame", "meaning_ref": "history-meaning",
            "outcome": "accepted", "outcome_quality": "exact", "requirement_fingerprint": "history-requirement",
            "request_fingerprint": "history-request", "accepted_group_fingerprint": "history-group",
            "receipt_fingerprint": "history-receipt", "receipt_json": "{}", "source": "history",
            "status": "committed", "ts": 14.0,
        }),
        ("claim_records", ("branch_id", "claim_id"), {
            "branch_id": expected.branch_id, "claim_id": "history-claim", "origin_branch": expected.branch_id,
            "session_id": expected.session_id, "world_id": "", "turn_index": 0, "source": "history",
            "fingerprint": "history-claim-fingerprint", "record_json": "{}", "status": "committed", "ts": 15.0,
        }),
        ("world_event_records", ("branch_id", "event_id"), {
            "branch_id": expected.branch_id, "event_id": "history-event", "origin_branch": expected.branch_id,
            "session_id": expected.session_id, "world_id": "", "turn_index": 0, "kind": "history",
            "relation_target": "", "source": "history", "fingerprint": "history-event-fingerprint",
            "record_json": "{}", "status": "committed", "ts": 16.0,
        }),
        ("chat_core_receipts", ("session_id",), {
            "session_id": expected.session_id, "branch_id": expected.branch_id, "journal_op_id": 1,
            "core_fingerprint": "history-chat-receipt", "world_fingerprint": "", "card_envelope_fingerprint": "",
            "character_actor_id": "history-character", "persona_actor_id": "history-persona",
            "admitted_turn": 0, "admission_fingerprint": "history-admission",
            "receipt_fingerprint": "history-chat-receipt", "committed_at": 17.0,
        }),
        ("worldlex_world_lineages", ("world_id",), {
            "world_id": "world_11111111111111111111111111111111", "parent_world_id": None, "created_at": 18.0,
        }),
        ("worldlex_capability_definitions", ("world_id", "definition_id", "revision"), {
            "world_id": "world_11111111111111111111111111111111", "definition_id": "history-definition", "revision": 1,
            "fingerprint": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
            "parent_fingerprint": None, "kind": "skill", "owner_scope": "world",
            "owner_id": "world_11111111111111111111111111111111", "schema": "capability-definition/1",
            "compiler_version": "capability-compiler/1", "record_json": "{}", "created_at": 19.0,
        }),
        ("chat_continuity_seed_receipts", ("session_id", "record_fingerprint"), {
            "session_id": expected.session_id, "record_fingerprint": "history-continuity",
            "branch_id": expected.branch_id, "family": "relationship", "record_json": "{}", "admitted_turn": 0,
            "journal_op_id": 1, "receipt_fingerprint": "history-continuity", "committed_at": 24.0,
            "lifecycle_source": "history", "response_occurrence_id": "history-response",
        }),
        ("chat_user_text_receipts", ("branch_id", "turn_index", "source_message_fingerprint"), {
            "branch_id": expected.branch_id, "turn_index": 0, "source_message_fingerprint": "history-user",
            "journal_op_id": 1, "committed_at": 25.0,
        }),
        ("chat_accepted_message_receipts", ("branch_id", "turn_index", "lifecycle_source"), {
            "branch_id": expected.branch_id, "turn_index": 0,
            "lifecycle_source": "history-accepted", "response_occurrence_id": "history-response",
            "source_message_fingerprint": "history-user",
            "receipt_fingerprint": "history-accepted-receipt", "committed_at": 26.0,
        }),
    )
    records: list[HistoricalRecord] = []
    for table, identity, values in specs:
        if not _table_exists(connection, table):
            continue
        columns = {str(row[1]) for row in connection.execute(f'PRAGMA main.table_info("{table}")')}
        selected = tuple((name, value) for name, value in values.items() if name in columns)
        records.append(HistoricalRecord(table, identity, selected))
    if expected.selector is not None:
        for table, values in _SELECTOR_ROWS:
            identity = _SELECTOR_SEMANTIC_IDENTITIES.get(table)
            if identity is None or not _table_exists(connection, table):
                continue
            columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA main.table_info("{table}")')
            }
            selected = tuple(
                (name, value) for name, value in values.items() if name in columns
            )
            records.append(HistoricalRecord(table, identity, selected))
    return tuple(records)


_SELECTOR_SESSION_ID = "history-selector-session"
_SELECTOR_BRANCH_ID = "history-selector-branch"
_SELECTOR_LIFECYCLE_KEY = "sha256:9a981735af04ecf9f923d6b6de5aa1e96817b0d39b2dfd21cab37b3ad37b69a6"
_SELECTOR_PAYLOAD = b'{"text":"historical selector fallback"}'
_SELECTOR_ENVELOPE_JSON = r'''{"attempt":{"index":0,"kind":"initial","ledger_anchor_hash":"sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be","request_hash":"sha256:29ae263dfe49fc841ee3323f28a8224ab6c93022b214eecba223b812881c5d90"},"candidate_declaration_fingerprint":"sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945","candidate_declarations":[],"delivery_proof":{"artifact_kind":"fallback","claim_projections":{"expected":{"mode":"exact_json","schema":"narration-claim-projection/1","value":{"claims":[]}},"ledger":{"mode":"exact_json","schema":"narration-claim-projection/1","value":{"claims":[]}},"observed":{"mode":"exact_json","schema":"narration-claim-projection/1","value":{"claims":[]}}},"comparisons":{"mode":"exact_json","observed_equals_expected":true,"observed_matches_ledger":true},"delivery":{"artifact_kind":"fallback","content_type":"application/json","logical_message_id":"sha256:a593e1dd05301f25a84d762f35ffa7a897f4ab3e2f23bb41729fdf8ea4d9d0fb","renderer_hash":"sha256:3f637e566d22bb737175e25ec0ec9b1df1426aeba3883484aba94881c9090e60","selected_artifact_digest":"sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0","visible_hash":"sha256:7ce9b16c125033295af00b2e4541b057772622371687156b1ba80e6aace618d6","wire_hash":"sha256:b57f74f5991c235cf0c0bdff588e9af93c71ab8eb2d920d4dbe300e2b93a6b06"},"expected_graph":{"claims":[]},"gate_receipt":{"artifact_kind":"fallback","comparison_mode":"exact_json","decision":"accept","expected_graph_fingerprint":"sha256:9ab10f0f885c9c17b9bb160e685330f8e0891ef8f13bba2ae0d86091e62bb844","expected_projection_fingerprint":"sha256:3c54627a7649601b6b9c32a07615167348b47a3fb7b2d6c3f428e018632167bf","ledger_graph_fingerprint":"sha256:9ab10f0f885c9c17b9bb160e685330f8e0891ef8f13bba2ae0d86091e62bb844","ledger_projection_fingerprint":"sha256:3c54627a7649601b6b9c32a07615167348b47a3fb7b2d6c3f428e018632167bf","ledger_root_hash":"sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be","observed_graph_fingerprint":"sha256:9ab10f0f885c9c17b9bb160e685330f8e0891ef8f13bba2ae0d86091e62bb844","observed_projection_fingerprint":"sha256:3c54627a7649601b6b9c32a07615167348b47a3fb7b2d6c3f428e018632167bf","proof_basis_fingerprint":"sha256:39e73fbbf6be44d8539098406041428b9017c2aa1734b4216981c21ac0ca2e87","reason_code":"truth_match","receipt_basis_fingerprint":"sha256:a5a838375fda2527635d0b8a6af3eafd2ea75656de668cde953ae092133002c9","receipt_fingerprint":"sha256:ee5277279fabbf935344bd2b6056cb2a3a2e0984f2f6441a64c2087a13380b0a","schema":"narration-truth-gate-receipt/1","selected_artifact_digest":"sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0"},"ledger_graph":{"claims":[]},"ledger_root_hash":"sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be","observed_graph":{"claims":[]},"proof_basis_fingerprint":"sha256:39e73fbbf6be44d8539098406041428b9017c2aa1734b4216981c21ac0ca2e87","proof_fingerprint":"sha256:75d67fa4fd2459586bba0ebbe7b3f0c2bec6b887c5829ddac750ef76d67346b0","schema":"narration-delivery-proof/1","verdict":"pass"},"diagnostics":{},"effect_fingerprint":"sha256:bc2850db88b86d7a7705d8f855567babf62d529c95c29fa67a35a9c3c2b813ab","effects":[{"effect_id":"history-selector-effect","occurrence_id":"history-selector-occurrence"}],"envelope_fingerprint":"sha256:abfbc97ac13afe701b534370548600219c68278879b1cc4a3ba6a75e674acdf7","gate":{"decision":"fallback","reason_code":"truth_match","receipt_fingerprint":"sha256:ee5277279fabbf935344bd2b6056cb2a3a2e0984f2f6441a64c2087a13380b0a"},"ledger":{"mechanics_post_hash":"sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be","pre_hash":"sha256:ad4c8a8429c463ff0ea09e5e2434d33e69d98fae0d18dbe5180808309b7ecd5b","terminal_post_hash":"sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be"},"lifecycle_key":"sha256:9a981735af04ecf9f923d6b6de5aa1e96817b0d39b2dfd21cab37b3ad37b69a6","lineage":{"source_envelope_fingerprint":null,"source_lifecycle_key":null},"occurrence_fingerprint":"sha256:6dcf4639423a8cca1c64bccecb810f57929a4546ef325df10c3a5ea69793aeef","occurrences":[{"occurrence_id":"history-selector-occurrence","settlement_ref":"history-selector-settlement"}],"output":{"accepted_hash":null,"accepted_size":null,"content_type":"application/json","fallback_hash":"sha256:b57f74f5991c235cf0c0bdff588e9af93c71ab8eb2d920d4dbe300e2b93a6b06","fallback_size":39,"selected":"fallback","selected_artifact_digest":"sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0"},"pending_intent":{"consumed_intent_id":null,"next_intent_id":null},"pre_mutation_key":{"accepted_head_hash":"d9dd515aa35c3411","accepted_prefix_pos":1,"branch_id":"history-selector-branch","lifecycle_key":"sha256:9a981735af04ecf9f923d6b6de5aa1e96817b0d39b2dfd21cab37b3ad37b69a6","pending_intent_fingerprint":"sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b","player_input_hash":"0898e8bfa2cd309d","pre_ledger_hash":"sha256:ad4c8a8429c463ff0ea09e5e2434d33e69d98fae0d18dbe5180808309b7ecd5b","schema":"semantic-turn-key/2","semantic_contract_version":"semantic-contract/history-selector-1","session_id":"history-selector-session","turn_index":0},"runtime":{"config_fingerprint":"sha256:804de9f5015a6a93ef94662a64cd0886d3bde4e0fe873c74aa08f5bc2255514d","engine_version":"history-selector-engine/1","rng_fingerprint":"sha256:3211fcb8d1b25f1c997b475d82e616785678375cd6da2c5d83aa0c8524ad4d85"},"schema":"semantic-turn-envelope/1"}'''

_SELECTOR_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    ("sessions", {"session_id": _SELECTOR_SESSION_ID, "external_id": "history-selector-external", "active_branch": _SELECTOR_BRANCH_ID, "frontend": "history-selector", "created_at": 30.0, "last_seen": 30.0}),
    ("branches", {"branch_id": _SELECTOR_BRANCH_ID, "session_id": _SELECTOR_SESSION_ID, "parent_branch": None, "forked_at": 0, "status": "live", "head_turn": 0}),
    ("branch_msgs", {"branch_id": _SELECTOR_BRANCH_ID, "pos": 0, "role": "assistant", "content_hash": "f05a16594f7c2967", "chain_hash": "d9dd515aa35c3411"}),
    ("branch_msgs", {"branch_id": _SELECTOR_BRANCH_ID, "pos": 1, "role": "user", "content_hash": "0898e8bfa2cd309d", "chain_hash": "5e0d6e30dc04e4f6"}),
    ("turns", {"branch_id": _SELECTOR_BRANCH_ID, "turn_index": 0, "user_hash": "0898e8bfa2cd309d", "assistant_hash": "f05a16594f7c2967", "chain_hash": "5e0d6e30dc04e4f6", "klass": "normal", "gen_type": "normal", "swipe_count": 0, "settled": 1, "extraction": "done", "accepted_response_occurrence_id": ""}),
    ("semantic_turn_lifecycles", {"lifecycle_key": _SELECTOR_LIFECYCLE_KEY, "session_id": _SELECTOR_SESSION_ID, "branch_id": _SELECTOR_BRANCH_ID, "turn_index": 0, "accepted_prefix_pos": 1, "accepted_head_hash": "d9dd515aa35c3411", "player_input_hash": "0898e8bfa2cd309d", "semantic_contract_version": "semantic-contract/history-selector-1", "key_json": "{\"accepted_head_hash\":\"d9dd515aa35c3411\",\"accepted_prefix_pos\":1,\"branch_id\":\"history-selector-branch\",\"lifecycle_key\":\"sha256:9a981735af04ecf9f923d6b6de5aa1e96817b0d39b2dfd21cab37b3ad37b69a6\",\"pending_intent_fingerprint\":\"sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b\",\"player_input_hash\":\"0898e8bfa2cd309d\",\"pre_ledger_hash\":\"sha256:ad4c8a8429c463ff0ea09e5e2434d33e69d98fae0d18dbe5180808309b7ecd5b\",\"schema\":\"semantic-turn-key/2\",\"semantic_contract_version\":\"semantic-contract/history-selector-1\",\"session_id\":\"history-selector-session\",\"turn_index\":0}", "status": "committed", "initial_request_hash": "sha256:29ae263dfe49fc841ee3323f28a8224ab6c93022b214eecba223b812881c5d90", "active_attempt_index": 0, "base_envelope_fingerprint": "sha256:abfbc97ac13afe701b534370548600219c68278879b1cc4a3ba6a75e674acdf7", "terminal_envelope_fingerprint": "sha256:abfbc97ac13afe701b534370548600219c68278879b1cc4a3ba6a75e674acdf7", "pre_ledger_hash": "sha256:ad4c8a8429c463ff0ea09e5e2434d33e69d98fae0d18dbe5180808309b7ecd5b", "mechanics_post_ledger_hash": "sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be", "post_ledger_hash": "sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be", "occurrence_fingerprint": "sha256:6dcf4639423a8cca1c64bccecb810f57929a4546ef325df10c3a5ea69793aeef", "effect_fingerprint": "sha256:bc2850db88b86d7a7705d8f855567babf62d529c95c29fa67a35a9c3c2b813ab", "rng_fingerprint": "sha256:3211fcb8d1b25f1c997b475d82e616785678375cd6da2c5d83aa0c8524ad4d85", "config_fingerprint": "sha256:804de9f5015a6a93ef94662a64cd0886d3bde4e0fe873c74aa08f5bc2255514d", "engine_version": "history-selector-engine/1", "consumed_intent_id": None, "next_intent_id": None, "source_lifecycle_key": None, "created_at": 1785041580.0174704, "updated_at": 1785041580.0182624}),
    ("semantic_turn_attempts", {"lifecycle_key": _SELECTOR_LIFECYCLE_KEY, "attempt_index": 0, "attempt_kind": "initial", "request_hash": "sha256:29ae263dfe49fc841ee3323f28a8224ab6c93022b214eecba223b812881c5d90", "ledger_anchor_hash": "sha256:0277b3e44a9d1b81d340c9c0ee03d1c9cf87f0d79861e2b5a031de50aa00f5be", "status": "fallback_ready", "refusal_code": None, "fallback_envelope_fingerprint": "sha256:abfbc97ac13afe701b534370548600219c68278879b1cc4a3ba6a75e674acdf7", "fallback_envelope_json": _SELECTOR_ENVELOPE_JSON, "fallback_bytes": _SELECTOR_PAYLOAD, "fallback_hash": "sha256:b57f74f5991c235cf0c0bdff588e9af93c71ab8eb2d920d4dbe300e2b93a6b06", "terminal_envelope_fingerprint": None, "terminal_envelope_json": None, "accepted_bytes": None, "accepted_hash": None, "logical_message_id": "sha256:a593e1dd05301f25a84d762f35ffa7a897f4ab3e2f23bb41729fdf8ea4d9d0fb", "selected_artifact_digest": "sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0", "created_at": 1785041580.0174704, "updated_at": 1785041580.0182624}),
    ("semantic_turn_delivery_claims", {"lifecycle_key": _SELECTOR_LIFECYCLE_KEY, "attempt_index": 0, "logical_message_id": "sha256:a593e1dd05301f25a84d762f35ffa7a897f4ab3e2f23bb41729fdf8ea4d9d0fb", "artifact_digest": "sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0", "status": "claimed", "claimed_at": 1785041580.3265216}),
    ("semantic_turn_delivery_completions", {"lifecycle_key": _SELECTOR_LIFECYCLE_KEY, "attempt_index": 0, "logical_message_id": "sha256:a593e1dd05301f25a84d762f35ffa7a897f4ab3e2f23bb41729fdf8ea4d9d0fb", "artifact_digest": "sha256:17bbe5ef77e398e38df1890b41cf4d58e3f73f2987330767f20cf51af1fa82d0", "status": "completed", "completed_at": 1785041580.327399}),
)

_SELECTOR_SEMANTIC_IDENTITIES = {
    "semantic_turn_lifecycles": ("lifecycle_key",),
    "semantic_turn_attempts": ("lifecycle_key", "attempt_index"),
    "semantic_turn_delivery_claims": ("lifecycle_key", "attempt_index"),
    "semantic_turn_delivery_completions": ("lifecycle_key", "attempt_index"),
}


def _seed_terminal_selector_lifecycle(
    connection: sqlite3.Connection,
) -> HistoricalSelector:
    """Insert the fully frozen pre-upgrade selector rows without runtime construction."""
    for table, values in _SELECTOR_ROWS:
        _insert_known(connection, table, values)
    return HistoricalSelector(_SELECTOR_BRANCH_ID, _SELECTOR_LIFECYCLE_KEY, _SELECTOR_PAYLOAD)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _insert_known(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """Use table metadata only to select a predeclared literal fixture statement."""
    if not _table_exists(connection, table):
        return
    columns = {str(row[1]) for row in connection.execute(f'PRAGMA main.table_info("{table}")')}
    selected = [(column, value) for column, value in values.items() if column in columns]
    if not selected:
        return
    names = ", ".join(f'"{column}"' for column, _value in selected)
    marks = ", ".join("?" for _column, _value in selected)
    connection.execute(
        f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
        tuple(value for _column, value in selected),
    )


def rebuild_schema_fixture(path: Path, fixture: Path) -> sqlite3.Connection:
    """Build one fresh database from top-level CREATE statements in a tracked fixture."""
    if fixture.suffixes[-2:] != [".schema", ".sql"] or not fixture.is_file():
        raise ValueError("schema fixture is invalid")
    if path.exists():
        raise ValueError("schema fixture destination is not fresh")
    connection = sqlite3.connect(path)
    try:
        statements = _create_statements(fixture.read_text(encoding="utf-8"))
        if not statements:
            raise ValueError("schema fixture is empty")
        for statement in statements:
            connection.execute(statement)
        connection.row_factory = sqlite3.Row
        return connection
    except Exception:
        connection.close()
        if path.exists():
            path.unlink()
        raise


def schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """Return deterministic schema metadata without selecting data rows."""
    snapshot: list[tuple[object, ...]] = []
    objects = tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE substr(name, 1, 7) <> 'sqlite_' ORDER BY type, name"
        )
    )
    for object_type, name, table_name, sql in objects:
        snapshot.append(("schema", object_type, name, table_name, _normalize_sql(sql)))
    table_names = tuple(name for object_type, name, _, _ in objects if object_type == "table")
    for table_name in table_names:
        quoted_table = _quote_identifier(table_name)
        for row in connection.execute(f"PRAGMA main.table_xinfo({quoted_table})"):
            snapshot.append(("table_xinfo", table_name, *tuple(row)))
        indexes = tuple(connection.execute(f"PRAGMA main.index_list({quoted_table})"))
        for row in indexes:
            snapshot.append(("index_list", table_name, *tuple(row)))
            quoted_index = _quote_identifier(row[1])
            for index_row in connection.execute(f"PRAGMA main.index_xinfo({quoted_index})"):
                snapshot.append(("index_xinfo", row[1], *tuple(index_row)))
    return tuple(snapshot)


def ledger_rows(connection: sqlite3.Connection) -> tuple[tuple[int, str, str], ...]:
    """Read only ordered migration identities from the migration ledger."""
    collision = connection.execute(
        "SELECT 1 FROM sqlite_temp_schema WHERE name = ? OR tbl_name = ? LIMIT 1",
        ("aetherstate_schema_migrations", "aetherstate_schema_migrations"),
    ).fetchone()
    if collision is not None:
        raise ValueError(_LEDGER_NAMESPACE_INVALID)
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT version, name, domain FROM main.aetherstate_schema_migrations ORDER BY version"
        )
    )


def _create_statements(text: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for character in text:
        pending += character
        if character != ";" or not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if not statement:
            continue
        if not re.match(r"CREATE\s+", _without_leading_comments(statement), re.IGNORECASE):
            raise ValueError("schema fixture contains a non-CREATE statement")
        statements.append(statement)
    if _without_leading_comments(pending).strip():
        raise ValueError("schema fixture contains an incomplete statement")
    return tuple(statements)


def _without_leading_comments(statement: str) -> str:
    remaining = statement.lstrip()
    while remaining.startswith("--") or remaining.startswith("/*"):
        if remaining.startswith("--"):
            newline = remaining.find("\n")
            remaining = "" if newline < 0 else remaining[newline + 1 :].lstrip()
        else:
            closing = remaining.find("*/")
            if closing < 0:
                return remaining
            remaining = remaining[closing + 2 :].lstrip()
    return remaining


def _normalize_sql(sql: object) -> object:
    return None if sql is None else _normalize_sql_outside_quotes(str(sql))


def _quote_identifier(value: object) -> str:
    return '"' + str(value).replace('"', '""') + '"'
