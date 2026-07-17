"""SQLite spine (planning/02 SS2, 03 SS3.3): sessions/branches/turns + ops journal + checkpoints.

state_at(branch, turn) = nearest checkpoint <= turn + ordered replay of journaled ops through a
caller-supplied reducer. The same primitive later drives edit-forks, swipe rollback, the replay
harness, and the inspector scrubber. Phase 2 supplies the real reducer; the spine is semantics-free.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from .worldlex_store import WorldLexStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, external_id TEXT UNIQUE, anchor_hash TEXT,
  frontend TEXT DEFAULT 'unknown', active_branch TEXT, frozen INTEGER DEFAULT 0,
  created_at REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS branches(
  branch_id TEXT PRIMARY KEY, session_id TEXT, parent_branch TEXT, forked_at INTEGER,
  status TEXT DEFAULT 'live', head_turn INTEGER DEFAULT -1);
CREATE TABLE IF NOT EXISTS turns(
  branch_id TEXT, turn_index INTEGER, user_hash TEXT, assistant_hash TEXT, chain_hash TEXT,
  klass TEXT, gen_type TEXT, swipe_count INTEGER DEFAULT 0, settled INTEGER DEFAULT 0,
  extraction TEXT DEFAULT 'pending', PRIMARY KEY(branch_id, turn_index));
CREATE TABLE IF NOT EXISTS ops_journal(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_lo INTEGER, turn_hi INTEGER,
  ops TEXT, source TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS effect_receipts(
  branch_id TEXT, effect_id TEXT, turn_index INTEGER, family TEXT, target TEXT,
  direction TEXT, delta INTEGER, payload_hash TEXT, owner TEXT, source TEXT,
  status TEXT DEFAULT 'committed', ts REAL,
  PRIMARY KEY(branch_id, effect_id));
CREATE INDEX IF NOT EXISTS idx_effect_claim
  ON effect_receipts(branch_id, turn_index, family, target, direction, owner);
CREATE TABLE IF NOT EXISTS mechanic_settlement_receipts(
  branch_id TEXT, settlement_ref TEXT, turn_index INTEGER, contract_id TEXT,
  frame_ref TEXT, meaning_ref TEXT, outcome TEXT, outcome_quality TEXT,
  requirement_fingerprint TEXT, request_fingerprint TEXT,
  accepted_group_fingerprint TEXT, receipt_fingerprint TEXT, receipt_json TEXT,
  source TEXT, status TEXT DEFAULT 'committed', ts REAL,
  PRIMARY KEY(branch_id, settlement_ref));
CREATE INDEX IF NOT EXISTS idx_mechanic_settlement_turn
  ON mechanic_settlement_receipts(branch_id, turn_index, contract_id);
CREATE TABLE IF NOT EXISTS semantic_bootstrap_proofs(
  session_id TEXT PRIMARY KEY, branch_id TEXT UNIQUE, turn_index INTEGER,
  proof_fingerprint TEXT, post_ledger_hash TEXT, journal_high_water_after INTEGER,
  proof_json TEXT, committed_at REAL);
CREATE TABLE IF NOT EXISTS claim_records(
  branch_id TEXT, claim_id TEXT, origin_branch TEXT, session_id TEXT, world_id TEXT,
  turn_index INTEGER, source TEXT, fingerprint TEXT, record_json TEXT,
  status TEXT DEFAULT 'committed', ts REAL,
  PRIMARY KEY(branch_id, claim_id));
CREATE INDEX IF NOT EXISTS idx_claim_records_turn
  ON claim_records(branch_id, turn_index, source);
CREATE TABLE IF NOT EXISTS world_event_records(
  branch_id TEXT, event_id TEXT, origin_branch TEXT, session_id TEXT, world_id TEXT,
  turn_index INTEGER, kind TEXT, relation_target TEXT, source TEXT,
  fingerprint TEXT, record_json TEXT,
  status TEXT DEFAULT 'committed', ts REAL,
  PRIMARY KEY(branch_id, event_id));
CREATE INDEX IF NOT EXISTS idx_world_event_records_turn
  ON world_event_records(branch_id, turn_index, kind);
CREATE TABLE IF NOT EXISTS checkpoints(
  branch_id TEXT, turn_index INTEGER, state TEXT, PRIMARY KEY(branch_id, turn_index));
CREATE TABLE IF NOT EXISTS branch_msgs(
  branch_id TEXT, pos INTEGER, role TEXT, content_hash TEXT, chain_hash TEXT,
  PRIMARY KEY(branch_id, pos));
CREATE TABLE IF NOT EXISTS slices(
  session_id TEXT PRIMARY KEY, for_turn INTEGER, components TEXT, created REAL);
CREATE TABLE IF NOT EXISTS turn_texts(
  branch_id TEXT, turn_index INTEGER, user_text TEXT, assistant_text TEXT,
  PRIMARY KEY(branch_id, turn_index));
CREATE TABLE IF NOT EXISTS caps(
  base_url TEXT, model TEXT, rung INTEGER, probed_at REAL, failures INTEGER DEFAULT 0,
  native TEXT DEFAULT '', anyof INTEGER DEFAULT -1,
  PRIMARY KEY(base_url, model));
CREATE TABLE IF NOT EXISTS discovery(
  branch_id TEXT, name TEXT, turns TEXT DEFAULT '[]', status TEXT DEFAULT 'counting',
  PRIMARY KEY(branch_id, name));
CREATE TABLE IF NOT EXISTS memories(
  memory_id TEXT PRIMARY KEY, session_id TEXT, branch_id TEXT, tier TEXT,
  text TEXT, participants TEXT DEFAULT '[]', location_id TEXT, tags TEXT DEFAULT '[]',
  importance INTEGER DEFAULT 3, created_turn INTEGER, last_accessed_turn INTEGER DEFAULT 0,
  parent_id TEXT, scene_index INTEGER DEFAULT 0, embedding_ref INTEGER);
CREATE INDEX IF NOT EXISTS idx_memories_branch ON memories(branch_id, parent_id);
CREATE TABLE IF NOT EXISTS recall(
  session_id TEXT PRIMARY KEY, for_turn INTEGER, lines TEXT, created REAL);
CREATE TABLE IF NOT EXISTS lint(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_index INTEGER, rule TEXT,
  severity TEXT, subjects TEXT, detail TEXT, evidence TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS idx_lint_branch ON lint(branch_id, turn_index);
CREATE TABLE IF NOT EXISTS hints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_ext TEXT, event TEXT,
  message_index INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS notes(
  session_id TEXT PRIMARY KEY, for_turn INTEGER, text TEXT, created REAL);
CREATE TABLE IF NOT EXISTS embeddings(
  memory_id TEXT PRIMARY KEY, vec BLOB, dim INTEGER);
CREATE TABLE IF NOT EXISTS director(
  id INTEGER PRIMARY KEY AUTOINCREMENT, branch_id TEXT, turn_index INTEGER,
  beat_id TEXT, scene_index INTEGER, ts REAL);
CREATE INDEX IF NOT EXISTS idx_director_branch ON director(branch_id, turn_index);
CREATE TABLE IF NOT EXISTS presets(
  preset_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, doc TEXT,
  created REAL, updated REAL, UNIQUE(kind, name));
"""

