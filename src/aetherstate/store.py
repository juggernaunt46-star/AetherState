"""SQLite spine (planning/02 SS2, 03 SS3.3): sessions/branches/turns + ops journal + checkpoints.

state_at(branch, turn) = nearest checkpoint <= turn + ordered replay of journaled ops through a
caller-supplied reducer. The same primitive later drives edit-forks, swipe rollback, the replay
harness, and the inspector scrubber. Phase 2 supplies the real reducer; the spine is semantics-free.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .experience import (
    ExperienceBinding,
    ExperienceModeLocked,
    normalize_experience_mode,
)
from .database_schema import database_schema_migrations
from .schema_migrations import SchemaMigrationRunner
from .worldlex_store import WorldLexStore

_CREATOR_SEED_RECEIPT_SCHEMA = "aetherstate-creator-seed-receipt/1"
_CHAT_CORE_ADMISSION_DOMAIN = b"aetherstate-chat-core-admission/1\0"
_CHAT_CORE_RECEIPT_DOMAIN = b"aetherstate-chat-core-receipt/1\0"
_CHAT_CONTINUITY_SEED_RECEIPT_DOMAIN = (
    b"aetherstate-chat-continuity-seed-receipt/1\0"
)
_CHAT_ACCEPTED_MESSAGE_RECEIPT_DOMAIN = (
    b"aetherstate-chat-accepted-message-receipt/1\0"
)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("receipt value must be finite JSON") from exc


def _chat_accepted_message_receipt_fingerprint(authority: dict) -> str:
    return "sha256:" + hashlib.sha256(
        _CHAT_ACCEPTED_MESSAGE_RECEIPT_DOMAIN
        + _canonical_json(authority).encode("utf-8")
    ).hexdigest()


def _chat_admission_fingerprint(card_envelope_fingerprint: str,
                                persona_actor_id: str) -> str:
    payload = f"{card_envelope_fingerprint}\0{persona_actor_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(_CHAT_CORE_ADMISSION_DOMAIN + payload).hexdigest()


def _chat_receipt_fingerprint(authority: dict) -> str:
    return "sha256:" + hashlib.sha256(
        _CHAT_CORE_RECEIPT_DOMAIN + _canonical_json(authority).encode("utf-8")
    ).hexdigest()


def _chat_continuity_seed_receipt_fingerprint(authority: dict) -> str:
    return "sha256:" + hashlib.sha256(
        _CHAT_CONTINUITY_SEED_RECEIPT_DOMAIN
        + _canonical_json(authority).encode("utf-8")
    ).hexdigest()


def _creator_seed_receipt_authority(
    *,
    session_id: str,
    seed_fingerprint: str,
    branch_id: str,
    seed: dict,
    world_source: Optional[dict],
    player_source: Optional[dict],
    world_requested: bool,
    player_requested: bool,
    world_id: str,
    player_id: str,
    admitted_turn: int,
    applied_ops: int,
    migrated: bool,
    committed_at: float,
) -> dict:
    """Build and validate every field that grants one portable seed receipt authority."""
    authority = {
        "schema": _CREATOR_SEED_RECEIPT_SCHEMA,
        "session_id": str(session_id),
        "seed_fingerprint": str(seed_fingerprint),
        "branch_id": str(branch_id),
        "seed": seed,
        "world_source": world_source,
        "player_source": player_source,
        "world_requested": bool(world_requested),
        "player_requested": bool(player_requested),
        "world_id": str(world_id),
        "player_id": str(player_id),
        "admitted_turn": int(admitted_turn),
        "applied_ops": int(applied_ops),
        "migrated": bool(migrated),
        "committed_at": float(committed_at),
    }
    # This also rejects NaN/infinity and values with no deterministic JSON representation.
    json.dumps(
        authority, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    if not authority["session_id"] or not authority["branch_id"]:
        raise ValueError("portable seed receipt scope is incomplete")
    if not isinstance(seed, dict) or set(seed) - {"world", "player"}:
        raise ValueError("portable seed receipt raw source is invalid")
    from .narrator import seed_fingerprint as exact_seed_fingerprint

    if exact_seed_fingerprint(seed) != authority["seed_fingerprint"]:
        raise ValueError("portable seed fingerprint does not match its exact source")
    seed_world = seed.get("world")
    seed_player = seed.get("player")
    expected_world_requested = bool(
        isinstance(seed_world, dict) and str(seed_world.get("name") or "").strip()
    )
    expected_player_requested = bool(isinstance(seed_player, dict) and seed_player)
    if authority["world_requested"] != expected_world_requested \
            or authority["player_requested"] != expected_player_requested:
        raise ValueError("portable seed receipt request flags diverge from its raw source")
    if not authority["world_requested"] and not authority["player_requested"]:
        raise ValueError("portable seed receipt has no requested source")
    if authority["world_requested"]:
        if not isinstance(world_source, dict) \
                or authority["world_id"] != str(world_source.get("world_id") or "") \
                or not authority["world_id"]:
            raise ValueError("portable seed receipt World identity diverges from its source")
    elif world_source is not None or authority["world_id"]:
        raise ValueError("portable seed receipt carries an unrequested World authority")
    if authority["player_requested"]:
        from .state import slug

        source_name = str((player_source or {}).get("name") or "") \
            if isinstance(player_source, dict) else ""
        if not source_name or authority["player_id"] != slug(source_name):
            raise ValueError("portable seed receipt Character identity diverges from its source")
    elif player_source is not None or authority["player_id"]:
        raise ValueError("portable seed receipt carries an unrequested Character authority")
    if authority["admitted_turn"] < 0 or authority["applied_ops"] < 0:
        raise ValueError("portable seed receipt admission counters are invalid")
    if authority["migrated"] != (authority["applied_ops"] == 0):
        raise ValueError("portable seed receipt migration flag diverges from its admission")
    if not math.isfinite(authority["committed_at"]) or authority["committed_at"] <= 0:
        raise ValueError("portable seed receipt commit time is invalid")
    return authority


def _creator_seed_receipt_integrity(authority: dict) -> str:
    canonical = json.dumps(
        authority, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _validated_creator_seed_receipt_row(row) -> Optional[dict]:
    """Decode one receipt row only after exact source and integrity validation."""
    if row is None:
        return None
    value = dict(row)
    # Rows written before receipt-integrity/1 are not authority. They stay recoverable: an exact
    # future POST can recheck current immutable sources and seal a replacement.
    if not str(value.get("receipt_fingerprint") or ""):
        return None
    try:
        seed = json.loads(value["seed_json"])
        world_source = json.loads(value["world_source_json"] or "null")
        player_source = json.loads(value["player_source_json"] or "null")
        if int(value["world_requested"]) not in (0, 1) \
                or int(value["player_requested"]) not in (0, 1) \
                or int(value["migrated"]) not in (0, 1):
            raise ValueError("portable seed receipt has invalid scalar fields")
        authority = _creator_seed_receipt_authority(
            session_id=value["session_id"],
            seed_fingerprint=value["seed_fingerprint"],
            branch_id=value["branch_id"],
            seed=seed,
            world_source=world_source,
            player_source=player_source,
            world_requested=bool(value["world_requested"]),
            player_requested=bool(value["player_requested"]),
            world_id=value["world_id"],
            player_id=value["player_id"],
            admitted_turn=value["admitted_turn"],
            applied_ops=value["applied_ops"],
            migrated=bool(value["migrated"]),
            committed_at=value["committed_at"],
        )
        expected_integrity = _creator_seed_receipt_integrity(authority)
        if not secrets.compare_digest(
            str(value["receipt_fingerprint"]), expected_integrity,
        ):
            raise ValueError("portable seed receipt integrity digest does not match")
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError) as exc:
        raise ValueError(
            "stored portable seed receipt failed exact integrity validation"
        ) from exc
    value["seed"] = seed
    value["world_source"] = world_source
    value["player_source"] = player_source
    return value


def _validated_creator_seed_receipt_rows(rows, wanted: set[str]) -> dict[str, dict]:
    receipts = {}
    for row in rows:
        session_id = str(row["session_id"])
        if session_id not in wanted:
            continue
        try:
            receipt = _validated_creator_seed_receipt_row(row)
        except ValueError:
            continue
        if receipt is not None:
            receipts[session_id] = receipt
    return receipts


def _ulid() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()  # migration callbacks share Store transaction ownership
        self.db.execute("PRAGMA journal_mode=WAL")
        self.schema_migrations = SchemaMigrationRunner(
            self.db, self._lock, database_schema_migrations()
        )
        self.schema_migrations.run_domain("store-core")
        # A process can stop during the cold LLM pass. Its in-flight claim is not durable work;
        # make the session retryable on the next start.
        with self.transaction():
            self.db.execute(
                "UPDATE sessions SET genesis='rules',"
                " genesis_epoch=COALESCE(genesis_epoch, 0)+1 WHERE genesis='llm'"
            )
        self.worldlex = WorldLexStore(self.db, self._lock, self.schema_migrations)
        # Semantic settlement owns its own additive schema but shares this connection/lock so a
        # reducer commit and its exact replay artifact can inhabit one SQLite transaction.
        from .turn_lifecycle import TurnLifecycleStore
        self.turn_lifecycle = TurnLifecycleStore(self, self.schema_migrations)
        from .system_health import SystemHealth
        self.system_health = SystemHealth(
            self.db, self._lock, self.schema_migrations
        )

    def _insert_chat_message_receipt_locked(
        self,
        branch_id: str,
        turn_index: int,
        lifecycle_source: str,
        response_occurrence_id: str,
        source_message_fingerprint: str,
    ) -> None:
        lifecycle = str(lifecycle_source or "")
        response_id = str(response_occurrence_id or "")
        source_fingerprint = str(source_message_fingerprint or "")
        if lifecycle not in {
            "user_text", "assistant_response", "deferred_extraction",
        } or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_fingerprint):
            raise ValueError("accepted Chat message receipt is malformed")
        if lifecycle == "user_text" and response_id:
            raise ValueError("user-text receipt cannot name an assistant response")
        if lifecycle != "user_text" and re.fullmatch(
            r"response:[0-9a-f]{64}",
            response_id,
        ) is None:
            raise ValueError("assistant receipt requires an accepted response")
        authority = {
            "schema": "aetherstate-chat-accepted-message-receipt/1",
            "branch_id": str(branch_id),
            "turn_index": int(turn_index),
            "lifecycle_source": lifecycle,
            "response_occurrence_id": response_id,
            "source_message_fingerprint": source_fingerprint,
        }
        receipt_fingerprint = _chat_accepted_message_receipt_fingerprint(
            authority
        )
        self.db.execute(
            "INSERT OR IGNORE INTO chat_accepted_message_receipts("
            "branch_id, turn_index, lifecycle_source, response_occurrence_id,"
            " source_message_fingerprint, receipt_fingerprint, committed_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                str(branch_id),
                int(turn_index),
                lifecycle,
                response_id,
                source_fingerprint,
                receipt_fingerprint,
                time.time(),
            ),
        )
        prior = self.db.execute(
            "SELECT response_occurrence_id, source_message_fingerprint,"
            " receipt_fingerprint FROM chat_accepted_message_receipts"
            " WHERE branch_id=? AND turn_index=? AND lifecycle_source=?",
            (str(branch_id), int(turn_index), lifecycle),
        ).fetchone()
        if prior is None or (
            str(prior["response_occurrence_id"] or "") != response_id
            or str(prior["source_message_fingerprint"] or "")
            != source_fingerprint
            or str(prior["receipt_fingerprint"] or "")
            != receipt_fingerprint
        ):
            raise ValueError("accepted Chat message receipt conflicts with prior lineage")

    def apply_guard(self):
        """Serialize one reducer commit; RLock permits nested read/write helpers."""
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Compose Store and WorldLex writes under one caller-visible commit boundary.

        Nested persistence uses a savepoint, so helpers such as ``journal`` and ``checkpoint``
        cannot commit an outer reducer transaction prematurely.
        """
        with self._lock:
            if self.db.in_transaction:
                savepoint = "store_" + uuid.uuid4().hex
                self.db.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield
                except BaseException:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                else:
                    self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                return

            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    # -- sessions / branches -------------------------------------------------
    def get_or_create_session(self, external_id: str) -> sqlite3.Row:
        with self.transaction():
            row = self.db.execute("SELECT * FROM sessions WHERE external_id=?",
                                  (external_id,)).fetchone()
            if row:
                self.db.execute("UPDATE sessions SET last_seen=? WHERE session_id=?",
                                (time.time(), row["session_id"]))
                return row
            sid, bid, now = _ulid(), _ulid(), time.time()
            self.db.execute("INSERT INTO branches(branch_id, session_id) VALUES(?,?)", (bid, sid))
            self.db.execute(
                "INSERT INTO sessions(session_id, external_id, active_branch, created_at, last_seen)"
                " VALUES(?,?,?,?,?)", (sid, external_id, bid, now, now))
            return self.db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()

    def create_session(self, external_id: Optional[str] = None,
                       anchor_hash: Optional[str] = None,
                       frontend: str = "l3") -> tuple[str, str]:
        """Mint a session (L3 path: no external id; anchor = first USER msg hash, 08 S1)."""
        with self.transaction():
            sid, bid, now = _ulid(), _ulid(), time.time()
            self.db.execute("INSERT INTO branches(branch_id, session_id) VALUES(?,?)", (bid, sid))
            self.db.execute(
                "INSERT INTO sessions(session_id, external_id, anchor_hash, frontend,"
                " active_branch, created_at, last_seen) VALUES(?,?,?,?,?,?,?)",
                (sid, external_id, anchor_hash, frontend, bid, now, now))
            return sid, bid

    def chat_core_receipt_for_session(self, session_id: str) -> Optional[dict]:
        """Return restart-safe proof for one exact journaled Chat Core admission."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM chat_core_receipts WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
            if row is None:
                return None
            branch = self.db.execute(
                "SELECT session_id FROM branches WHERE branch_id=?",
                (row["branch_id"],),
            ).fetchone()
            journal = self.db.execute(
                "SELECT branch_id, turn_lo, turn_hi, ops FROM ops_journal WHERE id=?",
                (row["journal_op_id"],),
            ).fetchone()
        authority = {
            "schema": "aetherstate-chat-core-receipt/1",
            "session_id": str(row["session_id"]),
            "branch_id": str(row["branch_id"]),
            "journal_op_id": int(row["journal_op_id"]),
            "core_fingerprint": str(row["core_fingerprint"]),
            "world_fingerprint": str(row["world_fingerprint"] or ""),
            "card_envelope_fingerprint": str(row["card_envelope_fingerprint"]),
            "character_actor_id": str(row["character_actor_id"]),
            "persona_actor_id": str(row["persona_actor_id"]),
            "admitted_turn": int(row["admitted_turn"]),
            "admission_fingerprint": str(row["admission_fingerprint"]),
        }
        if branch is None or str(branch["session_id"]) != authority["session_id"] \
                or journal is None or str(journal["branch_id"]) != authority["branch_id"] \
                or int(journal["turn_lo"]) != authority["admitted_turn"] \
                or int(journal["turn_hi"]) != authority["admitted_turn"]:
            raise ValueError("Chat Core receipt lost its exact session or journal binding")
        if authority["admission_fingerprint"] != _chat_admission_fingerprint(
            authority["card_envelope_fingerprint"], authority["persona_actor_id"],
        ) or str(row["receipt_fingerprint"]) != _chat_receipt_fingerprint(authority):
            raise ValueError("Chat Core receipt fingerprint is invalid")
        try:
            ops = json.loads(journal["ops"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Chat Core receipt journal is unreadable") from exc
        matches = [
            op for op in ops
            if isinstance(op, dict)
            and op.get("op") == "chat_core_seed"
            and op.get("core_fingerprint") == authority["core_fingerprint"]
            and str(op.get("world_fingerprint") or "") == authority["world_fingerprint"]
            and op.get("card_envelope_fingerprint")
            == authority["card_envelope_fingerprint"]
            and op.get("character_actor_id") == authority["character_actor_id"]
            and op.get("persona_actor_id") == authority["persona_actor_id"]
            and op.get("_turn") == authority["admitted_turn"]
        ]
        if len(matches) != 1:
            raise ValueError("Chat Core receipt does not identify one exact journal operation")
        return {
            **authority,
            "receipt_fingerprint": str(row["receipt_fingerprint"]),
            "committed_at": float(row["committed_at"]),
        }

    def persist_chat_core_receipt(
        self,
        *,
        session_id: str,
        branch_id: str,
        journal_op_id: int,
        core_fingerprint: str,
        world_fingerprint: str,
        card_envelope_fingerprint: str,
        character_actor_id: str,
        persona_actor_id: str,
        admitted_turn: int,
    ) -> dict:
        admission_fingerprint = _chat_admission_fingerprint(
            card_envelope_fingerprint, persona_actor_id,
        )
        authority = {
            "schema": "aetherstate-chat-core-receipt/1",
            "session_id": str(session_id),
            "branch_id": str(branch_id),
            "journal_op_id": int(journal_op_id),
            "core_fingerprint": str(core_fingerprint),
            "world_fingerprint": str(world_fingerprint or ""),
            "card_envelope_fingerprint": str(card_envelope_fingerprint),
            "character_actor_id": str(character_actor_id),
            "persona_actor_id": str(persona_actor_id),
            "admitted_turn": int(admitted_turn),
            "admission_fingerprint": admission_fingerprint,
        }
        receipt_fingerprint = _chat_receipt_fingerprint(authority)
        with self.transaction():
            prior = self.chat_core_receipt_for_session(session_id)
            if prior is not None:
                comparable = {
                    key: prior[key] for key in authority
                }
                if comparable != authority:
                    raise ValueError(
                        "session already has a different Chat Core admission receipt",
                    )
                return prior
            committed_at = time.time()
            self.db.execute(
                "INSERT INTO chat_core_receipts("
                "session_id, branch_id, journal_op_id, core_fingerprint, world_fingerprint,"
                "card_envelope_fingerprint, character_actor_id, persona_actor_id, admitted_turn,"
                "admission_fingerprint, receipt_fingerprint, committed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, branch_id, journal_op_id, core_fingerprint,
                    world_fingerprint, card_envelope_fingerprint, character_actor_id,
                    persona_actor_id, admitted_turn, admission_fingerprint,
                    receipt_fingerprint, committed_at,
                ),
            )
            receipt = self.chat_core_receipt_for_session(session_id)
            if receipt is None:
                raise RuntimeError("Chat Core receipt was not durably readable")
            return receipt

    def chat_continuity_seed_receipts(self, session_id: str) -> list[dict]:
        """Return exact per-record Creator seed proofs bound to their journal operations."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM chat_continuity_seed_receipts"
                " WHERE session_id=? ORDER BY committed_at, family, record_fingerprint",
                (str(session_id),),
            ).fetchall()
        receipts: list[dict] = []
        for row in rows:
            try:
                record = json.loads(row["record_json"])
                authority = {
                    "schema": "aetherstate-chat-continuity-seed-receipt/1",
                    "session_id": str(row["session_id"]),
                    "record_fingerprint": str(row["record_fingerprint"]),
                    "branch_id": str(row["branch_id"]),
                    "family": str(row["family"]),
                    "record": record,
                    "admitted_turn": int(row["admitted_turn"]),
                    "journal_op_id": int(row["journal_op_id"]),
                    "lifecycle_source": str(row["lifecycle_source"] or ""),
                    "response_occurrence_id": str(row["response_occurrence_id"] or ""),
                }
                branch = self.db.execute(
                    "SELECT session_id FROM branches WHERE branch_id=?",
                    (authority["branch_id"],),
                ).fetchone()
                journal = self.db.execute(
                    "SELECT branch_id, turn_lo, turn_hi, ops, source,"
                    " lifecycle_source, response_occurrence_id"
                    " FROM ops_journal WHERE id=?",
                    (authority["journal_op_id"],),
                ).fetchone()
                if branch is None \
                        or str(branch["session_id"]) != authority["session_id"] \
                        or journal is None \
                        or str(journal["branch_id"]) != authority["branch_id"] \
                        or int(journal["turn_lo"]) != authority["admitted_turn"] \
                        or int(journal["turn_hi"]) != authority["admitted_turn"] \
                        or str(journal["source"]) != "genesis" \
                        or str(journal["lifecycle_source"] or "") \
                        != authority["lifecycle_source"] \
                        or str(journal["response_occurrence_id"] or "") \
                        != authority["response_occurrence_id"]:
                    raise ValueError("continuity seed receipt lost its exact Ledger binding")
                ops = json.loads(journal["ops"])
                matches = [
                    op for op in ops
                    if isinstance(op, dict)
                    and isinstance(op.get("_continuity_seed"), dict)
                    and op["_continuity_seed"].get("family") == authority["family"]
                    and op["_continuity_seed"].get("record_fingerprint")
                    == authority["record_fingerprint"]
                    and op["_continuity_seed"].get("record") == record
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "continuity seed receipt does not own one exact journal operation"
                    )
                expected = _chat_continuity_seed_receipt_fingerprint(authority)
                if not secrets.compare_digest(
                    str(row["receipt_fingerprint"]), expected
                ):
                    legacy_authority = {
                        key: value
                        for key, value in authority.items()
                        if key not in {"lifecycle_source", "response_occurrence_id"}
                    }
                    legacy = (
                        not authority["lifecycle_source"]
                        and not authority["response_occurrence_id"]
                        and secrets.compare_digest(
                            str(row["receipt_fingerprint"]),
                            _chat_continuity_seed_receipt_fingerprint(
                                legacy_authority
                            ),
                        )
                    )
                    if not legacy:
                        raise ValueError(
                            "continuity seed receipt fingerprint is invalid"
                        )
                committed_at = float(row["committed_at"])
                if not math.isfinite(committed_at):
                    raise ValueError("continuity seed receipt time is invalid")
            except (
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
                json.JSONDecodeError,
            ) as exc:
                raise ValueError(
                    "stored Chat continuity seed receipt failed exact validation"
                ) from exc
            receipts.append(
                {
                    **authority,
                    "receipt_fingerprint": str(row["receipt_fingerprint"]),
                    "committed_at": committed_at,
                }
            )
        return receipts

    def persist_chat_continuity_seed_receipt(
        self,
        *,
        session_id: str,
        record_fingerprint: str,
        branch_id: str,
        family: str,
        record: dict,
        admitted_turn: int,
        journal_op_id: int,
        lifecycle_source: str = "creator_starting_continuity",
        response_occurrence_id: str = "",
    ) -> dict:
        authority = {
            "schema": "aetherstate-chat-continuity-seed-receipt/1",
            "session_id": str(session_id),
            "record_fingerprint": str(record_fingerprint),
            "branch_id": str(branch_id),
            "family": str(family),
            "record": json.loads(_canonical_json(record)),
            "admitted_turn": int(admitted_turn),
            "journal_op_id": int(journal_op_id),
            "lifecycle_source": str(lifecycle_source or ""),
            "response_occurrence_id": str(response_occurrence_id or ""),
        }
        receipt_fingerprint = _chat_continuity_seed_receipt_fingerprint(authority)
        with self.transaction():
            prior = next(
                (
                    receipt
                    for receipt in self.chat_continuity_seed_receipts(session_id)
                    if receipt["record_fingerprint"] == record_fingerprint
                ),
                None,
            )
            if prior is not None:
                if any(prior[key] != authority[key] for key in authority):
                    raise ValueError(
                        "continuity seed identity already owns different journal bytes"
                    )
                return prior
            committed_at = time.time()
            self.db.execute(
                "INSERT INTO chat_continuity_seed_receipts("
                "session_id, record_fingerprint, branch_id, family, record_json,"
                " admitted_turn, journal_op_id, receipt_fingerprint, committed_at,"
                " lifecycle_source, response_occurrence_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    authority["session_id"],
                    authority["record_fingerprint"],
                    authority["branch_id"],
                    authority["family"],
                    _canonical_json(authority["record"]),
                    authority["admitted_turn"],
                    authority["journal_op_id"],
                    receipt_fingerprint,
                    committed_at,
                    authority["lifecycle_source"],
                    authority["response_occurrence_id"],
                ),
            )
            receipt = next(
                (
                    candidate
                    for candidate in self.chat_continuity_seed_receipts(session_id)
                    if candidate["record_fingerprint"] == record_fingerprint
                ),
                None,
            )
            if receipt is None:
                raise RuntimeError("continuity seed receipt was not durably readable")
            return receipt

    def creator_seed_receipt(self, session_id: str, seed_fingerprint: str) -> Optional[dict]:
        """Return one validated durable portable-seed admission receipt.

        The raw portable source is retained so the fingerprint remains independently
        re-checkable after restart.  A damaged or manually forged row fails closed instead of
        becoming authority merely because its primary-key text happens to match a card.
        """
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM creator_seed_receipts"
                " WHERE session_id=? AND seed_fingerprint=?",
                (str(session_id), str(seed_fingerprint)),
            ).fetchone()
        return _validated_creator_seed_receipt_row(row)

    def creator_seed_receipt_for_session(self, session_id: str) -> Optional[dict]:
        """Return the session's sole validated receipt, regardless of fingerprint."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM creator_seed_receipts WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        return _validated_creator_seed_receipt_row(row)

    def creator_seed_session_snapshot(
        self, session_ids,
    ) -> tuple[dict[str, tuple[str, object, int]], dict[str, dict]]:
        """Atomically snapshot session positions and receipt rows, then validate unlocked.

        The lock covers only two bounded reads. Exact JSON/integrity validation happens after the
        snapshot so a cosmetic session-list request never holds the global apply guard while doing
        per-session decoding or historical state replay.
        """
        wanted = {str(session_id) for session_id in session_ids if str(session_id)}
        if not wanted:
            return {}, {}
        with self._lock:
            session_rows = self.db.execute(
                "SELECT s.session_id, s.active_branch, b.head_turn,"
                " COALESCE(j.journal_fence, 0) AS journal_fence FROM sessions s"
                " LEFT JOIN branches b ON b.branch_id=s.active_branch"
                " LEFT JOIN (SELECT branch_id, MAX(id) AS journal_fence FROM ops_journal"
                " GROUP BY branch_id) j ON j.branch_id=s.active_branch"
            ).fetchall()
            receipt_rows = self.db.execute("SELECT * FROM creator_seed_receipts").fetchall()
        positions = {
            str(row["session_id"]): (
                str(row["active_branch"] or ""), row["head_turn"],
                int(row["journal_fence"] or 0),
            )
            for row in session_rows if str(row["session_id"]) in wanted
        }
        return positions, _validated_creator_seed_receipt_rows(receipt_rows, wanted)

    def persist_creator_seed_receipt(
        self,
        *,
        session_id: str,
        seed_fingerprint: str,
        branch_id: str,
        seed: dict,
        world_source: Optional[dict],
        player_source: Optional[dict],
        world_requested: bool,
        player_requested: bool,
        world_id: str,
        player_id: str,
        admitted_turn: int,
        applied_ops: int,
        migrated: bool,
        replace_stale: bool = False,
    ) -> dict:
        """Persist exactly one portable-source authority receipt for a session.

        Callers normally hold the outer reducer transaction.  Nested use is a savepoint, so the
        receipt cannot survive a later admission rollback.  The unique session index prevents a
        second, merely similar card revision from being blessed concurrently.
        """
        try:
            committed_at = time.time()
            authority = _creator_seed_receipt_authority(
                session_id=session_id,
                seed_fingerprint=seed_fingerprint,
                branch_id=branch_id,
                seed=seed,
                world_source=world_source,
                player_source=player_source,
                world_requested=world_requested,
                player_requested=player_requested,
                world_id=world_id,
                player_id=player_id,
                admitted_turn=admitted_turn,
                applied_ops=applied_ops,
                migrated=migrated,
                committed_at=committed_at,
            )
            seed_json = json.dumps(
                authority["seed"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            world_source_json = json.dumps(
                authority["world_source"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            player_source_json = json.dumps(
                authority["player_source"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
            receipt_fingerprint = _creator_seed_receipt_integrity(authority)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("portable seed receipt source is not canonical finite JSON") from exc
        with self.transaction():
            # Lazy schema migration: an unsigned pre-integrity row grants no authority.  Remove
            # only that exact legacy shape so the submitted seed must prove current postconditions
            # before this method can seal a replacement.  A signed-but-corrupt row still raises.
            unsigned = self.db.execute(
                "SELECT receipt_fingerprint FROM creator_seed_receipts WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
            if unsigned is not None and not str(unsigned["receipt_fingerprint"] or ""):
                self.db.execute(
                    "DELETE FROM creator_seed_receipts WHERE session_id=?",
                    (str(session_id),),
                )
            prior = self.creator_seed_receipt_for_session(session_id)
            if prior is not None:
                exact = (
                    prior["seed_fingerprint"] == seed_fingerprint
                    and prior["seed_json"] == seed_json
                    and prior["world_source_json"] == world_source_json
                    and prior["player_source_json"] == player_source_json
                    and bool(prior["world_requested"]) == bool(world_requested)
                    and bool(prior["player_requested"]) == bool(player_requested)
                    and prior["world_id"] == str(world_id)
                    and prior["player_id"] == str(player_id)
                )
                if not exact:
                    raise ValueError(
                        "session already has a different portable seed admission receipt"
                    )
                if not replace_stale:
                    return prior
                self.db.execute(
                    "DELETE FROM creator_seed_receipts"
                    " WHERE session_id=? AND seed_fingerprint=?",
                    (str(session_id), str(seed_fingerprint)),
                )
            self.db.execute(
                "INSERT INTO creator_seed_receipts("
                "session_id, seed_fingerprint, branch_id, seed_json, world_source_json,"
                "player_source_json, world_requested,"
                "player_requested, world_id, player_id, admitted_turn, applied_ops, migrated,"
                "receipt_fingerprint, committed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(session_id), str(seed_fingerprint), str(branch_id), seed_json,
                    world_source_json, player_source_json,
                    int(bool(world_requested)), int(bool(player_requested)), str(world_id),
                    str(player_id), int(admitted_turn), int(applied_ops), int(bool(migrated)),
                    receipt_fingerprint, committed_at,
                ),
            )
            receipt = self.creator_seed_receipt(session_id, seed_fingerprint)
            if receipt is None:  # defensive: the row was just written inside this transaction
                raise RuntimeError("portable seed receipt was not durably readable")
            return receipt

    def persist_semantic_bootstrap_proof(self, value):
        """Persist one exact T0 proof while its session/genesis transaction is still fenced.

        The pure bootstrap module validates structure and replay.  This Store boundary additionally
        binds that proof to the just-created session/branch, the exact current global journal high
        water, the durable journal window, and the reducer-derived current state.  Callers must hold
        the outer transaction that created and seeded the session; nested use remains one savepoint.
        """
        from .capability_glossary import content_fingerprint
        from .semantic_bootstrap_runtime import semantic_bootstrap_persistence_payload
        from .state import current_state

        payload = semantic_bootstrap_persistence_payload(value)
        session_id = str(payload["session_id"])
        branch_id = str(payload["branch_id"])
        proof_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self.transaction():
            session = self.db.execute(
                "SELECT active_branch FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            branch = self.db.execute(
                "SELECT session_id FROM branches WHERE branch_id=?", (branch_id,)
            ).fetchone()
            if session is None or branch is None \
                    or str(session["active_branch"]) != branch_id \
                    or str(branch["session_id"]) != session_id:
                raise ValueError("semantic bootstrap proof is not bound to its active session branch")
            if self.journal_high_water() != int(payload["journal_high_water_after"]):
                raise ValueError("semantic bootstrap proof is not at the current journal fence")
            observed_rows = self.journal_window(
                branch_id,
                after_id=int(payload["journal_high_water_before"]),
                through_id=int(payload["journal_high_water_after"]),
            )
            if observed_rows != payload["journal_rows"]:
                raise ValueError("semantic bootstrap proof differs from its durable journal window")
            observed_state = current_state(self, branch_id)
            if content_fingerprint(observed_state) != payload["post_bootstrap_state_fingerprint"] \
                    or observed_state != payload["post_bootstrap_state"]:
                raise ValueError("semantic bootstrap proof differs from current reducer state")
            prior = self.db.execute(
                "SELECT * FROM semantic_bootstrap_proofs WHERE session_id=? OR branch_id=?",
                (session_id, branch_id),
            ).fetchone()
            if prior is not None:
                if str(prior["session_id"]) == session_id \
                        and str(prior["branch_id"]) == branch_id \
                        and str(prior["proof_fingerprint"]) == payload["fingerprint"] \
                        and str(prior["proof_json"]) == proof_json:
                    return payload
                raise ValueError("semantic bootstrap proof identity is already immutable")
            self.db.execute(
                "INSERT INTO semantic_bootstrap_proofs(session_id, branch_id, turn_index,"
                " proof_fingerprint, post_ledger_hash, journal_high_water_after, proof_json,"
                " committed_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    branch_id,
                    int(payload["turn_index"]),
                    str(payload["fingerprint"]),
                    str(payload["post_bootstrap_state_fingerprint"]),
                    int(payload["journal_high_water_after"]),
                    proof_json,
                    time.time(),
                ),
            )
        return payload

    def semantic_bootstrap_proof(self, session_id: str, branch_id: Optional[str] = None):
        """Return a revalidated immutable bootstrap proof, or ``None`` when none was committed."""
        from .semantic_bootstrap_runtime import validate_semantic_bootstrap_proof

        with self._lock:
            row = self.db.execute(
                "SELECT * FROM semantic_bootstrap_proofs WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            return None
        if branch_id is not None and str(row["branch_id"]) != str(branch_id):
            raise ValueError("semantic bootstrap proof belongs to a different branch")
        try:
            payload = json.loads(row["proof_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("stored semantic bootstrap proof is malformed") from exc
        proof = validate_semantic_bootstrap_proof(payload)
        if proof.session_id != str(row["session_id"]) \
                or proof.branch_id != str(row["branch_id"]) \
                or proof.turn_index != int(row["turn_index"]) \
                or proof.fingerprint != str(row["proof_fingerprint"]) \
                or proof.post_bootstrap_state_fingerprint != str(row["post_ledger_hash"]) \
                or proof.journal_high_water_after != int(row["journal_high_water_after"]):
            raise ValueError("stored semantic bootstrap proof columns are inconsistent")
        with self._lock:
            session = self.db.execute(
                "SELECT active_branch FROM sessions WHERE session_id=?", (proof.session_id,)
            ).fetchone()
            branch = self.db.execute(
                "SELECT session_id FROM branches WHERE branch_id=?", (proof.branch_id,)
            ).fetchone()
        if session is None or branch is None \
                or str(session["active_branch"]) != proof.branch_id \
                or str(branch["session_id"]) != proof.session_id:
            raise ValueError("stored semantic bootstrap proof lost its session binding")
        observed_rows = self.journal_window(
            proof.branch_id,
            after_id=proof.journal_high_water_before,
            through_id=proof.journal_high_water_after,
        )
        if observed_rows != proof.journal_rows:
            raise ValueError("stored semantic bootstrap proof lost its journal binding")
        return proof

    def touch_session(self, session_id: str) -> float:
        """Persist and return the one timestamp used to mark a session as recently seen."""
        now = time.time()
        with self.transaction():
            self.db.execute("UPDATE sessions SET last_seen=? WHERE session_id=?",
                            (now, session_id))
        return now

    def experience_binding(self, session_id: str) -> ExperienceBinding:
        with self._lock:
            row = self.db.execute(
                "SELECT experience_mode, experience_mode_source,"
                " experience_mode_locked_turn, core_fingerprint,"
                " character_actor_id, persona_actor_id"
                " FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError("unknown session")
        return ExperienceBinding(
            normalize_experience_mode(row["experience_mode"]),
            str(row["experience_mode_source"] or ""),
            (
                int(row["experience_mode_locked_turn"])
                if row["experience_mode_locked_turn"] is not None
                else None
            ),
            str(row["core_fingerprint"] or ""),
            str(row["character_actor_id"] or ""),
            str(row["persona_actor_id"] or ""),
        )

    def experience_inference_set_unlocked(
        self, session_id: str, mode: str, source: str
    ) -> ExperienceBinding:
        selected = normalize_experience_mode(mode)
        inferred_source = str(source or "default")
        if inferred_source == "explicit":
            raise ValueError("inference source cannot be explicit")
        with self.transaction():
            current = self.experience_binding(session_id)
            if current.locked or current.source == "explicit":
                return current
            self.db.execute(
                "UPDATE sessions SET experience_mode=?, experience_mode_source=?"
                " WHERE session_id=? AND experience_mode_locked_turn IS NULL"
                " AND COALESCE(experience_mode_source, '')<>'explicit'",
                (selected, inferred_source, session_id),
            )
            return self.experience_binding(session_id)

    def experience_mode_set_unlocked(
        self, session_id: str, mode: str
    ) -> ExperienceBinding:
        selected = normalize_experience_mode(mode)
        with self.transaction():
            current = self.experience_binding(session_id)
            if current.locked:
                raise ExperienceModeLocked(current)
            self.db.execute(
                "UPDATE sessions SET experience_mode=?, experience_mode_source='explicit'"
                " WHERE session_id=? AND experience_mode_locked_turn IS NULL",
                (selected, session_id),
            )
            return self.experience_binding(session_id)

    def experience_lock(self, session_id: str, turn: int) -> ExperienceBinding:
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 0:
            raise ValueError("locked turn must be a non-negative integer")
        with self.transaction():
            current = self.experience_binding(session_id)
            if current.locked:
                return current
            self.db.execute(
                "UPDATE sessions SET experience_mode=?,"
                " experience_mode_source=CASE WHEN COALESCE(experience_mode_source,'')=''"
                " THEN 'default' ELSE experience_mode_source END,"
                " experience_mode_locked_turn=?"
                " WHERE session_id=? AND experience_mode_locked_turn IS NULL",
                (current.mode, turn, session_id),
            )
            return self.experience_binding(session_id)

    def reset_unlocked_experience(
        self, session_id: str, mode: str
    ) -> ExperienceBinding:
        """Atomically change an unaccepted session and discard only provisional Genesis state."""
        selected = normalize_experience_mode(mode)
        with self.transaction():
            current = self.experience_binding(session_id)
            if current.locked:
                raise ExperienceModeLocked(current)
            accepted = self.db.execute(
                "SELECT 1 FROM turn_texts tt JOIN branches b ON b.branch_id=tt.branch_id"
                " WHERE b.session_id=? AND COALESCE(tt.assistant_text,'')<>'' LIMIT 1",
                (session_id,),
            ).fetchone()
            if accepted is not None:
                raise ExperienceModeLocked(current)
            row = self.db.execute(
                "SELECT active_branch FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError("unknown session")
            active_branch = str(row["active_branch"] or "")
            branch_rows = self.db.execute(
                "SELECT branch_id FROM branches WHERE session_id=?", (session_id,)
            ).fetchall()
            branch_ids = {str(branch["branch_id"]) for branch in branch_rows}

            checkpoint_rows = self.db.execute(
                "SELECT branch_id, turn_index, state FROM checkpoints WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?)",
                (session_id,),
            ).fetchall()

            durable_records = self.db.execute(
                "SELECT 1 FROM claim_records WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?)"
                " UNION ALL SELECT 1 FROM world_event_records WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?)"
                " UNION ALL SELECT 1 FROM effect_receipts WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?)"
                " UNION ALL SELECT 1 FROM mechanic_settlement_receipts WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?) LIMIT 1",
                (session_id, session_id, session_id, session_id),
            ).fetchone()
            if durable_records is not None:
                raise ExperienceModeLocked(current)

            raw_creator_receipt = self.db.execute(
                "SELECT 1 FROM creator_seed_receipts WHERE session_id=?", (session_id,)
            ).fetchone()
            try:
                creator_receipt = self.creator_seed_receipt_for_session(session_id)
            except ValueError as exc:
                raise ExperienceModeLocked(current) from exc
            if raw_creator_receipt is not None and creator_receipt is None:
                raise ExperienceModeLocked(current)
            raw_chat_receipt = self.db.execute(
                "SELECT 1 FROM chat_core_receipts WHERE session_id=?", (session_id,),
            ).fetchone()
            try:
                chat_receipt = self.chat_core_receipt_for_session(session_id)
            except ValueError as exc:
                raise ExperienceModeLocked(current) from exc
            if raw_chat_receipt is not None and chat_receipt is None:
                raise ExperienceModeLocked(current)
            raw_continuity_receipts = self.db.execute(
                "SELECT COUNT(*) AS n FROM chat_continuity_seed_receipts"
                " WHERE session_id=?",
                (session_id,),
            ).fetchone()
            try:
                continuity_receipts = self.chat_continuity_seed_receipts(session_id)
            except ValueError as exc:
                raise ExperienceModeLocked(current) from exc
            if int(raw_continuity_receipts["n"] or 0) != len(continuity_receipts):
                raise ExperienceModeLocked(current)
            continuity_journal_ids = {
                int(receipt["journal_op_id"]) for receipt in continuity_receipts
            }

            authored_row_ids: set[int] = set()
            authored_world_id = ""
            authored_world_state = None
            if creator_receipt is not None:
                if not creator_receipt["world_requested"] \
                        or creator_receipt["player_requested"] \
                        or creator_receipt["migrated"] \
                        or int(creator_receipt["applied_ops"]) <= 0 \
                        or str(creator_receipt["branch_id"]) not in branch_ids:
                    raise ExperienceModeLocked(current)
                authored_world_id = str(creator_receipt["world_id"] or "")
                world_source = creator_receipt["world_source"]
                from . import creator as _creator
                from .state import (
                    TIMES,
                    _creator_world_snapshot,
                    empty_state,
                    reduce_state,
                    slug,
                )

                admitted_turn = int(creator_receipt["admitted_turn"])
                expected_snapshot = _creator_world_snapshot(
                    world_source, admitted_turn
                )
                expected_payloads = [
                    json.dumps(
                        op,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    for op in _creator.world_to_ops(world_source)
                ]
                expected_payloads.sort()
                candidates = self.db.execute(
                    "SELECT id, ops FROM ops_journal WHERE branch_id=? AND turn_lo=?"
                    " AND turn_hi=? AND source='user' ORDER BY id",
                    (
                        str(creator_receipt["branch_id"]),
                        int(creator_receipt["admitted_turn"]),
                        int(creator_receipt["admitted_turn"]),
                    ),
                ).fetchall()
                exact_rows = []
                for candidate in candidates:
                    try:
                        ops = json.loads(candidate["ops"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(ops, list) \
                            or len(ops) != int(creator_receipt["applied_ops"]):
                        continue
                    normalized_payloads = []
                    valid_payloads = True
                    for op in ops:
                        op_turn = op.get("_turn") if isinstance(op, dict) else None
                        if not isinstance(op, dict) \
                                or not isinstance(op_turn, int) \
                                or isinstance(op_turn, bool) \
                                or op_turn != admitted_turn:
                            valid_payloads = False
                            break
                        normalized = dict(op)
                        expected_metadata = {"_turn": admitted_turn}
                        if normalized.get("op") == "creator_world_seed":
                            expected_metadata["_snapshot"] = expected_snapshot
                        if normalized.get("op") == "scene_set" \
                                and current.mode == "rpg":
                            expected_metadata["_canon"] = 1
                        if normalized.get("op") == "time_advance" \
                                and current.mode == "rpg":
                            expected_metadata["_turn_mark"] = 1
                            new_time = normalized.get("to_time_of_day")
                            if new_time in TIMES \
                                    and TIMES.index(new_time) <= TIMES.index("evening"):
                                expected_metadata["_day_wrap"] = 1
                        if normalized.get("op") == "front_add":
                            expected_metadata.update({
                                "_fid": slug(str(normalized.get("name") or ""))[:64],
                                "_segments": max(
                                    3, min(12, int(normalized.get("segments", 6)))
                                ),
                                "_pace": max(
                                    1, min(3, int(normalized.get("pace", 1)))
                                ),
                            })
                            if normalized.get("event_duration_turns") is not None:
                                expected_metadata["_event_duration_turns"] = max(
                                    1,
                                    min(
                                        100,
                                        int(normalized["event_duration_turns"]),
                                    ),
                                )
                        actual_metadata = {
                            key: value
                            for key, value in normalized.items()
                            if str(key).startswith("_")
                        }
                        try:
                            metadata_matches = json.dumps(
                                actual_metadata,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ) == json.dumps(
                                expected_metadata,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            )
                        except (TypeError, ValueError):
                            metadata_matches = False
                        if not metadata_matches:
                            valid_payloads = False
                            break
                        for key in expected_metadata:
                            normalized.pop(key)
                        try:
                            normalized_payloads.append(json.dumps(
                                normalized,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ))
                        except (TypeError, ValueError):
                            valid_payloads = False
                            break
                    if valid_payloads \
                            and sorted(normalized_payloads) == expected_payloads:
                        exact_rows.append((candidate, ops))
                if len(exact_rows) != 1:
                    raise ExperienceModeLocked(current)
                authored_world_state = reduce_state(
                    empty_state(), exact_rows[0][1]
                )
                authored_row_ids.add(int(exact_rows[0][0]["id"]))

            proof_row = self.db.execute(
                "SELECT branch_id FROM semantic_bootstrap_proofs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            proof = None
            proof_row_ids: set[int] = set()
            if proof_row is not None:
                proof_branch = str(proof_row["branch_id"] or "")
                if proof_branch != active_branch or proof_branch not in branch_ids:
                    raise ExperienceModeLocked(current)
                try:
                    proof = self.semantic_bootstrap_proof(session_id, proof_branch)
                except ValueError as exc:
                    raise ExperienceModeLocked(current) from exc
                if proof is None:
                    raise ExperienceModeLocked(current)
                proof_row_ids = {int(entry["id"]) for entry in proof.journal_rows}

            journal_rows = self.db.execute(
                "SELECT id, branch_id, turn_hi, source, ops FROM ops_journal WHERE branch_id IN"
                " (SELECT branch_id FROM branches WHERE session_id=?) ORDER BY id",
                (session_id,),
            ).fetchall()
            provisional_ids: set[int] = set()
            journal_ops: dict[int, list[dict]] = {}
            for journal_row in journal_rows:
                journal_id = int(journal_row["id"])
                source = str(journal_row["source"] or "")
                try:
                    ops = json.loads(journal_row["ops"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ExperienceModeLocked(current) from exc
                if not isinstance(ops, list) \
                        or any(not isinstance(op, dict) for op in ops):
                    raise ExperienceModeLocked(current)
                journal_ops[journal_id] = ops
                if journal_id in authored_row_ids:
                    continue
                if source == "genesis" or (
                    source == "bootstrap" and journal_id in proof_row_ids
                ):
                    provisional_ids.add(journal_id)
                    if authored_world_id:
                        provisional_world_ids = {
                            str(op.get("world_id") or "")
                            for op in ops
                            if isinstance(op, dict)
                            and op.get("op") == "world_identity_set"
                        }
                        provisional_world_ids.update(
                            str((op.get("document") or {}).get("world_id") or "")
                            for op in ops
                            if isinstance(op, dict)
                            and op.get("op") == "creator_world_seed"
                        )
                        provisional_world_ids.discard("")
                        if provisional_world_ids - {authored_world_id}:
                            raise ExperienceModeLocked(current)
                    continue
                raise ExperienceModeLocked(current)

            if proof_row_ids - provisional_ids:
                raise ExperienceModeLocked(current)
            if chat_receipt is not None \
                    and int(chat_receipt["journal_op_id"]) not in provisional_ids:
                raise ExperienceModeLocked(current)
            if continuity_journal_ids - provisional_ids:
                raise ExperienceModeLocked(current)

            if checkpoint_rows:
                from .state import empty_state, reduce_state

                creator_branch = (
                    str(creator_receipt["branch_id"])
                    if creator_receipt is not None
                    else ""
                )
                for checkpoint_row in checkpoint_rows:
                    checkpoint_branch = str(checkpoint_row["branch_id"])
                    if creator_branch and checkpoint_branch != creator_branch:
                        raise ExperienceModeLocked(current)
                    branch_journal = [
                        row for row in journal_rows
                        if str(row["branch_id"]) == checkpoint_branch
                    ]
                    if not branch_journal:
                        raise ExperienceModeLocked(current)
                    try:
                        checkpoint_state = json.loads(checkpoint_row["state"])
                        checkpoint_json = json.dumps(
                            checkpoint_state,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise ExperienceModeLocked(current) from exc
                    checkpoint_turn = int(checkpoint_row["turn_index"])
                    prefix_state = empty_state()
                    permitted_states: set[str] = set()
                    for journal_row in branch_journal:
                        if int(journal_row["turn_hi"]) > checkpoint_turn:
                            continue
                        prefix_state = reduce_state(
                            prefix_state,
                            journal_ops[int(journal_row["id"])],
                        )
                        permitted_states.add(json.dumps(
                            prefix_state,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ))
                    if checkpoint_json not in permitted_states:
                        raise ExperienceModeLocked(current)

            from .turn_lifecycle import TurnLifecycleError

            try:
                self.turn_lifecycle.delete_provisional_bootstrap_family(
                    session_id, proof
                )
            except (TurnLifecycleError, TypeError, ValueError) as exc:
                raise ExperienceModeLocked(current) from exc
            if proof_row is not None:
                self.db.execute(
                    "DELETE FROM semantic_bootstrap_proofs WHERE session_id=?",
                    (session_id,),
                )
            if provisional_ids:
                marks = ",".join("?" for _ in provisional_ids)
                self.db.execute(
                    f"DELETE FROM ops_journal WHERE id IN ({marks})",
                    tuple(sorted(provisional_ids)),
                )
            if chat_receipt is not None:
                self.db.execute(
                    "DELETE FROM chat_core_receipts WHERE session_id=?", (session_id,),
                )
            if continuity_receipts:
                self.db.execute(
                    "DELETE FROM chat_continuity_seed_receipts WHERE session_id=?",
                    (session_id,),
                )
            if checkpoint_rows:
                self.db.execute(
                    "DELETE FROM checkpoints WHERE branch_id IN"
                    " (SELECT branch_id FROM branches WHERE session_id=?)",
                    (session_id,),
                )
            if creator_receipt is not None:
                self.checkpoint(
                    str(creator_receipt["branch_id"]),
                    int(creator_receipt["admitted_turn"]),
                    authored_world_state,
                )
            self.db.execute(
                "UPDATE sessions SET genesis='', genesis_epoch=COALESCE(genesis_epoch,0)+1,"
                " narrator_speaker='', experience_mode=?, experience_mode_source='explicit',"
                " core_fingerprint='', character_actor_id='', persona_actor_id=''"
                " WHERE session_id=? AND experience_mode_locked_turn IS NULL",
                (selected, session_id),
            )
            return self.experience_binding(session_id)

    def relink_external(self, session_id: str, external_id: str) -> None:
        """08 S4: chat rename — L3 chain evidence outranks a never-seen L1 id."""
        with self.transaction():
            self.db.execute("UPDATE sessions SET external_id=NULL WHERE external_id=?",
                            (external_id,))
            self.db.execute("UPDATE sessions SET external_id=? WHERE session_id=?",
                            (external_id, session_id))

    def inherit_session_settings(self, source_session_id: str,
                                 target_session_id: str) -> bool:
        """Copy frozen session policy into an explicitly identified child chat.

        Branches inherit ledger state through ``fork_branch``. These four session-scoped
        controls live outside that ledger and therefore need an explicit, bounded copy.
        """
        with self.transaction():
            source = self.db.execute(
                "SELECT frozen, genesis, mode, narrator_speaker, experience_mode,"
                " experience_mode_source, experience_mode_locked_turn, core_fingerprint,"
                " character_actor_id, persona_actor_id FROM sessions"
                " WHERE session_id=?", (source_session_id,)).fetchone()
            if source is None:
                return False
            inherit_experience = (
                source["experience_mode_locked_turn"] is not None
                or source["experience_mode_source"] == "explicit"
            )
            cur = self.db.execute(
                "UPDATE sessions SET frozen=?, genesis=?, mode=?, narrator_speaker=?,"
                " experience_mode=?, experience_mode_source=?,"
                " experience_mode_locked_turn=?, core_fingerprint=?,"
                " character_actor_id=?, persona_actor_id=?"
                " WHERE session_id=?",
                (source["frozen"], source["genesis"], source["mode"],
                  source["narrator_speaker"],
                  source["experience_mode"] if inherit_experience else "",
                  source["experience_mode_source"] if inherit_experience else "",
                  source["experience_mode_locked_turn"] if inherit_experience else None,
                  source["core_fingerprint"] if inherit_experience else "",
                  source["character_actor_id"] if inherit_experience else "",
                  source["persona_actor_id"] if inherit_experience else "",
                  target_session_id))
            if cur.rowcount != 1:
                return False
            source_receipt = self.chat_core_receipt_for_session(source_session_id)
            if source_receipt is not None and inherit_experience:
                target = self.db.execute(
                    "SELECT active_branch FROM sessions WHERE session_id=?",
                    (target_session_id,),
                ).fetchone()
                target_branch = str(target["active_branch"] or "") if target else ""
                rows = self.db.execute(
                    "SELECT id, ops FROM ops_journal WHERE branch_id=? ORDER BY id",
                    (target_branch,),
                ).fetchall()
                matching = []
                for row in rows:
                    try:
                        ops = json.loads(row["ops"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if any(
                        isinstance(op, dict)
                        and op.get("op") == "chat_core_seed"
                        and op.get("core_fingerprint") == source_receipt["core_fingerprint"]
                        and op.get("persona_actor_id") == source_receipt["persona_actor_id"]
                        for op in ops
                    ):
                        matching.append(int(row["id"]))
                if len(matching) != 1:
                    raise ValueError("forked Chat Core lost its exact journal identity")
                self.persist_chat_core_receipt(
                    session_id=target_session_id,
                    branch_id=target_branch,
                    journal_op_id=matching[0],
                    core_fingerprint=source_receipt["core_fingerprint"],
                    world_fingerprint=source_receipt["world_fingerprint"],
                    card_envelope_fingerprint=source_receipt["card_envelope_fingerprint"],
                    character_actor_id=source_receipt["character_actor_id"],
                    persona_actor_id=source_receipt["persona_actor_id"],
                    admitted_turn=source_receipt["admitted_turn"],
                )
                source_continuity = self.chat_continuity_seed_receipts(
                    source_session_id
                )
                for seed_receipt in source_continuity:
                    matching_seed_rows = []
                    for row in rows:
                        try:
                            ops = json.loads(row["ops"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if any(
                            isinstance(op, dict)
                            and isinstance(op.get("_continuity_seed"), dict)
                            and op["_continuity_seed"].get("family")
                            == seed_receipt["family"]
                            and op["_continuity_seed"].get("record_fingerprint")
                            == seed_receipt["record_fingerprint"]
                            for op in ops
                        ):
                            matching_seed_rows.append(int(row["id"]))
                    if not matching_seed_rows:
                        # The seed was outside the proven fork prefix.
                        continue
                    if len(matching_seed_rows) != 1:
                        raise ValueError(
                            "forked continuity seed lost its exact journal identity"
                        )
                    self.persist_chat_continuity_seed_receipt(
                        session_id=target_session_id,
                        record_fingerprint=seed_receipt["record_fingerprint"],
                        branch_id=target_branch,
                        family=seed_receipt["family"],
                        record=seed_receipt["record"],
                        admitted_turn=seed_receipt["admitted_turn"],
                        journal_op_id=matching_seed_rows[0],
                        lifecycle_source=seed_receipt["lifecycle_source"],
                        response_occurrence_id=seed_receipt["response_occurrence_id"],
                    )
            return True

    # -- canonical transcript (L3 spine, 03 SS2.2) -------------------------------
    def append_msgs(self, branch_id: str, start_pos: int,
                    rows: list[tuple[str, str, str]]) -> None:
        """rows = [(role, content_hash, chain_hash)] appended at start_pos.."""
        with self.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO branch_msgs(branch_id, pos, role, content_hash,"
                " chain_hash) VALUES(?,?,?,?,?)",
                [(branch_id, start_pos + i, r, ch, kh) for i, (r, ch, kh) in enumerate(rows)])

    def truncate_msgs(self, branch_id: str, keep: int) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM branch_msgs WHERE branch_id=? AND pos>=?",
                            (branch_id, keep))

    def get_msgs(self, branch_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT pos, role, content_hash, chain_hash FROM branch_msgs"
                " WHERE branch_id=? ORDER BY pos", (branch_id,)).fetchall()

    def live_branches(self) -> list[sqlite3.Row]:
        """Index rebuild on restart (03 SS2.3): every live branch + its session recency."""
        with self._lock:
            return self.db.execute(
                "SELECT b.branch_id, b.session_id, s.last_seen FROM branches b"
                " JOIN sessions s ON s.session_id=b.session_id"
                " WHERE b.status='live'").fetchall()

    def turn_for_message_ordinal(self, branch_id: str, ordinal: int) -> int:
        """Map N shared Player/text messages to the Nth recorded source turn."""
        if ordinal <= 0:
            return -1
        with self._lock:
            rows = self.db.execute(
                "SELECT turn_index FROM turns WHERE branch_id=? ORDER BY turn_index",
                (branch_id,)).fetchall()
        if not rows:
            return -1
        return int(rows[min(ordinal, len(rows)) - 1]["turn_index"])

    def fork_branch(self, source_branch: str, at_pos: int, fork_turn: int,
                    new_session_id: Optional[str] = None,
                    kill_source: bool = False, prune_keep: int = 3,
                    discard_empty_branch: Optional[str] = None) -> str:
        """03 SS2.3 case 3 / 08 S5: copy transcript prefix [0, at_pos) + turns <= fork_turn."""
        with self.transaction():
            self.turn_lifecycle.assert_fork_prefix_ready(source_branch, at_pos, fork_turn)
            row = self.db.execute("SELECT session_id FROM branches WHERE branch_id=?",
                                  (source_branch,)).fetchone()
            sid = new_session_id or (row["session_id"] if row else None)
            bid = _ulid()
            self.db.execute(
                "INSERT INTO branches(branch_id, session_id, parent_branch, forked_at,"
                " head_turn) VALUES(?,?,?,?,?)", (bid, sid, source_branch, at_pos, fork_turn))
            self.db.execute(
                "INSERT INTO branch_msgs(branch_id, pos, role, content_hash, chain_hash)"
                " SELECT ?, pos, role, content_hash, chain_hash FROM branch_msgs"
                " WHERE branch_id=? AND pos<?", (bid, source_branch, at_pos))
            self.db.execute(
                "INSERT INTO turns(branch_id, turn_index, user_hash, assistant_hash,"
                " chain_hash, klass, gen_type, swipe_count, settled, extraction,"
                " accepted_response_occurrence_id)"
                " SELECT ?, turn_index, user_hash, assistant_hash, chain_hash, klass,"
                " gen_type, swipe_count, settled, extraction,"
                " accepted_response_occurrence_id FROM turns"
                " WHERE branch_id=? AND turn_index<=?", (bid, source_branch, fork_turn))
            # 03 SS3.3: the fork inherits state history up to the fork point — ops journal,
            # checkpoints and turn texts are copied so state_at(new_branch) replays correctly.
            source_journal = self.db.execute(
                "SELECT * FROM ops_journal WHERE branch_id=? AND turn_hi<=? ORDER BY id",
                (source_branch, fork_turn),
            ).fetchall()
            journal_id_map: dict[int, int] = {}
            for journal_row in source_journal:
                cursor = self.db.execute(
                    "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts,"
                    " lifecycle_source, response_occurrence_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        bid, journal_row["turn_lo"], journal_row["turn_hi"],
                        journal_row["ops"], journal_row["source"], journal_row["ts"],
                        journal_row["lifecycle_source"],
                        journal_row["response_occurrence_id"],
                    ),
                )
                journal_id_map[int(journal_row["id"])] = int(cursor.lastrowid)
            self.db.execute(
                "INSERT INTO effect_receipts(branch_id, effect_id, turn_index, family, target,"
                " direction, delta, payload_hash, owner, source, status, ts,"
                " lifecycle_source, response_occurrence_id)"
                " SELECT ?, effect_id, turn_index, family, target, direction, delta, payload_hash,"
                " owner, source, status, ts, lifecycle_source, response_occurrence_id"
                " FROM effect_receipts"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO mechanic_settlement_receipts(branch_id, settlement_ref, turn_index,"
                " contract_id, frame_ref, meaning_ref, outcome, outcome_quality,"
                " requirement_fingerprint, request_fingerprint, accepted_group_fingerprint,"
                " receipt_fingerprint, receipt_json, source, status, ts,"
                " lifecycle_source, response_occurrence_id)"
                " SELECT ?, settlement_ref, turn_index, contract_id, frame_ref, meaning_ref,"
                " outcome, outcome_quality, requirement_fingerprint, request_fingerprint,"
                " accepted_group_fingerprint, receipt_fingerprint, receipt_json, source, status,"
                " ts, lifecycle_source, response_occurrence_id"
                " FROM mechanic_settlement_receipts"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO claim_records(branch_id, claim_id, origin_branch, session_id,"
                " world_id, turn_index, source, fingerprint, record_json, status, ts,"
                " lifecycle_source, response_occurrence_id)"
                " SELECT ?, claim_id, origin_branch, session_id, world_id, turn_index, source,"
                " fingerprint, record_json, status, ts, lifecycle_source,"
                " response_occurrence_id FROM claim_records"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO world_event_records(branch_id, event_id, origin_branch, session_id,"
                " world_id, turn_index, kind, relation_target, source, fingerprint, record_json,"
                " status, ts, lifecycle_source, response_occurrence_id)"
                " SELECT ?, event_id, origin_branch, session_id, world_id, turn_index, kind,"
                " relation_target, source, fingerprint, record_json, status, ts,"
                " lifecycle_source, response_occurrence_id"
                " FROM world_event_records"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO checkpoints(branch_id, turn_index, state)"
                " SELECT ?, turn_index, state FROM checkpoints"
                " WHERE branch_id=? AND turn_index<=?", (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO turn_texts(branch_id, turn_index, user_text, assistant_text)"
                " SELECT ?, turn_index, user_text, assistant_text FROM turn_texts"
                " WHERE branch_id=? AND turn_index<=?", (bid, source_branch, fork_turn))
            accepted_receipts = self.db.execute(
                "SELECT turn_index, lifecycle_source, response_occurrence_id,"
                " source_message_fingerprint"
                " FROM chat_accepted_message_receipts"
                " WHERE branch_id=? AND turn_index<=?"
                " ORDER BY turn_index, lifecycle_source",
                (source_branch, fork_turn),
            ).fetchall()
            for receipt in accepted_receipts:
                self._insert_chat_message_receipt_locked(
                    bid,
                    int(receipt["turn_index"]),
                    str(receipt["lifecycle_source"]),
                    str(receipt["response_occurrence_id"] or ""),
                    str(receipt["source_message_fingerprint"] or ""),
                )
            # memory index rows follow the spine (02 SS10); fresh ids, parent links remapped
            mrows = self.db.execute(
                "SELECT * FROM memories WHERE branch_id=? AND created_turn<=?",
                (source_branch, fork_turn)).fetchall()
            memory_refs: dict[str, str] = {}
            idmap: dict[str, str] = {}
            for memory_row in mrows:
                source_ref = str(memory_row["journal_op_ref"] or "")
                target_ref = ""
                if source_ref:
                    row_id, separator, op_index = source_ref.partition(":")
                    mapped_id = journal_id_map.get(int(row_id)) if row_id.isdigit() else None
                    if separator and mapped_id is not None and op_index.isdigit():
                        target_ref = f"{mapped_id}:{op_index}"
                source_memory_id = str(memory_row["memory_id"])
                memory_refs[source_memory_id] = target_ref
                idmap[source_memory_id] = (
                    "memory:" + hashlib.sha256(target_ref.encode("utf-8")).hexdigest()
                    if target_ref
                    else _ulid()
                )
            for r in mrows:
                target_ref = memory_refs[str(r["memory_id"])]
                target_journal_id = int(target_ref.partition(":")[0]) if target_ref else None
                try:
                    source_refs = json.loads(r["source_journal_op_refs"] or "[]")
                except (TypeError, ValueError):
                    source_refs = []
                target_source_refs = []
                for source_ref in source_refs if isinstance(source_refs, list) else []:
                    row_id, separator, op_index = str(source_ref).partition(":")
                    mapped_id = journal_id_map.get(int(row_id)) if row_id.isdigit() else None
                    if separator and mapped_id is not None and op_index.isdigit():
                        target_source_refs.append(f"{mapped_id}:{op_index}")
                self.db.execute(
                    "INSERT INTO memories(memory_id, session_id, branch_id, tier, text,"
                    " participants, location_id, tags, importance, created_turn,"
                    " last_accessed_turn, parent_id, scene_index, visibility, scoped_actors,"
                    " journal_op_id, journal_op_ref, lifecycle_source,"
                    " response_occurrence_id, source_message_fingerprint,"
                    " source_journal_op_refs)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (idmap[r["memory_id"]], sid, bid, r["tier"], r["text"], r["participants"],
                     r["location_id"], r["tags"], r["importance"], r["created_turn"],
                     r["last_accessed_turn"], idmap.get(r["parent_id"]), r["scene_index"],
                     r["visibility"], r["scoped_actors"], target_journal_id, target_ref,
                     r["lifecycle_source"], r["response_occurrence_id"],
                     r["source_message_fingerprint"], json.dumps(target_source_refs)))
            user_receipts = self.db.execute(
                "SELECT * FROM chat_user_text_receipts"
                " WHERE branch_id=? AND turn_index<=?",
                (source_branch, fork_turn),
            ).fetchall()
            for receipt in user_receipts:
                mapped_journal_id = journal_id_map.get(int(receipt["journal_op_id"]))
                if mapped_journal_id is None:
                    continue
                self.db.execute(
                    "INSERT INTO chat_user_text_receipts("
                    "branch_id, turn_index, source_message_fingerprint,"
                    " journal_op_id, committed_at) VALUES(?,?,?,?,?)",
                    (
                        bid,
                        receipt["turn_index"],
                        receipt["source_message_fingerprint"],
                        mapped_journal_id,
                        receipt["committed_at"],
                    ),
                )
            recall_rows = self.db.execute(
                "SELECT * FROM recall_records"
                " WHERE branch_id=? AND source_turn<=? ORDER BY id",
                (source_branch, fork_turn),
            ).fetchall()
            for recall_row in recall_rows:
                try:
                    source_refs = json.loads(recall_row["journal_op_refs"] or "[]")
                except (TypeError, ValueError):
                    source_refs = []
                target_refs = []
                for source_ref in source_refs if isinstance(source_refs, list) else []:
                    row_id, separator, op_index = str(source_ref).partition(":")
                    mapped_id = journal_id_map.get(int(row_id)) if row_id.isdigit() else None
                    if separator and mapped_id is not None and op_index.isdigit():
                        target_refs.append(f"{mapped_id}:{op_index}")
                self.db.execute(
                    "INSERT INTO recall_records("
                    "session_id, branch_id, for_turn, source_turn, lifecycle_source,"
                    " response_occurrence_id, source_message_fingerprint,"
                    " journal_op_refs, lines, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        bid,
                        recall_row["for_turn"],
                        recall_row["source_turn"],
                        recall_row["lifecycle_source"],
                        recall_row["response_occurrence_id"],
                        recall_row["source_message_fingerprint"],
                        json.dumps(target_refs),
                        recall_row["lines"],
                        recall_row["created"],
                    ),
                )
            self.turn_lifecycle.fork_prefix(source_branch, bid, at_pos, fork_turn)
            self.db.execute("UPDATE sessions SET active_branch=? WHERE session_id=?", (bid, sid))
            if discard_empty_branch and discard_empty_branch not in {source_branch, bid}:
                empty = self.db.execute(
                    "SELECT session_id, head_turn FROM branches WHERE branch_id=?",
                    (discard_empty_branch,)).fetchone()
                branch_tables = ("branch_msgs", "turns", "ops_journal", "effect_receipts",
                                 "mechanic_settlement_receipts", "claim_records",
                                 "world_event_records", "checkpoints", "turn_texts",
                                 "memories", "recall_records", "chat_user_text_receipts",
                                 "chat_accepted_message_receipts",
                                 "discovery", "lint", "director",
                                 "semantic_turn_lifecycles")
                occupied = empty is None or any(
                    self.db.execute(
                        f"SELECT 1 FROM {table} WHERE branch_id=? LIMIT 1",
                        (discard_empty_branch,)).fetchone()
                    for table in branch_tables
                )
                if empty and empty["session_id"] == sid and empty["head_turn"] < 0 \
                        and not occupied:
                    self.db.execute("DELETE FROM branches WHERE branch_id=?",
                                    (discard_empty_branch,))
            if kill_source:
                self.db.execute("UPDATE branches SET status='dead' WHERE branch_id=?",
                                (source_branch,))
                dead = self.db.execute(
                    "SELECT b.branch_id FROM branches AS b"
                    " WHERE b.session_id=? AND b.status='dead'"
                    " AND NOT EXISTS (SELECT 1 FROM semantic_bootstrap_proofs AS p"
                    " WHERE p.branch_id=b.branch_id)"
                    " ORDER BY b.forked_at DESC", (row["session_id"],)).fetchall()
                for d in dead[prune_keep:]:   # prune K oldest-dead (03 SS2.3, Q3)
                    self.turn_lifecycle.delete_branch(d["branch_id"])
                    for table in (
                        "branch_msgs", "turns", "ops_journal", "effect_receipts",
                        "mechanic_settlement_receipts", "claim_records",
                        "world_event_records", "checkpoints", "turn_texts", "memories",
                        "recall_records", "chat_user_text_receipts",
                        "chat_accepted_message_receipts",
                        "discovery", "lint", "director",
                    ):
                        self.db.execute(
                            f"DELETE FROM {table} WHERE branch_id=?", (d["branch_id"],)
                        )
                    self.db.execute("DELETE FROM branches WHERE branch_id=?", (d["branch_id"],))
            return bid

    # -- turns -----------------------------------------------------------------
    def record_turn(self, branch_id: str, turn_index: int, klass: str, gen_type: str) -> None:
        with self.transaction():
            prev = self.db.execute(
                "SELECT turn_index FROM turns WHERE branch_id=? AND turn_index<? AND settled=0",
                (branch_id, turn_index)).fetchall()
            for r in prev:  # a newer turn settles everything before it (03 SS3.1, lag-1 gate)
                self.db.execute("UPDATE turns SET settled=1 WHERE branch_id=? AND turn_index=?",
                                (branch_id, r["turn_index"]))
            self.db.execute(
                "INSERT OR IGNORE INTO turns(branch_id, turn_index, klass, gen_type) VALUES(?,?,?,?)",
                (branch_id, turn_index, klass, gen_type))
            self.db.execute("UPDATE branches SET head_turn=MAX(head_turn,?) WHERE branch_id=?",
                            (turn_index, branch_id))

    def bump_swipe(self, branch_id: str) -> int:
        with self.transaction():
            row = self.db.execute("SELECT head_turn FROM branches WHERE branch_id=?",
                                  (branch_id,)).fetchone()
            if not row or row["head_turn"] < 0:
                return 0
            self.db.execute(
                "DELETE FROM chat_accepted_message_receipts"
                " WHERE branch_id=? AND turn_index=?"
                " AND lifecycle_source IN ('assistant_response','deferred_extraction')",
                (branch_id, int(row["head_turn"])),
            )
            self.db.execute(
                "UPDATE turns SET swipe_count=swipe_count+1, assistant_hash=NULL,"
                " accepted_response_occurrence_id='' "
                "WHERE branch_id=? AND turn_index=?", (branch_id, row["head_turn"]))
            got = self.db.execute("SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                                  (branch_id, row["head_turn"])).fetchone()
            return got["swipe_count"] if got else 0

    def write_turn_hashes(self, branch_id: str, turn_index: int, *,
                          user_hash: Optional[str] = None,
                          assistant_hash: Optional[str] = None,
                          accepted_response_occurrence_id: Optional[str] = None) -> None:
        """Persist content-free turn identity used by lost-reply and response dedup guards."""
        fields: list[str] = []
        values: list[object] = []
        if user_hash is not None:
            fields.append("user_hash=?")
            values.append(str(user_hash))
        if assistant_hash is not None:
            fields.append("assistant_hash=?")
            values.append(str(assistant_hash))
        if accepted_response_occurrence_id is not None:
            fields.append("accepted_response_occurrence_id=?")
            values.append(str(accepted_response_occurrence_id))
        if not fields:
            return
        with self.transaction():
            self.db.execute(
                f"UPDATE turns SET {', '.join(fields)} WHERE branch_id=? AND turn_index=?",
                (*values, branch_id, turn_index))

    def publish_chat_response_if_current(
        self,
        branch_id: str,
        turn_index: int,
        *,
        expected_swipe_count: int,
        assistant_text: str,
        assistant_hash: str,
        accepted_response_occurrence_id: str,
    ) -> bool:
        """Publish one Chat candidate only while its exact swipe attempt still owns the turn."""
        if isinstance(expected_swipe_count, bool) \
                or not isinstance(expected_swipe_count, int) \
                or expected_swipe_count < 0:
            return False
        response_id = str(accepted_response_occurrence_id or "")
        if re.fullmatch(r"response:[0-9a-f]{64}", response_id) is None:
            return False
        with self.transaction():
            updated = self.db.execute(
                "UPDATE turns SET assistant_hash=?, accepted_response_occurrence_id=?"
                " WHERE branch_id=? AND turn_index=? AND swipe_count=?"
                " AND COALESCE(accepted_response_occurrence_id, '')=''"
                " AND assistant_hash IS NULL",
                (
                    str(assistant_hash),
                    response_id,
                    branch_id,
                    int(turn_index),
                    expected_swipe_count,
                ),
            )
            if updated.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO turn_texts(branch_id, turn_index, assistant_text)"
                " VALUES(?,?,?)"
                " ON CONFLICT(branch_id, turn_index) DO UPDATE"
                " SET assistant_text=excluded.assistant_text",
                (branch_id, int(turn_index), str(assistant_text)),
            )
            source = self.db.execute(
                "SELECT user_text, assistant_text FROM turn_texts"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            ).fetchone()
            user_text = str(source["user_text"] or "") if source else ""
            accepted_text = str(source["assistant_text"] or "") if source else ""
            if user_text:
                self._insert_chat_message_receipt_locked(
                    branch_id,
                    int(turn_index),
                    "user_text",
                    "",
                    "sha256:" + hashlib.sha256(
                        user_text.encode("utf-8")
                    ).hexdigest(),
                )
            if accepted_text:
                accepted_fingerprint = "sha256:" + hashlib.sha256(
                    accepted_text.encode("utf-8")
                ).hexdigest()
                for lifecycle in (
                    "assistant_response",
                    "deferred_extraction",
                ):
                    self._insert_chat_message_receipt_locked(
                        branch_id,
                        int(turn_index),
                        lifecycle,
                        response_id,
                        accepted_fingerprint,
                    )
            return True

    def chat_user_text_admitted(
        self,
        branch_id: str,
        turn_index: int,
        source_message_fingerprint: str,
    ) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM chat_user_text_receipts r"
                " JOIN ops_journal j ON j.id=r.journal_op_id"
                " AND j.branch_id=r.branch_id"
                " AND j.turn_lo=r.turn_index AND j.turn_hi=r.turn_index"
                " AND j.source='rule' AND j.lifecycle_source='user_text'"
                " AND COALESCE(j.response_occurrence_id, '')=''"
                " WHERE r.branch_id=? AND r.turn_index=?"
                " AND r.source_message_fingerprint=?",
                (
                    str(branch_id),
                    int(turn_index),
                    str(source_message_fingerprint),
                ),
            ).fetchone()
        return row is not None

    def record_chat_user_text_admission(
        self,
        branch_id: str,
        turn_index: int,
        source_message_fingerprint: str,
        journal_op_id: int,
    ) -> None:
        fingerprint = str(source_message_fingerprint or "")
        digest = fingerprint.removeprefix("sha256:")
        if not fingerprint.startswith("sha256:") \
                or len(digest) != 64 \
                or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Chat user-text receipt requires an exact message fingerprint")
        with self.transaction():
            journal = self.db.execute(
                "SELECT branch_id, turn_lo, turn_hi, source, lifecycle_source,"
                " response_occurrence_id"
                " FROM ops_journal WHERE id=?",
                (int(journal_op_id),),
            ).fetchone()
            if journal is None \
                    or str(journal["branch_id"]) != str(branch_id) \
                    or int(journal["turn_lo"]) != int(turn_index) \
                    or int(journal["turn_hi"]) != int(turn_index) \
                    or str(journal["source"]) != "rule" \
                    or str(journal["lifecycle_source"]) != "user_text" \
                    or str(journal["response_occurrence_id"] or ""):
                raise ValueError("Chat user-text receipt lost its exact Ledger operation")
            prior = self.db.execute(
                "SELECT journal_op_id FROM chat_user_text_receipts"
                " WHERE branch_id=? AND turn_index=? AND source_message_fingerprint=?",
                (
                    str(branch_id),
                    int(turn_index),
                    fingerprint,
                ),
            ).fetchone()
            if prior is not None and int(prior["journal_op_id"]) != int(journal_op_id):
                live_prior = self.db.execute(
                    "SELECT 1 FROM ops_journal WHERE id=? AND branch_id=?"
                    " AND turn_lo=? AND turn_hi=? AND source='rule'"
                    " AND lifecycle_source='user_text'"
                    " AND COALESCE(response_occurrence_id, '')=''",
                    (
                        int(prior["journal_op_id"]),
                        str(branch_id),
                        int(turn_index),
                        int(turn_index),
                    ),
                ).fetchone()
                if live_prior is not None:
                    raise ValueError("Chat user-text receipt conflicts with prior Ledger truth")
            self.db.execute(
                "INSERT INTO chat_user_text_receipts("
                "branch_id, turn_index, source_message_fingerprint, journal_op_id, committed_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(branch_id, turn_index, source_message_fingerprint)"
                " DO UPDATE SET journal_op_id=excluded.journal_op_id,"
                " committed_at=excluded.committed_at",
                (
                    str(branch_id),
                    int(turn_index),
                    fingerprint,
                    int(journal_op_id),
                    time.time(),
                ),
            )

    # -- versioning spine (03 SS3.3) --------------------------------------------
    def journal(self, branch_id: str, turn_lo: int, turn_hi: int,
                ops: list[dict], source: str, *,
                claim_records: Optional[list[dict]] = None,
                world_event_records: Optional[list[dict]] = None,
                lifecycle_source: str = "",
                response_occurrence_id: str = "") -> int:
        lifecycle = str(lifecycle_source or source)
        response_id = str(response_occurrence_id or "")
        with self.transaction():
            self._assert_typed_record_ownership(
                branch_id, turn_lo, turn_hi, ops,
                claim_records or [], world_event_records or [],
            )
            cursor = self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts,"
                " lifecycle_source, response_occurrence_id) VALUES(?,?,?,?,?,?,?,?)",
                (
                    branch_id, turn_lo, turn_hi, json.dumps(ops), source, time.time(),
                    lifecycle, response_id,
                ))
            self._insert_typed_records(
                branch_id,
                source,
                claim_records or [],
                world_event_records or [],
                lifecycle_source=lifecycle,
                response_occurrence_id=response_id,
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _record_json(value: dict) -> str:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )

    def _assert_typed_record_ownership(
        self,
        branch_id: str,
        turn_lo: int,
        turn_hi: int,
        ops: list[dict],
        claim_records: list[dict],
        world_event_records: list[dict],
    ) -> None:
        """Bind every typed row to one exact operation in this journal transaction."""
        if not isinstance(ops, list):
            raise ValueError("journal operations must be a list")
        branch = self.db.execute(
            "SELECT session_id FROM branches WHERE branch_id=?", (branch_id,)
        ).fetchone()
        if branch is None:
            raise ValueError("typed record journal names an unknown branch")
        session_id = str(branch["session_id"])

        owned_claims = [
            op.get("_record") for op in ops
            if isinstance(op, dict) and op.get("op") == "claim_record"
            and isinstance(op.get("_record"), dict)
        ]
        owned_events = [
            op.get("event") for op in ops
            if isinstance(op, dict) and op.get("op") == "world_event_admit"
            and isinstance(op.get("event"), dict)
        ]
        if sorted(self._record_json(row) for row in owned_claims) \
                != sorted(self._record_json(row) for row in claim_records):
            raise ValueError("Claim Records do not exactly match their owning journal operations")
        if sorted(self._record_json(row) for row in owned_events) \
                != sorted(self._record_json(row) for row in world_event_records):
            raise ValueError(
                "World Event Records do not exactly match their owning journal operations"
            )
        for label, rows in (("Claim Record", claim_records),
                            ("World Event Record", world_event_records)):
            for record in rows:
                if str(record.get("branch_id") or "") != branch_id \
                        or str(record.get("session_id") or "") != session_id:
                    raise ValueError(f"{label} does not belong to its owning Ledger branch")
                turn = int(record.get("turn", record.get("turn_index", -1)))
                if not int(turn_lo) <= turn <= int(turn_hi):
                    raise ValueError(f"{label} turn lies outside its owning journal window")

    def _insert_typed_records(
        self,
        branch_id: str,
        source: str,
        claim_records: list[dict],
        world_event_records: list[dict],
        *,
        lifecycle_source: str = "",
        response_occurrence_id: str = "",
    ) -> None:
        """Publish typed records beside their owning journal row.

        Exact retries are no-ops.  Reusing an identity with changed bytes aborts the
        caller's outer transaction, so the journal, checkpoint, and typed tables
        cannot diverge.
        """
        now = time.time()
        for raw in claim_records:
            from .claim_frame import validate_claim_record

            record = validate_claim_record(raw)
            claim_id = str(record.get("claim_id") or record.get("record_id") or "")
            if not claim_id:
                raise ValueError("Claim Record has no durable identity")
            fingerprint = str(record.get("fingerprint") or "")
            prior = self.db.execute(
                "SELECT fingerprint, record_json FROM claim_records"
                " WHERE branch_id=? AND claim_id=?",
                (branch_id, claim_id),
            ).fetchone()
            encoded = self._record_json(record)
            if prior is not None:
                if str(prior["fingerprint"]) != fingerprint \
                        or str(prior["record_json"]) != encoded:
                    raise ValueError("Claim Record identity conflicts with durable Store truth")
                continue
            self.db.execute(
                "INSERT INTO claim_records(branch_id, claim_id, origin_branch, session_id,"
                " world_id, turn_index, source, fingerprint, record_json, status, ts,"
                " lifecycle_source, response_occurrence_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,'committed',?,?,?)",
                (
                    branch_id,
                    claim_id,
                    str(record.get("branch_id") or branch_id),
                    str(record.get("session_id") or ""),
                    str(record.get("world_id") or ""),
                    int(record.get("turn", record.get("turn_index", -1))),
                    source,
                    fingerprint,
                    encoded,
                    now,
                    lifecycle_source,
                    response_occurrence_id,
                ),
            )
        for raw in world_event_records:
            from .world_events import validate_world_event_record

            record = validate_world_event_record(raw)
            event_id = str(record["event_id"])
            fingerprint = str(record.get("fingerprint") or "")
            prior = self.db.execute(
                "SELECT fingerprint, record_json FROM world_event_records"
                " WHERE branch_id=? AND event_id=?",
                (branch_id, event_id),
            ).fetchone()
            encoded = self._record_json(record)
            if prior is not None:
                if str(prior["fingerprint"]) != fingerprint \
                        or str(prior["record_json"]) != encoded:
                    raise ValueError(
                        "World Event Record identity conflicts with durable Store truth"
                    )
                continue
            self.db.execute(
                "INSERT INTO world_event_records(branch_id, event_id, origin_branch, session_id,"
                " world_id, turn_index, kind, relation_target, source, fingerprint, record_json,"
                " status, ts, lifecycle_source, response_occurrence_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,'committed',?,?,?)",
                (
                    branch_id,
                    event_id,
                    str(record.get("branch_id") or branch_id),
                    str(record["session_id"]),
                    str(record["world_id"]),
                    int(record["turn"]),
                    str(record["kind"]),
                    record.get("relation_target"),
                    source,
                    fingerprint,
                    encoded,
                    now,
                    lifecycle_source,
                    response_occurrence_id,
                ),
            )

    def claim_records(self, branch_id: str, through_turn: Optional[int] = None) -> list[dict]:
        """Read and integrity-check branch-owned Claim Records in replay order."""
        sql = "SELECT fingerprint, record_json FROM claim_records WHERE branch_id=?"
        params: tuple = (branch_id,)
        if through_turn is not None:
            sql += " AND turn_index<=?"
            params = (branch_id, int(through_turn))
        sql += " ORDER BY turn_index, claim_id"
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        from .claim_frame import validate_claim_record

        out: list[dict] = []
        for row in rows:
            record = validate_claim_record(json.loads(row["record_json"]))
            if str(record.get("fingerprint") or "") != str(row["fingerprint"]):
                raise ValueError("durable Claim Record fingerprint column diverged")
            out.append(record)
        return out

    def world_event_records(
        self, branch_id: str, through_turn: Optional[int] = None
    ) -> list[dict]:
        """Read and integrity-check branch-owned World Event Records in replay order."""
        sql = "SELECT fingerprint, record_json FROM world_event_records WHERE branch_id=?"
        params: tuple = (branch_id,)
        if through_turn is not None:
            sql += " AND turn_index<=?"
            params = (branch_id, int(through_turn))
        sql += " ORDER BY turn_index, event_id"
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        from .world_events import validate_world_event_record

        out: list[dict] = []
        for row in rows:
            record = validate_world_event_record(json.loads(row["record_json"]))
            if str(record.get("fingerprint") or "") != str(row["fingerprint"]):
                raise ValueError("durable World Event Record fingerprint column diverged")
            out.append(record)
        return out

    def world_event_origin_branches(self, branch_id: str) -> list[str]:
        """Return only immutable event origins actually copied into this branch view."""
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT origin_branch FROM world_event_records"
                " WHERE branch_id=? AND origin_branch<>? ORDER BY origin_branch",
                (branch_id, branch_id),
            ).fetchall()
        return [str(row["origin_branch"]) for row in rows if row["origin_branch"]]

    def knowledge_record_scope(self, branch_id: str) -> dict:
        """Return the current session and explicit ancestor chain for typed retrieval.

        Forks copy immutable Claim/Fact/Epistemic occurrences without rewriting
        their original branch identity.  The lineage therefore grants only this
        branch and its real ancestors; sibling branches never enter the view.
        """
        with self._lock:
            rows = self.db.execute(
                "WITH RECURSIVE lineage(branch_id, session_id, parent_branch, depth) AS ("
                " SELECT branch_id, session_id, parent_branch, 0 FROM branches"
                " WHERE branch_id=?"
                " UNION ALL"
                " SELECT b.branch_id, b.session_id, b.parent_branch, lineage.depth + 1"
                " FROM branches AS b JOIN lineage ON b.branch_id=lineage.parent_branch"
                " WHERE lineage.depth < 127"
                ") SELECT branch_id, session_id, depth FROM lineage ORDER BY depth",
                (branch_id,),
            ).fetchall()
        if not rows:
            return {"session_id": "", "branch_id": branch_id, "source_branch_ids": []}
        return {
            "session_id": str(rows[0]["session_id"] or ""),
            "branch_id": str(rows[0]["branch_id"]),
            "source_branch_ids": [str(row["branch_id"]) for row in rows[1:]],
        }

    @staticmethod
    def _project_journal_rows(rows) -> list[dict]:
        """Return the canonical content-bearing journal projection in durable ID order."""
        projected: list[dict] = []
        for row in rows:
            try:
                ops = json.loads(row["ops"])
            except (json.JSONDecodeError, TypeError):
                ops = []
            projected.append({
                "id": int(row["id"]),
                "turn_lo": int(row["turn_lo"]),
                "turn_hi": int(row["turn_hi"]),
                "source": str(row["source"]),
                "ops": ops if isinstance(ops, list) else [],
            })
        return projected

    def journal_high_water(self) -> int:
        """Return the exact durable journal ID visible at the current transaction fence."""
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(id), 0) AS high_water FROM ops_journal"
            ).fetchone()
        return int(row["high_water"])

    def journal_window(
        self, branch_id: str, *, after_id: int, through_id: int
    ) -> list[dict]:
        """Project only rows inserted for ``branch_id`` inside one fenced ID window."""
        if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
            raise ValueError("journal after_id must be a non-negative integer")
        if isinstance(through_id, bool) or not isinstance(through_id, int) \
                or through_id < after_id:
            raise ValueError("journal through_id must be an integer at or above after_id")
        with self._lock:
            rows = self.db.execute(
                "SELECT id, turn_lo, turn_hi, source, ops FROM ops_journal"
                " WHERE branch_id=? AND id>? AND id<=? ORDER BY id",
                (branch_id, after_id, through_id),
            ).fetchall()
        return self._project_journal_rows(rows)

    def effect_receipts(self, branch_id: str, effect_ids: list[str]) -> dict[str, sqlite3.Row]:
        ids = [str(effect_id) for effect_id in effect_ids if effect_id]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM effect_receipts WHERE branch_id=? AND effect_id IN ({marks})",
                (branch_id, *ids)).fetchall()
        return {str(row["effect_id"]): row for row in rows}

    def mechanic_settlement_receipts(
            self, branch_id: str, settlement_refs: list[str]) -> dict[str, sqlite3.Row]:
        """Return branch-scoped whole-mechanic receipts by deterministic reference."""
        refs = [str(settlement_ref) for settlement_ref in settlement_refs if settlement_ref]
        if not refs:
            return {}
        marks = ",".join("?" for _ in refs)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM mechanic_settlement_receipts WHERE branch_id=?"
                f" AND settlement_ref IN ({marks})",
                (branch_id, *refs)).fetchall()
        return {str(row["settlement_ref"]): row for row in rows}

    def damage_claim(self, branch_id: str, turn_lo: int, turn_hi: int, family: str,
                     target: str, direction: str, owners: tuple[str, ...]) -> Optional[sqlite3.Row]:
        if not owners:
            return None
        marks = ",".join("?" for _ in owners)
        with self._lock:
            return self.db.execute(
                "SELECT * FROM effect_receipts WHERE branch_id=? AND turn_index>=?"
                " AND turn_index<=? AND family=? AND target=? AND direction=?"
                f" AND owner IN ({marks}) AND status='committed' ORDER BY ts LIMIT 1",
                (branch_id, turn_lo, turn_hi, family, target, direction, *owners)).fetchone()

    def journal_with_receipts(self, branch_id: str, turn_lo: int, turn_hi: int,
                              ops: list[dict], source: str, receipts: list[dict],
                              mechanic_receipts: Optional[list[dict]] = None, *,
                              claim_records: Optional[list[dict]] = None,
                              world_event_records: Optional[list[dict]] = None,
                              lifecycle_source: str = "",
                              response_occurrence_id: str = "") -> int:
        """Commit one journal row and its damage/mechanic receipts atomically."""
        now = time.time()
        lifecycle = str(lifecycle_source or source)
        response_id = str(response_occurrence_id or "")
        with self.transaction():
            self._assert_typed_record_ownership(
                branch_id, turn_lo, turn_hi, ops,
                claim_records or [], world_event_records or [],
            )
            cursor = self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts,"
                " lifecycle_source, response_occurrence_id) VALUES(?,?,?,?,?,?,?,?)",
                (
                    branch_id, turn_lo, turn_hi, json.dumps(ops), source, now,
                    lifecycle, response_id,
                ))
            self.db.executemany(
                "INSERT INTO effect_receipts(branch_id, effect_id, turn_index, family, target,"
                " direction, delta, payload_hash, owner, source, status, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(branch_id, r["effect_id"], turn_hi, r["family"], r["target"],
                  r["direction"], r["delta"], r["payload_hash"], r["owner"], source,
                  "committed", now) for r in receipts])
            if receipts:
                self.db.executemany(
                    "UPDATE effect_receipts SET lifecycle_source=?, response_occurrence_id=?"
                    " WHERE branch_id=? AND effect_id=?",
                    [
                        (lifecycle, response_id, branch_id, r["effect_id"])
                        for r in receipts
                    ],
                )
            self.db.executemany(
                "INSERT INTO mechanic_settlement_receipts(branch_id, settlement_ref, turn_index,"
                " contract_id, frame_ref, meaning_ref, outcome, outcome_quality,"
                " requirement_fingerprint, request_fingerprint, accepted_group_fingerprint,"
                " receipt_fingerprint, receipt_json, source, status, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(
                    branch_id,
                    r["settlement_ref"],
                    turn_hi,
                    r["contract_id"],
                    r["frame_ref"],
                    r["meaning_ref"],
                    r["outcome"],
                    r["outcome_quality"],
                    r["requirement_fingerprint"],
                    r["request_fingerprint"],
                    r["accepted_group_fingerprint"],
                    r["receipt_fingerprint"],
                    json.dumps(r["receipt"], sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False),
                    source,
                    "committed",
                    now,
                ) for r in (mechanic_receipts or [])])
            if mechanic_receipts:
                self.db.executemany(
                    "UPDATE mechanic_settlement_receipts"
                    " SET lifecycle_source=?, response_occurrence_id=?"
                    " WHERE branch_id=? AND settlement_ref=?",
                    [
                        (lifecycle, response_id, branch_id, r["settlement_ref"])
                        for r in mechanic_receipts
                    ],
                )
            self._insert_typed_records(
                branch_id,
                source,
                claim_records or [],
                world_event_records or [],
                lifecycle_source=lifecycle,
                response_occurrence_id=response_id,
            )
            return int(cursor.lastrowid)

    def rule_ops_between(self, branch_id: str, turn_lo: int, turn_hi: int) -> list[dict]:
        """Rule/user ops whose journal range sits within ``turn_lo..turn_hi``.

        A delayed extraction batch may span several turns or run after combat rows have been
        cleared.  The privileged journal is the durable evidence of foes introduced and enemy
        actions settled during that batch.  Results are flattened in journal order.
        """
        lo, hi = sorted((int(turn_lo), int(turn_hi)))
        with self._lock:
            rows = self.db.execute(
                "SELECT ops FROM ops_journal WHERE branch_id=? AND turn_lo>=? AND turn_hi<=?"
                " AND source IN ('rule','user') ORDER BY id",
                (branch_id, lo, hi)).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                out.extend(o for o in json.loads(r["ops"]) if isinstance(o, dict))
            except (ValueError, TypeError):
                continue
        return out

    def rule_ops_at(self, branch_id: str, turn_index: int) -> list[dict]:
        """All rule/user-source ops journaled exactly at ``turn_index``.

        2026-07-10 (Eranmor re-serve): the lost-turn path re-reads the settled checks of a
        turn whose reply never arrived. Read-only; never raises past sqlite.
        """
        return self.rule_ops_between(branch_id, turn_index, turn_index)

    def diagnostic_turn(self, branch_id: str, turn_index: int) -> dict:
        """Read exact branch lineage and journal rows touching one turn.

        This is intentionally a narrow local-diagnostics projection.  It contains ledger data but
        no config, request headers, credentials, or model prose.
        """
        turn = int(turn_index)
        with self._lock:
            branch = self.db.execute(
                "SELECT branch_id, session_id, parent_branch, forked_at, status, head_turn"
                " FROM branches WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            rows = self.db.execute(
                "SELECT id, turn_lo, turn_hi, source, ops FROM ops_journal"
                " WHERE branch_id=? AND turn_lo<=? AND turn_hi>=? ORDER BY id",
                (branch_id, turn, turn),
            ).fetchall()
        lineage = {
            "branch_id": branch_id,
            "session_id": str(branch["session_id"]) if branch else "",
            "parent_branch": str(branch["parent_branch"]) if branch and branch["parent_branch"] else None,
            "forked_at": branch["forked_at"] if branch else None,
            "status": str(branch["status"]) if branch else "unknown",
            "head_turn": int(branch["head_turn"]) if branch else None,
        }
        journal = self._project_journal_rows(rows)
        return {"lineage": lineage, "journal": journal}

    def checkpoint(self, branch_id: str, turn_index: int, state: dict) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO checkpoints(branch_id, turn_index, state) VALUES(?,?,?)",
                (branch_id, turn_index, json.dumps(state)))

    def state_at(self, branch_id: str, turn_index: int,
                 reducer: Callable[[dict, list[dict]], dict],
                 empty: Optional[dict] = None) -> dict:
        """Nearest checkpoint <= turn_index, then ordered replay of journaled ops through reducer."""
        with self._lock:
            ck = self.db.execute(
                "SELECT turn_index, state FROM checkpoints WHERE branch_id=? AND turn_index<=? "
                "ORDER BY turn_index DESC LIMIT 1", (branch_id, turn_index)).fetchone()
            base_turn, state = (ck["turn_index"], json.loads(ck["state"])) if ck else (-1, empty or {})
            rows = self.db.execute(
                "SELECT ops FROM ops_journal WHERE branch_id=? AND turn_hi>? AND turn_hi<=? "
                "ORDER BY id", (branch_id, base_turn, turn_index)).fetchall()
        for r in rows:
            state = reducer(state, json.loads(r["ops"]))
        return state

    # -- P2: freeze mirror + slice rows (03 SS10 currency; inspector/status reads) ----
    def set_frozen(self, session_id: str, frozen: bool) -> None:
        with self.transaction():
            self.db.execute("UPDATE sessions SET frozen=? WHERE session_id=?",
                            (1 if frozen else 0, session_id))

    def write_slice(self, session_id: str, for_turn: int, components: list) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO slices(session_id, for_turn, components, created)"
                " VALUES(?,?,?,?)",
                (session_id, for_turn, json.dumps(components), time.time()))

    def read_slice(self, session_id: str):
        with self._lock:
            return self.db.execute("SELECT * FROM slices WHERE session_id=?",
                                   (session_id,)).fetchone()

    # -- P3: extraction context prose + scheduling + capability cache -----------------
    def write_turn_text(self, branch_id: str, turn_index: int,
                        user_text: str | None = None,
                        assistant_text: str | None = None, keep: int = 60) -> None:
        """Prose retained ONLY for pending extraction context (01 SS7); pruned to `keep` turns."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO turn_texts(branch_id, turn_index) VALUES(?,?)"
                " ON CONFLICT(branch_id, turn_index) DO NOTHING", (branch_id, turn_index))
            if user_text is not None:
                self.db.execute("UPDATE turn_texts SET user_text=? WHERE branch_id=? AND turn_index=?",
                                (user_text, branch_id, turn_index))
            if assistant_text is not None:
                self.db.execute("UPDATE turn_texts SET assistant_text=? WHERE branch_id=?"
                                " AND turn_index=?", (assistant_text, branch_id, turn_index))
            self.db.execute("DELETE FROM turn_texts WHERE branch_id=? AND turn_index<="
                            "(SELECT MAX(turn_index) FROM turn_texts WHERE branch_id=?)-?",
                            (branch_id, branch_id, keep))

    def get_turn_texts(self, branch_id: str, lo: int, hi: int) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT turn_index, user_text, assistant_text FROM turn_texts"
                " WHERE branch_id=? AND turn_index>=? AND turn_index<=? ORDER BY turn_index",
                (branch_id, lo, hi)).fetchall()

    def pending_extractions(self, branch_id: str) -> list[int]:
        """Settled turns awaiting Tier-1 (restart-durable scheduling: rescan, no jobs table)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT turn_index FROM turns WHERE branch_id=? AND settled=1"
                " AND extraction='pending' ORDER BY turn_index", (branch_id,)).fetchall()
        return [r["turn_index"] for r in rows]

    def mark_extraction(self, branch_id: str, lo: int, hi: int, status: str, *,
                        expected: str | None = None) -> None:
        """Set extraction lifecycle state, optionally only from one expected state.

        The conditional form is used by cold jobs after an await.  A same-turn swipe may have
        retired that work while the model call was in flight; the stale job must not turn the
        retry's terminal ``skipped`` state back into ``failed`` or ``done``.
        """
        suffix = " AND extraction=?" if expected is not None else ""
        params: tuple[object, ...] = (status, branch_id, lo, hi)
        if expected is not None:
            params += (expected,)
        with self.transaction():
            self.db.execute("UPDATE turns SET extraction=? WHERE branch_id=?"
                            f" AND turn_index>=? AND turn_index<=?{suffix}", params)

    def extraction_range_is(self, branch_id: str, lo: int, hi: int, status: str) -> bool:
        """True only when every recorded turn in ``lo..hi`` has ``status``.

        Callers that need an atomic check-and-commit hold ``apply_guard()`` across this read and
        their reducer writes.  A swipe uses the same lock when it retires the range.
        """
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN extraction=? THEN 1 ELSE 0 END) AS matched"
                " FROM turns WHERE branch_id=? AND turn_index>=? AND turn_index<=?",
                (status, branch_id, lo, hi)).fetchone()
        return bool(row and row["total"] and row["total"] == row["matched"])

    def extraction_pending_range(self, branch_id: str, lo: int, hi: int) -> bool:
        """True only while every recorded turn in ``lo..hi`` still awaits this batch."""
        return self.extraction_range_is(branch_id, lo, hi, "pending")

    def accepted_response_occurrence_id(
        self, branch_id: str, turn_index: int
    ) -> str:
        with self._lock:
            row = self.db.execute(
                "SELECT accepted_response_occurrence_id FROM turns"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            ).fetchone()
        return str(row["accepted_response_occurrence_id"] or "") if row else ""

    def chat_extraction_compare_and_set(
        self,
        branch_id: str,
        turn_index: int,
        response_occurrence_id: str,
        status: str,
        *,
        expected: str = "pending",
    ) -> bool:
        """Transition one Chat extraction only while its accepted candidate still owns it."""
        response_id = str(response_occurrence_id or "")
        if not response_id:
            return False
        with self.transaction():
            updated = self.db.execute(
                "UPDATE turns SET extraction=? WHERE branch_id=? AND turn_index=?"
                " AND accepted_response_occurrence_id=? AND extraction=?",
                (
                    str(status),
                    branch_id,
                    int(turn_index),
                    response_id,
                    str(expected),
                ),
            )
            return updated.rowcount == 1

    def settle_head(self, branch_id: str) -> bool:
        """Idle settle (2026-07-04): the head turn normally settles only when the NEXT
        request arrives (lag-1 swipe protection), so the newest turn never extracted
        until the user sent another message. After the idle debounce it is safe to
        settle it — a later swipe is handled by the existing rollback guard (08 E7)."""
        with self.transaction():
            cur = self.db.execute(
                "UPDATE turns SET settled=1 WHERE branch_id=? AND settled=0 AND"
                " turn_index=(SELECT head_turn FROM branches WHERE branch_id=?)"
                " AND EXISTS(SELECT 1 FROM turn_texts tt WHERE tt.branch_id=turns.branch_id"
                "  AND tt.turn_index=turns.turn_index AND tt.assistant_text IS NOT NULL"
                "  AND tt.assistant_text<>'')",   # never settle a turn still generating
                (branch_id, branch_id))
            return cur.rowcount > 0

    def rollback_to(self, branch_id: str, turn_index: int) -> None:
        """03 SS3.3 swipe rollback (early flush): retract journaled state past `turn_index`
        and requeue those turns for extraction on re-settle."""
        with self.transaction():
            self.turn_lifecycle.delete_after(branch_id, turn_index)
            self.db.execute("DELETE FROM ops_journal WHERE branch_id=? AND turn_hi>?",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM effect_receipts WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM mechanic_settlement_receipts WHERE branch_id=? AND turn_index>?",
                (branch_id, turn_index))
            self.db.execute("DELETE FROM claim_records WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM world_event_records WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM checkpoints WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute("UPDATE turns SET extraction='pending' WHERE branch_id=?"
                            " AND turn_index>? AND extraction IN ('done','failed')",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM memories WHERE branch_id=? AND created_turn>?",
                            (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM recall_records WHERE branch_id=? AND source_turn>?",
                (branch_id, turn_index),
            )
            self.db.execute(
                "DELETE FROM chat_user_text_receipts"
                " WHERE branch_id=? AND turn_index>?",
                (branch_id, turn_index),
            )
            self.db.execute(
                "DELETE FROM chat_accepted_message_receipts"
                " WHERE branch_id=? AND turn_index>?",
                (branch_id, turn_index),
            )
            # members whose summary rolled back re-enter retrieval (08 L2 hierarchy intact)
            self.db.execute(
                "UPDATE memories SET parent_id=NULL WHERE branch_id=? AND parent_id IS NOT"
                " NULL AND parent_id NOT IN (SELECT memory_id FROM memories WHERE branch_id=?)",
                (branch_id, branch_id))
            self.db.execute(   # embeddings of rolled-back memories are orphans
                "DELETE FROM embeddings WHERE memory_id NOT IN"
                " (SELECT memory_id FROM memories)")
            self.db.execute("DELETE FROM lint WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM director WHERE branch_id=? AND turn_index>?",
                            (branch_id, turn_index))
            self.db.execute(          # a corrective note staged past the rollback is stale
                "DELETE FROM notes WHERE for_turn>? AND session_id="
                "(SELECT session_id FROM branches WHERE branch_id=?)",
                (turn_index + 1, branch_id))

    def retract_extraction_at(self, branch_id: str, turn_index: int) -> None:
        """Retire narrator extraction when a same-turn narration retry starts.

        User/rule mechanics (the resolved action, costs, HP receipts, enemy action and next
        intent) survive.  Narrator/extraction proposals are removed, the abandoned assistant
        prose is cleared, and this turn becomes terminal ``skipped``: replacement prose is
        continuity text, never a second mechanical settlement opportunity.
        """
        with self.transaction():
            self.db.execute("DELETE FROM ops_journal WHERE branch_id=? AND turn_hi>=?"
                            " AND source='extraction'", (branch_id, turn_index))
            self.db.execute("DELETE FROM effect_receipts WHERE branch_id=? AND turn_index>=?"
                            " AND source='extraction'", (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM mechanic_settlement_receipts WHERE branch_id=? AND turn_index>=?"
                " AND source='extraction'", (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM claim_records WHERE branch_id=? AND turn_index>=?"
                " AND source='extraction'", (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM world_event_records WHERE branch_id=? AND turn_index>=?"
                " AND source='extraction'",
                (branch_id, turn_index))
            self.db.execute("DELETE FROM checkpoints WHERE branch_id=? AND turn_index>=?",
                            (branch_id, turn_index))
            self.db.execute("UPDATE turns SET extraction='skipped' WHERE branch_id=?"
                            " AND turn_index=?", (branch_id, turn_index))
            self.db.execute("UPDATE turn_texts SET assistant_text=NULL WHERE branch_id=?"
                            " AND turn_index=?", (branch_id, turn_index))
            self.db.execute("DELETE FROM memories WHERE branch_id=? AND created_turn>=?",
                            (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM recall_records WHERE branch_id=? AND source_turn>=?",
                (branch_id, turn_index),
            )
            self.db.execute(
                "UPDATE memories SET parent_id=NULL WHERE branch_id=? AND parent_id IS NOT"
                " NULL AND parent_id NOT IN (SELECT memory_id FROM memories WHERE branch_id=?)",
                (branch_id, branch_id))
            self.db.execute("DELETE FROM embeddings WHERE memory_id NOT IN"
                            " (SELECT memory_id FROM memories)")
            self.db.execute("DELETE FROM lint WHERE branch_id=? AND turn_index>=?",
                            (branch_id, turn_index))
            self.db.execute("DELETE FROM director WHERE branch_id=? AND turn_index>=?",
                            (branch_id, turn_index))
            self.db.execute(
                "DELETE FROM notes WHERE for_turn>=? AND session_id="
                "(SELECT session_id FROM branches WHERE branch_id=?)",
                (turn_index, branch_id))

    def retract_chat_response_at(self, branch_id: str, turn_index: int) -> str:
        """Retract only the abandoned accepted Chat response lineage.

        User-text recognition survives. Assistant-response and deferred-extraction records
        tied to the exact response occurrence are retired, and the turn becomes eligible for
        the replacement response's own cold path.
        """
        with self.transaction():
            turn = self.db.execute(
                "SELECT accepted_response_occurrence_id FROM turns"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            ).fetchone()
            response_id = (
                str(turn["accepted_response_occurrence_id"] or "")
                if turn is not None
                else ""
            )
            if response_id:
                lifecycle = ("assistant_response", "deferred_extraction")
                marks = ",".join("?" for _ in lifecycle)
                self.db.execute(
                    f"DELETE FROM chat_accepted_message_receipts WHERE branch_id=?"
                    f" AND turn_index=? AND lifecycle_source IN ({marks})"
                    " AND response_occurrence_id=?",
                    (
                        branch_id,
                        int(turn_index),
                        *lifecycle,
                        response_id,
                    ),
                )
                self.db.execute(
                    f"DELETE FROM ops_journal WHERE branch_id=?"
                    f" AND lifecycle_source IN ({marks})"
                    " AND response_occurrence_id=?",
                    (branch_id, *lifecycle, response_id),
                )
                for table in (
                    "effect_receipts",
                    "mechanic_settlement_receipts",
                    "claim_records",
                    "world_event_records",
                ):
                    self.db.execute(
                        f"DELETE FROM {table} WHERE branch_id=?"
                        f" AND lifecycle_source IN ({marks})"
                        " AND response_occurrence_id=?",
                        (branch_id, *lifecycle, response_id),
                    )
                memory_rows = self.db.execute(
                    f"SELECT memory_id FROM memories WHERE branch_id=?"
                    f" AND lifecycle_source IN ({marks})"
                    " AND response_occurrence_id=?",
                    (branch_id, *lifecycle, response_id),
                ).fetchall()
                memory_ids = [str(row["memory_id"]) for row in memory_rows]
                self.db.execute(
                    f"DELETE FROM memories WHERE branch_id=?"
                    f" AND lifecycle_source IN ({marks})"
                    " AND response_occurrence_id=?",
                    (branch_id, *lifecycle, response_id),
                )
                if memory_ids:
                    memory_marks = ",".join("?" for _ in memory_ids)
                    self.db.execute(
                        f"DELETE FROM embeddings WHERE memory_id IN ({memory_marks})",
                        tuple(memory_ids),
                    )
                self.db.execute(
                    "DELETE FROM recall_records WHERE branch_id=?"
                    " AND response_occurrence_id=?",
                    (branch_id, response_id),
                )
                self.db.execute(
                    "UPDATE memories SET parent_id=NULL WHERE branch_id=?"
                    " AND parent_id IS NOT NULL"
                    " AND parent_id NOT IN ("
                    "SELECT memory_id FROM memories WHERE branch_id=?"
                    ")",
                    (branch_id, branch_id),
                )
            self.db.execute(
                "DELETE FROM checkpoints WHERE branch_id=? AND turn_index>=?",
                (branch_id, int(turn_index)),
            )
            self.db.execute(
                "UPDATE turn_texts SET assistant_text=NULL"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            )
            self.db.execute(
                "UPDATE turns SET extraction='pending', assistant_hash=NULL,"
                " accepted_response_occurrence_id=''"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            )
            return response_id

    def apply_deferred_chat_swipe(
        self,
        *,
        branch_id: str,
        turn_index: int,
        keep: int,
        expected_swipe_count: int,
    ) -> None:
        """Apply the deferred Chat transcript mutation with one exact count CAS."""
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (turn_index, keep, expected_swipe_count)
        ):
            raise ValueError("Chat swipe mutation plan is invalid")
        with self.transaction():
            branch = self.db.execute(
                "SELECT head_turn, status FROM branches WHERE branch_id=?",
                (branch_id,),
            ).fetchone()
            turn = self.db.execute(
                "SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                (branch_id, turn_index),
            ).fetchone()
            if branch is None or branch["status"] != "live" \
                    or int(branch["head_turn"]) != turn_index or turn is None:
                raise ValueError("Chat swipe branch advanced before mutation")
            if int(turn["swipe_count"] or 0) != expected_swipe_count:
                raise ValueError("Chat swipe count changed before mutation")
            self.db.execute(
                "DELETE FROM branch_msgs WHERE branch_id=? AND pos>=?",
                (branch_id, keep),
            )
            self.retract_chat_response_at(branch_id, turn_index)
            updated = self.db.execute(
                "UPDATE turns SET swipe_count=?"
                " WHERE branch_id=? AND turn_index=? AND swipe_count=?",
                (
                    expected_swipe_count + 1,
                    branch_id,
                    turn_index,
                    expected_swipe_count,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("Chat swipe lost its count CAS")

    # -- memory index (02 SS10; retrieval metadata lives HERE, not in the journal) ----
    def memories_add(self, session_id: str, branch_id: str, tier: str, text: str,
                     participants: list, location_id: Optional[str], tags: list,
                     importance: int, created_turn: int, scene_index: int, *,
                     visibility: str = "public",
                     scoped_actors: Optional[list[str]] = None,
                     journal_op_ref: str = "",
                     lifecycle_source: str = "",
                     response_occurrence_id: str = "",
                     source_message_fingerprint: str = "",
                     source_journal_op_refs: Optional[list[str]] = None) -> str:
        op_ref = str(journal_op_ref or "")
        mid = (
            "memory:" + hashlib.sha256(op_ref.encode("utf-8")).hexdigest()
            if op_ref
            else _ulid()
        )
        journal_row_id = None
        if op_ref:
            row_id, separator, op_index = op_ref.partition(":")
            if not separator or not row_id.isdigit() or not op_index.isdigit():
                raise ValueError("memory journal_op_ref must be <row id>:<op index>")
            journal_row_id = int(row_id)
        scope_json = json.dumps(sorted({
            str(actor) for actor in (scoped_actors or []) if str(actor)
        }))
        source_refs_json = json.dumps(sorted({
            str(ref) for ref in (source_journal_op_refs or []) if str(ref)
        }))
        encoded_participants = json.dumps(participants)
        encoded_tags = json.dumps(tags)
        values = (
            mid, session_id, branch_id, tier, text, encoded_participants,
            location_id, encoded_tags, importance, created_turn, created_turn,
            scene_index, str(visibility or ""), scope_json, journal_row_id, op_ref,
            str(lifecycle_source or ""), str(response_occurrence_id or ""),
            str(source_message_fingerprint or ""),
            source_refs_json,
        )
        with self.transaction():
            prior = self.db.execute(
                "SELECT * FROM memories WHERE memory_id=?", (mid,),
            ).fetchone()
            if prior is not None:
                comparable = (
                    prior["session_id"], prior["branch_id"], prior["tier"], prior["text"],
                    prior["participants"], prior["location_id"], prior["tags"],
                    prior["importance"], prior["created_turn"], prior["scene_index"],
                    prior["visibility"], prior["scoped_actors"], prior["journal_op_id"],
                    prior["journal_op_ref"], prior["lifecycle_source"],
                    prior["response_occurrence_id"], prior["source_message_fingerprint"],
                    prior["source_journal_op_refs"],
                )
                expected = (
                    session_id, branch_id, tier, text, encoded_participants,
                    location_id, encoded_tags, importance, created_turn, scene_index,
                    str(visibility or ""), scope_json, journal_row_id, op_ref,
                    str(lifecycle_source or ""), str(response_occurrence_id or ""),
                    str(source_message_fingerprint or ""),
                    source_refs_json,
                )
                if comparable != expected:
                    raise ValueError("memory mirror identity conflicts with prior bytes")
                return mid
            self.db.execute(
                "INSERT INTO memories(memory_id, session_id, branch_id, tier, text,"
                " participants, location_id, tags, importance, created_turn,"
                " last_accessed_turn, scene_index, visibility, scoped_actors,"
                " journal_op_id, journal_op_ref, lifecycle_source,"
                " response_occurrence_id, source_message_fingerprint,"
                " source_journal_op_refs)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values)
        return mid

    def turn_text_source_fingerprint(
        self,
        branch_id: str,
        turn_index: int,
        lifecycle_source: str,
        *,
        require_receipt: bool = False,
    ) -> str:
        column = (
            "user_text" if lifecycle_source == "user_text"
            else "assistant_text"
            if lifecycle_source in {"assistant_response", "deferred_extraction"}
            else ""
        )
        if not column:
            return ""
        with self._lock:
            receipt = self.db.execute(
                "SELECT response_occurrence_id, source_message_fingerprint,"
                " receipt_fingerprint FROM chat_accepted_message_receipts"
                " WHERE branch_id=? AND turn_index=? AND lifecycle_source=?",
                (
                    str(branch_id),
                    int(turn_index),
                    str(lifecycle_source),
                ),
            ).fetchone()
            if receipt is not None:
                response_id = str(
                    receipt["response_occurrence_id"] or ""
                )
                source_fingerprint = str(
                    receipt["source_message_fingerprint"] or ""
                )
                authority = {
                    "schema": "aetherstate-chat-accepted-message-receipt/1",
                    "branch_id": str(branch_id),
                    "turn_index": int(turn_index),
                    "lifecycle_source": str(lifecycle_source),
                    "response_occurrence_id": response_id,
                    "source_message_fingerprint": source_fingerprint,
                }
                if str(receipt["receipt_fingerprint"] or "") != (
                    _chat_accepted_message_receipt_fingerprint(authority)
                ):
                    return ""
                if lifecycle_source == "user_text":
                    if response_id:
                        return ""
                elif self.accepted_response_occurrence_id(
                    branch_id,
                    int(turn_index),
                ) != response_id:
                    return ""
                return source_fingerprint
            if require_receipt:
                return ""
            row = self.db.execute(
                f"SELECT {column} AS source_text FROM turn_texts"
                " WHERE branch_id=? AND turn_index=?",
                (branch_id, int(turn_index)),
            ).fetchone()
        text = str(row["source_text"] or "") if row else ""
        return (
            "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            if text
            else ""
        )

    def journal_op_refs_current(
        self,
        branch_id: str,
        refs: object,
        *,
        require: bool = False,
    ) -> bool:
        """Verify exact operation coordinates still belong to this accepted branch lineage."""
        if not isinstance(refs, list):
            return False
        normalized = sorted({str(ref) for ref in refs if str(ref)})
        if require and not normalized:
            return False
        with self._lock:
            for ref in normalized:
                row_id, separator, op_index = ref.partition(":")
                if not separator or not row_id.isdigit() or not op_index.isdigit():
                    return False
                row = self.db.execute(
                    "SELECT turn_hi, ops, lifecycle_source, response_occurrence_id"
                    " FROM ops_journal WHERE branch_id=? AND id=?",
                    (str(branch_id), int(row_id)),
                ).fetchone()
                if row is None:
                    return False
                try:
                    operations = json.loads(row["ops"])
                except (TypeError, ValueError):
                    return False
                index = int(op_index)
                if not isinstance(operations, list) or not 0 <= index < len(operations):
                    return False
                response_id = str(row["response_occurrence_id"] or "")
                if response_id:
                    accepted = self.db.execute(
                        "SELECT accepted_response_occurrence_id FROM turns"
                        " WHERE branch_id=? AND turn_index=?",
                        (str(branch_id), int(row["turn_hi"])),
                    ).fetchone()
                    if accepted is None or str(
                        accepted["accepted_response_occurrence_id"] or ""
                    ) != response_id:
                        return False
        return True

    def memory_artifact_lineage_current(self, branch_id: str, row) -> bool:
        """Validate one indexed memory/summary before it enters a Chat-derived artifact."""
        try:
            turn = int(row["created_turn"])
            lifecycle = str(row["lifecycle_source"] or "")
            response_id = str(row["response_occurrence_id"] or "")
            fingerprint = str(row["source_message_fingerprint"] or "")
            direct_ref = str(row["journal_op_ref"] or "")
            source_refs = json.loads(row["source_journal_op_refs"] or "[]")
        except (KeyError, TypeError, ValueError):
            return False
        if lifecycle not in {
            "user_text", "assistant_response", "deferred_extraction",
        } or not fingerprint:
            return False
        if lifecycle == "user_text" and response_id:
            return False
        if lifecycle in {"assistant_response", "deferred_extraction"} \
                and (
                    not response_id
                    or self.accepted_response_occurrence_id(branch_id, turn)
                    != response_id
                ):
            return False
        if fingerprint != self.turn_text_source_fingerprint(
            branch_id,
            turn,
            lifecycle,
            require_receipt=True,
        ):
            return False
        refs = [direct_ref] if direct_ref else []
        if isinstance(source_refs, list):
            refs.extend(str(ref) for ref in source_refs if str(ref))
        return self.journal_op_refs_current(branch_id, refs, require=True)

    def memories_candidates(self, branch_id: str, fetch_cap: int = 1000) -> list[sqlite3.Row]:
        """Unconsolidated rows only (parent_id IS NULL — 08 L2 exclusion), newest first;
        structured prefilter runs in Python over this capped fetch (08 L3)."""
        with self._lock:
            return self.db.execute(
                "SELECT * FROM memories WHERE branch_id=? AND parent_id IS NULL"
                " ORDER BY created_turn DESC LIMIT ?", (branch_id, fetch_cap)).fetchall()

    def memories_bump_access(self, ids: list[str], turn: int) -> None:
        with self.transaction():
            self.db.executemany("UPDATE memories SET last_accessed_turn=? WHERE memory_id=?",
                                [(turn, i) for i in ids])

    def memories_set_parent(self, ids: list[str], parent_id: str) -> None:
        with self.transaction():
            self.db.executemany("UPDATE memories SET parent_id=? WHERE memory_id=?",
                                [(parent_id, i) for i in ids])

    def memories_stale_episodic(self, branch_id: str, max_scene_index: int) -> list[sqlite3.Row]:
        """Episodic, unconsolidated, from scenes old enough to reflect (08 L2)."""
        with self._lock:
            return self.db.execute(
                "SELECT * FROM memories WHERE branch_id=? AND tier='episodic' AND"
                " parent_id IS NULL AND scene_index<=? ORDER BY scene_index, created_turn",
                (branch_id, max_scene_index)).fetchall()

    def clear_recall(
        self,
        session_id: str,
        *,
        branch_id: str,
        for_turn: int,
    ) -> None:
        with self.transaction():
            self.db.execute(
                "DELETE FROM recall_records"
                " WHERE session_id=? AND branch_id=? AND for_turn=?",
                (str(session_id), str(branch_id), int(for_turn)),
            )

    def write_recall(
        self,
        session_id: str,
        for_turn: int,
        lines: list[str],
        *,
        branch_id: str = "",
        source_turn: int = -1,
        lifecycle_source: str = "",
        response_occurrence_id: str = "",
        source_message_fingerprint: str = "",
        journal_op_refs: Optional[list[str]] = None,
        replace: bool = True,
    ) -> None:
        with self.transaction():
            if not branch_id:
                self.db.execute(
                    "INSERT OR REPLACE INTO recall(session_id, for_turn, lines, created)"
                    " VALUES(?,?,?,?)",
                    (session_id, for_turn, json.dumps(lines), time.time()),
                )
                return
            if replace:
                self.db.execute(
                    "DELETE FROM recall_records"
                    " WHERE session_id=? AND branch_id=? AND for_turn=?",
                    (str(session_id), str(branch_id), int(for_turn)),
                )
            self.db.execute(
                "INSERT INTO recall_records("
                "session_id, branch_id, for_turn, source_turn, lifecycle_source,"
                " response_occurrence_id, source_message_fingerprint,"
                " journal_op_refs, lines, created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(session_id),
                    str(branch_id),
                    int(for_turn),
                    int(source_turn),
                    str(lifecycle_source or ""),
                    str(response_occurrence_id or ""),
                    str(source_message_fingerprint or ""),
                    json.dumps(list(journal_op_refs or [])),
                    json.dumps(list(lines)),
                    time.time(),
                ),
            )

    def read_recall(
        self,
        session_id: str,
        *,
        branch_id: str = "",
        for_turn: Optional[int] = None,
        experience_mode: str = "",
    ) -> list[str]:
        if branch_id:
            query = (
                "SELECT * FROM recall_records"
                " WHERE session_id=? AND branch_id=?"
            )
            params: list[object] = [str(session_id), str(branch_id)]
            if for_turn is not None:
                query += " AND for_turn=?"
                params.append(int(for_turn))
            query += " ORDER BY id"
            with self._lock:
                rows = self.db.execute(query, tuple(params)).fetchall()
            out: list[str] = []
            for row in rows:
                lifecycle = str(row["lifecycle_source"] or "")
                response_id = str(row["response_occurrence_id"] or "")
                source_turn = int(row["source_turn"])
                if experience_mode == "chat":
                    if lifecycle not in {
                        "user_text",
                        "assistant_response",
                        "deferred_extraction",
                    }:
                        continue
                    if lifecycle == "user_text":
                        if response_id:
                            continue
                    elif not response_id:
                        continue
                if response_id:
                    current = self.accepted_response_occurrence_id(
                        branch_id,
                        source_turn,
                    )
                    if current != response_id:
                        continue
                source_fingerprint = str(
                    row["source_message_fingerprint"] or ""
                )
                current_fingerprint = self.turn_text_source_fingerprint(
                    branch_id,
                    source_turn,
                    lifecycle,
                    require_receipt=experience_mode == "chat",
                )
                if experience_mode == "chat":
                    if not source_fingerprint \
                            or source_fingerprint != current_fingerprint:
                        continue
                elif source_fingerprint \
                        and source_fingerprint != current_fingerprint:
                    continue
                try:
                    refs = json.loads(row["journal_op_refs"] or "[]")
                    values = json.loads(row["lines"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(refs, list):
                    continue
                if (refs or experience_mode == "chat") and not self.journal_op_refs_current(
                    branch_id,
                    refs,
                    require=experience_mode == "chat",
                ):
                    continue
                if isinstance(values, list):
                    out.extend(str(value) for value in values)
            if out or experience_mode == "chat":
                return out
        with self._lock:
            row = self.db.execute(
                "SELECT lines FROM recall WHERE session_id=?",
                (session_id,),
            ).fetchone()
        try:
            return json.loads(row["lines"]) if row else []
        except (TypeError, ValueError):
            return []

    # -- P4 linter (03 SS9): violation log + staged corrective note -------------------
    def lint_add(self, branch_id: str, turn_index: int, violations: list) -> None:
        with self.transaction():
            self.db.executemany(
                "INSERT INTO lint(branch_id, turn_index, rule, severity, subjects,"
                " detail, evidence, ts) VALUES(?,?,?,?,?,?,?,?)",
                [(branch_id, turn_index, v.rule, v.severity,
                  "|".join(str(x) for x in v.subjects), v.detail, v.evidence,
                  time.time()) for v in violations])

    def lint_recent(self, branch_id: str, since_turn: int) -> set:
        """(rule, subjects) keys inside the cooldown window (dedup — 03 SS9)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT rule, subjects FROM lint WHERE branch_id=? AND turn_index>=?",
                (branch_id, since_turn)).fetchall()
        return {(r["rule"], r["subjects"]) for r in rows}

    def lint_l9_evidence(self, branch_id: str, since_turn: int):
        """Latest L9 evidence within the escalation window (Q12), else None."""
        with self._lock:
            row = self.db.execute(
                "SELECT evidence FROM lint WHERE branch_id=? AND rule='L9' AND"
                " turn_index>=? ORDER BY turn_index DESC, id DESC LIMIT 1",
                (branch_id, since_turn)).fetchone()
        return row["evidence"] if row else None

    def lint_counts(self) -> dict:
        with self._lock:
            rows = self.db.execute(
                "SELECT rule, COUNT(*) AS n FROM lint GROUP BY rule").fetchall()
        return {r["rule"]: r["n"] for r in rows}

    def embeddings_missing(self, branch_id: str, limit: int) -> list:
        with self._lock:
            return self.db.execute(
                "SELECT m.memory_id, m.text FROM memories m LEFT JOIN embeddings e"
                " ON e.memory_id=m.memory_id WHERE m.branch_id=? AND e.memory_id IS NULL"
                " ORDER BY m.created_turn DESC LIMIT ?", (branch_id, limit)).fetchall()

    def embeddings_put(self, rows: list) -> None:
        """rows: (memory_id, packed_vec, dim)"""
        with self.transaction():
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings(memory_id, vec, dim) VALUES(?,?,?)",
                rows)

    def embeddings_get(self, memory_ids: list) -> dict:
        if not memory_ids:
            return {}
        with self._lock:
            q = ",".join("?" * len(memory_ids))
            rows = self.db.execute(
                f"SELECT memory_id, vec FROM embeddings WHERE memory_id IN ({q})",
                memory_ids).fetchall()
        return {r["memory_id"]: r["vec"] for r in rows}

    def summaries_unsynthesized(self, branch_id: str, limit: int) -> list:
        """Rules-digest summary rows the LLM hasn't upgraded yet (tag marker)."""
        with self._lock:
            return self.db.execute(
                "SELECT * FROM memories WHERE branch_id=? AND tier='summary'"
                " AND tags NOT LIKE '%synthesized%' ORDER BY created_turn LIMIT ?",
                (branch_id, limit)).fetchall()

    def memories_members(self, parent_id: str) -> list:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM memories WHERE parent_id=? ORDER BY created_turn",
                (parent_id,)).fetchall()

    def memories_update_text(self, memory_id: str, text: str, add_tag: str = "") -> None:
        with self.transaction():
            row = self.db.execute("SELECT tags FROM memories WHERE memory_id=?",
                                  (memory_id,)).fetchone()
            if row is None:
                return
            tags = json.loads(row["tags"] or "[]")
            if add_tag and add_tag not in tags:
                tags.append(add_tag)
            self.db.execute(
                "UPDATE memories SET text=?, tags=? WHERE memory_id=?",
                (text, json.dumps(tags), memory_id))
            self.db.execute(   # the old digest's vector no longer matches the text
                "DELETE FROM embeddings WHERE memory_id=?", (memory_id,))

    def session_mode(self, session_id: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT mode FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
        return (row["mode"] or "enriched") if row else "enriched"

    def session_mode_set(self, session_id: str, mode: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE sessions SET mode=? WHERE session_id=?",
                            (mode, session_id))

    def genesis_state(self, session_id: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT genesis FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
        return (row["genesis"] or "") if row else "skipped"

    def genesis_mark(self, session_id: str, stage: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE sessions SET genesis=? WHERE session_id=?",
                            (stage, session_id))

    def genesis_claim_llm(self, session_id: str) -> Optional[int]:
        """Claim Stage B and return its durable generation token.

        A later forced or exact structured genesis increments ``genesis_epoch``.  The old worker
        may finish its provider call, but it can no longer publish state or overwrite the newer
        marker with its stale result.
        """
        with self.transaction():
            cur = self.db.execute(
                "UPDATE sessions SET genesis='llm',"
                " genesis_epoch=COALESCE(genesis_epoch, 0)+1 WHERE session_id=? "
                "AND genesis IN ('', 'rules')", (session_id,))
            if cur.rowcount != 1:
                return None
            row = self.db.execute(
                "SELECT genesis_epoch FROM sessions WHERE session_id=?", (session_id,),
            ).fetchone()
            return int(row["genesis_epoch"]) if row is not None else None

    def genesis_claim_is_current(
        self,
        session_id: str,
        claim_epoch: int,
        branch_id: Optional[str] = None,
    ) -> bool:
        """Return whether a Stage-B worker still owns this generation and active branch."""
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM sessions WHERE session_id=? AND genesis='llm'"
                " AND genesis_epoch=? AND (? IS NULL OR active_branch=?)",
                (session_id, int(claim_epoch), branch_id, branch_id),
            ).fetchone()
        return row is not None

    def genesis_mark_if_claim(
        self,
        session_id: str,
        claim_epoch: int,
        stage: str,
        branch_id: Optional[str] = None,
    ) -> bool:
        """Compare-and-set a claim terminal, optionally requiring its original active branch."""
        with self.transaction():
            cur = self.db.execute(
                "UPDATE sessions SET genesis=? WHERE session_id=? AND genesis='llm'"
                " AND genesis_epoch=? AND (? IS NULL OR active_branch=?)",
                (stage, session_id, int(claim_epoch), branch_id, branch_id),
            )
            return cur.rowcount == 1

    def genesis_supersede(self, session_id: str, stage: str) -> Optional[int]:
        """Invalidate every older Stage-B claim and publish a newer session decision."""
        with self.transaction():
            cur = self.db.execute(
                "UPDATE sessions SET genesis=?,"
                " genesis_epoch=COALESCE(genesis_epoch, 0)+1 WHERE session_id=?",
                (stage, session_id),
            )
            if cur.rowcount != 1:
                return None
            row = self.db.execute(
                "SELECT genesis_epoch FROM sessions WHERE session_id=?", (session_id,),
            ).fetchone()
            return int(row["genesis_epoch"]) if row is not None else None

    def narrator_speaker(self, session_id: str) -> str:
        """Typed frontend/world voice protected from every entity-authority path."""
        with self._lock:
            row = self.db.execute("SELECT narrator_speaker FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
        return str(row["narrator_speaker"] or "") if row else ""

    def narrator_speaker_set(self, session_id: str, speaker: str) -> None:
        speaker = str(speaker or "").strip()[:80]
        if not speaker:
            return
        with self.transaction():
            self.db.execute("UPDATE sessions SET narrator_speaker=? WHERE session_id=?",
                            (speaker, session_id))

    def session_label_set(self, session_id: str, label: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE sessions SET label=? WHERE session_id=?",
                            ((label or "")[:120], session_id))

    # -- creator presets (2026-07-06): named world/player docs, reusable across sessions --
    def preset_save(self, kind: str, name: str, doc: dict) -> int:
        """Upsert a named preset (kind: 'world' | 'player'). Returns preset_id."""
        now = time.time()
        with self.transaction():
            self.db.execute(
                "INSERT INTO presets(kind, name, doc, created, updated) VALUES(?,?,?,?,?)"
                " ON CONFLICT(kind, name) DO UPDATE SET doc=excluded.doc, updated=excluded.updated",
                (kind, (name or "")[:120], json.dumps(doc), now, now))
            row = self.db.execute("SELECT preset_id FROM presets WHERE kind=? AND name=?",
                                  (kind, (name or "")[:120])).fetchone()
        return row["preset_id"] if row else 0

    def preset_list(self) -> list:
        with self._lock:
            rows = self.db.execute(
                "SELECT preset_id, kind, name, updated FROM presets"
                " ORDER BY kind, name COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]

    def preset_get(self, preset_id: int) -> Optional[dict]:
        with self._lock:
            row = self.db.execute("SELECT * FROM presets WHERE preset_id=?",
                                  (int(preset_id),)).fetchone()
        if not row:
            return None
        try:
            doc = json.loads(row["doc"] or "{}")
        except (json.JSONDecodeError, ValueError):
            doc = {}
        return {"preset_id": row["preset_id"], "kind": row["kind"],
                "name": row["name"], "doc": doc, "updated": row["updated"]}

    def preset_delete(self, preset_id: int) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM presets WHERE preset_id=?", (int(preset_id),))

    def session_delete(self, session_id: str) -> None:
        """Remove a session and everything under it (all branches + per-session rows)."""
        with self.transaction():
            bids = [r["branch_id"] for r in self.db.execute(
                "SELECT branch_id FROM branches WHERE session_id=?", (session_id,)).fetchall()]
            ext = self.db.execute("SELECT external_id FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
            for b in bids:
                self.turn_lifecycle.delete_branch(b)
                for tbl in ("turns", "ops_journal", "effect_receipts",
                            "mechanic_settlement_receipts", "claim_records",
                            "world_event_records", "checkpoints", "branch_msgs",
                            "turn_texts", "memories", "recall_records",
                            "chat_user_text_receipts",
                            "chat_accepted_message_receipts",
                            "lint", "director", "discovery"):
                    self.db.execute(f"DELETE FROM {tbl} WHERE branch_id=?", (b,))
                self.db.execute("DELETE FROM branches WHERE branch_id=?", (b,))
            for tbl in ("slices", "recall", "notes"):
                self.db.execute(f"DELETE FROM {tbl} WHERE session_id=?", (session_id,))
            self.db.execute(
                "DELETE FROM semantic_bootstrap_proofs WHERE session_id=?", (session_id,)
            )
            self.db.execute(
                "DELETE FROM creator_seed_receipts WHERE session_id=?", (session_id,)
            )
            self.db.execute(
                "DELETE FROM chat_core_receipts WHERE session_id=?", (session_id,)
            )
            self.db.execute("DELETE FROM embeddings WHERE memory_id NOT IN"
                            " (SELECT memory_id FROM memories)")
            if ext and ext["external_id"]:
                self.db.execute("DELETE FROM hints WHERE session_ext=?", (ext["external_id"],))
            self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def director_add(self, branch_id: str, turn_index: int, beat_id: str,
                     scene_index: int) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO director(branch_id, turn_index, beat_id, scene_index, ts)"
                " VALUES(?,?,?,?,?)",
                (branch_id, turn_index, beat_id, scene_index, time.time()))

    def director_recent(self, branch_id: str, since_turn: int) -> list:
        """Firings inside the cooldown lookback (03 SS8 cooldown/once_per_scene)."""
        with self._lock:
            return self.db.execute(
                "SELECT beat_id, turn_index, scene_index FROM director"
                " WHERE branch_id=? AND turn_index>=?",
                (branch_id, since_turn)).fetchall()

    def director_counts(self) -> dict:
        with self._lock:
            rows = self.db.execute(
                "SELECT beat_id, COUNT(*) AS n FROM director GROUP BY beat_id").fetchall()
        return {r["beat_id"]: r["n"] for r in rows}

    def write_note(self, session_id: str, for_turn: int, text: str) -> None:
        """Stage next turn's corrective note; empty text clears (one row per session)."""
        with self.transaction():
            if text:
                self.db.execute(
                    "INSERT OR REPLACE INTO notes(session_id, for_turn, text, created)"
                    " VALUES(?,?,?,?)", (session_id, for_turn, text, time.time()))
            else:
                self.db.execute("DELETE FROM notes WHERE session_id=?", (session_id,))

    def read_note(self, session_id: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT text FROM notes WHERE session_id=?",
                                  (session_id,)).fetchone()
        return row["text"] if row else ""

    def caps_get(self, base_url: str, model: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM caps WHERE base_url=? AND model=?",
                                   (base_url, model)).fetchone()

    def caps_set(self, base_url: str, model: str, rung: int,
                 native: Optional[str] = None, anyof: Optional[int] = None) -> None:
        """native/anyof=None preserve the existing values (demotion must forget neither
        the dialect nor the Q18 anyOf verdict)."""
        with self.transaction():
            if native is None or anyof is None:
                row = self.db.execute(
                    "SELECT native, anyof FROM caps WHERE base_url=? AND model=?",
                    (base_url, model)).fetchone()
                if native is None:
                    native = row["native"] if row else ""
                if anyof is None:
                    anyof = row["anyof"] if row else -1
            self.db.execute(
                "INSERT OR REPLACE INTO caps(base_url, model, rung, probed_at, failures,"
                " native, anyof) VALUES(?,?,?,?,0,?,?)",
                (base_url, model, rung, time.time(), native, anyof))

    def caps_all(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM caps ORDER BY base_url, model").fetchall()

    def caps_fail(self, base_url: str, model: str) -> int:
        """Returns the consecutive-failure count (06 A.2: 3 -> demote one rung)."""
        with self.transaction():
            self.db.execute("UPDATE caps SET failures=failures+1 WHERE base_url=? AND model=?",
                            (base_url, model))
            row = self.db.execute("SELECT failures FROM caps WHERE base_url=? AND model=?",
                                  (base_url, model)).fetchone()
            return row["failures"] if row else 0

    def caps_ok(self, base_url: str, model: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE caps SET failures=0 WHERE base_url=? AND model=?",
                            (base_url, model))

    # -- entity discovery (08 B2) ---------------------------------------------
    def discovery_bump(self, branch_id: str, name: str, turn_index: int) -> int:
        """Record one turn of evidence for a candidate name; returns distinct-turn count."""
        with self.transaction():
            row = self.db.execute("SELECT turns, status FROM discovery WHERE branch_id=?"
                                  " AND name=?", (branch_id, name)).fetchone()
            turns = json.loads(row["turns"]) if row else []
            if row and row["status"] != "counting":
                return len(turns)                    # already created/proposed: stop counting
            if turn_index not in turns:
                turns.append(turn_index)
            self.db.execute(
                "INSERT OR REPLACE INTO discovery(branch_id, name, turns, status)"
                " VALUES(?,?,?,'counting')", (branch_id, name, json.dumps(turns)))
            return len(turns)

    def discovery_mark(self, branch_id: str, name: str, status: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE discovery SET status=? WHERE branch_id=? AND name=?",
                            (status, branch_id, name))

    def discovery_rows(self, branch_id: str, status: Optional[str] = None) -> list[sqlite3.Row]:
        with self._lock:
            if status:
                return self.db.execute("SELECT * FROM discovery WHERE branch_id=? AND status=?",
                                       (branch_id, status)).fetchall()
            return self.db.execute("SELECT * FROM discovery WHERE branch_id=?",
                                   (branch_id,)).fetchall()

    def close(self) -> None:
        self.db.close()