# Additive column migrations for DBs created before a column existed (pre-migration DBs).
_MIGRATIONS = [("caps", "native", "TEXT DEFAULT ''"),
               ("caps", "anyof", "INTEGER DEFAULT -1"),   # Q18 addendum (-1 unprobed)
               ("sessions", "genesis", "TEXT DEFAULT ''"),  # ''|rules|llm|done|skipped
               ("sessions", "mode", "TEXT DEFAULT 'enriched'"),  # 05 SS7: enriched|passthrough
               ("sessions", "label", "TEXT DEFAULT ''"),  # user-facing friendly name (rename)
               ("sessions", "narrator_speaker", "TEXT DEFAULT ''"),
               # Typed event ownership is needed for extraction-only retraction on old DBs.
               ("world_event_records", "source", "TEXT DEFAULT ''")]


def _ulid() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        for table, col, decl in _MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # A process can stop during the cold LLM pass. Its in-flight claim is not durable work;
        # make the session retryable on the next start.
        self.db.execute("UPDATE sessions SET genesis='rules' WHERE genesis='llm'")
        self.db.commit()
        self._lock = threading.RLock()  # apply_delta holds this across receipt + journal commit
        self.worldlex = WorldLexStore(self.db, self._lock)
        # Semantic settlement owns its own additive schema but shares this connection/lock so a
        # reducer commit and its exact replay artifact can inhabit one SQLite transaction.
        from .turn_lifecycle import TurnLifecycleStore
        self.turn_lifecycle = TurnLifecycleStore(self)

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
                "SELECT frozen, genesis, mode, narrator_speaker FROM sessions"
                " WHERE session_id=?", (source_session_id,)).fetchone()
            if source is None:
                return False
            cur = self.db.execute(
                "UPDATE sessions SET frozen=?, genesis=?, mode=?, narrator_speaker=?"
                " WHERE session_id=?",
                (source["frozen"], source["genesis"], source["mode"],
                 source["narrator_speaker"], target_session_id))
            return cur.rowcount == 1

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
                " chain_hash, klass, gen_type, swipe_count, settled, extraction)"
                " SELECT ?, turn_index, user_hash, assistant_hash, chain_hash, klass,"
                " gen_type, swipe_count, settled, extraction FROM turns"
                " WHERE branch_id=? AND turn_index<=?", (bid, source_branch, fork_turn))
            # 03 SS3.3: the fork inherits state history up to the fork point — ops journal,
            # checkpoints and turn texts are copied so state_at(new_branch) replays correctly.
            self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts)"
                " SELECT ?, turn_lo, turn_hi, ops, source, ts FROM ops_journal"
                " WHERE branch_id=? AND turn_hi<=? ORDER BY id", (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO effect_receipts(branch_id, effect_id, turn_index, family, target,"
                " direction, delta, payload_hash, owner, source, status, ts)"
                " SELECT ?, effect_id, turn_index, family, target, direction, delta, payload_hash,"
                " owner, source, status, ts FROM effect_receipts"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO mechanic_settlement_receipts(branch_id, settlement_ref, turn_index,"
                " contract_id, frame_ref, meaning_ref, outcome, outcome_quality,"
                " requirement_fingerprint, request_fingerprint, accepted_group_fingerprint,"
                " receipt_fingerprint, receipt_json, source, status, ts)"
                " SELECT ?, settlement_ref, turn_index, contract_id, frame_ref, meaning_ref,"
                " outcome, outcome_quality, requirement_fingerprint, request_fingerprint,"
                " accepted_group_fingerprint, receipt_fingerprint, receipt_json, source, status,"
                " ts FROM mechanic_settlement_receipts"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO claim_records(branch_id, claim_id, origin_branch, session_id,"
                " world_id, turn_index, source, fingerprint, record_json, status, ts)"
                " SELECT ?, claim_id, origin_branch, session_id, world_id, turn_index, source,"
                " fingerprint, record_json, status, ts FROM claim_records"
                " WHERE branch_id=? AND turn_index<=?",
                (bid, source_branch, fork_turn))
            self.db.execute(
                "INSERT INTO world_event_records(branch_id, event_id, origin_branch, session_id,"
                " world_id, turn_index, kind, relation_target, source, fingerprint, record_json,"
                " status, ts)"
                " SELECT ?, event_id, origin_branch, session_id, world_id, turn_index, kind,"
                " relation_target, source, fingerprint, record_json, status, ts"
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
            # memory index rows follow the spine (02 SS10); fresh ids, parent links remapped
            mrows = self.db.execute(
                "SELECT * FROM memories WHERE branch_id=? AND created_turn<=?",
                (source_branch, fork_turn)).fetchall()
            idmap = {r["memory_id"]: _ulid() for r in mrows}
            for r in mrows:
                self.db.execute(
                    "INSERT INTO memories(memory_id, session_id, branch_id, tier, text,"
                    " participants, location_id, tags, importance, created_turn,"
                    " last_accessed_turn, parent_id, scene_index) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (idmap[r["memory_id"]], sid, bid, r["tier"], r["text"], r["participants"],
                     r["location_id"], r["tags"], r["importance"], r["created_turn"],
                     r["last_accessed_turn"], idmap.get(r["parent_id"]), r["scene_index"]))
            self.turn_lifecycle.fork_prefix(source_branch, bid, at_pos, fork_turn)
            self.db.execute("UPDATE sessions SET active_branch=? WHERE session_id=?", (bid, sid))
            if discard_empty_branch and discard_empty_branch not in {source_branch, bid}:
                empty = self.db.execute(
                    "SELECT session_id, head_turn FROM branches WHERE branch_id=?",
                    (discard_empty_branch,)).fetchone()
                branch_tables = ("branch_msgs", "turns", "ops_journal", "effect_receipts",
                                 "mechanic_settlement_receipts", "claim_records",
                                 "world_event_records", "checkpoints", "turn_texts",
                                 "memories", "discovery", "lint", "director",
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
                "UPDATE turns SET swipe_count=swipe_count+1, assistant_hash=NULL "
                "WHERE branch_id=? AND turn_index=?", (branch_id, row["head_turn"]))
            got = self.db.execute("SELECT swipe_count FROM turns WHERE branch_id=? AND turn_index=?",
                                  (branch_id, row["head_turn"])).fetchone()
            return got["swipe_count"] if got else 0

    def write_turn_hashes(self, branch_id: str, turn_index: int, *,
                          user_hash: Optional[str] = None,
                          assistant_hash: Optional[str] = None) -> None:
        """Persist content-free turn identity used by lost-reply and response dedup guards."""
        fields: list[str] = []
        values: list[object] = []
        if user_hash is not None:
            fields.append("user_hash=?")
            values.append(str(user_hash))
        if assistant_hash is not None:
            fields.append("assistant_hash=?")
            values.append(str(assistant_hash))
        if not fields:
            return
        with self.transaction():
            self.db.execute(
                f"UPDATE turns SET {', '.join(fields)} WHERE branch_id=? AND turn_index=?",
                (*values, branch_id, turn_index))

    # -- versioning spine (03 SS3.3) --------------------------------------------
    def journal(self, branch_id: str, turn_lo: int, turn_hi: int,
                ops: list[dict], source: str, *,
                claim_records: Optional[list[dict]] = None,
                world_event_records: Optional[list[dict]] = None) -> None:
        with self.transaction():
            self._assert_typed_record_ownership(
                branch_id, turn_lo, turn_hi, ops,
                claim_records or [], world_event_records or [],
            )
            self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts)"
                " VALUES(?,?,?,?,?,?)",
                (branch_id, turn_lo, turn_hi, json.dumps(ops), source, time.time()))
            self._insert_typed_records(
                branch_id,
                source,
                claim_records or [],
                world_event_records or [],
            )

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
                " world_id, turn_index, source, fingerprint, record_json, status, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,'committed',?)",
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
                " status, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,'committed',?)",
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
                              world_event_records: Optional[list[dict]] = None) -> None:
        """Commit one journal row and its damage/mechanic receipts atomically."""
        now = time.time()
        with self.transaction():
            self._assert_typed_record_ownership(
                branch_id, turn_lo, turn_hi, ops,
                claim_records or [], world_event_records or [],
            )
            self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts)"
                " VALUES(?,?,?,?,?,?)",
                (branch_id, turn_lo, turn_hi, json.dumps(ops), source, now))
            self.db.executemany(
                "INSERT INTO effect_receipts(branch_id, effect_id, turn_index, family, target,"
                " direction, delta, payload_hash, owner, source, status, ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(branch_id, r["effect_id"], turn_hi, r["family"], r["target"],
                  r["direction"], r["delta"], r["payload_hash"], r["owner"], source,
                  "committed", now) for r in receipts])
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
            self._insert_typed_records(
                branch_id,
                source,
                claim_records or [],
                world_event_records or [],
            )

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

    # -- memory index (02 SS10; retrieval metadata lives HERE, not in the journal) ----
    def memories_add(self, session_id: str, branch_id: str, tier: str, text: str,
                     participants: list, location_id: Optional[str], tags: list,
                     importance: int, created_turn: int, scene_index: int) -> str:
        mid = _ulid()
        with self.transaction():
            self.db.execute(
                "INSERT INTO memories(memory_id, session_id, branch_id, tier, text,"
                " participants, location_id, tags, importance, created_turn,"
                " last_accessed_turn, scene_index) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, session_id, branch_id, tier, text, json.dumps(participants),
                 location_id, json.dumps(tags), importance, created_turn, created_turn,
                 scene_index))
        return mid

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

    def write_recall(self, session_id: str, for_turn: int, lines: list[str]) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO recall(session_id, for_turn, lines, created)"
                " VALUES(?,?,?,?)", (session_id, for_turn, json.dumps(lines), time.time()))

    def read_recall(self, session_id: str) -> list[str]:
        with self._lock:
            row = self.db.execute("SELECT lines FROM recall WHERE session_id=?",
                                  (session_id,)).fetchone()
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

    def genesis_claim_llm(self, session_id: str) -> bool:
        """Atomically claim Stage B so chat-open and first-request fallbacks cannot both run it."""
        with self.transaction():
            cur = self.db.execute(
                "UPDATE sessions SET genesis='llm' WHERE session_id=? "
                "AND genesis IN ('', 'rules')", (session_id,))
        return cur.rowcount == 1

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
                            "turn_texts", "memories", "lint", "director", "discovery"):
                    self.db.execute(f"DELETE FROM {tbl} WHERE branch_id=?", (b,))
                self.db.execute("DELETE FROM branches WHERE branch_id=?", (b,))
            for tbl in ("slices", "recall", "notes"):
                self.db.execute(f"DELETE FROM {tbl} WHERE session_id=?", (session_id,))
            self.db.execute(
                "DELETE FROM semantic_bootstrap_proofs WHERE session_id=?", (session_id,)
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
