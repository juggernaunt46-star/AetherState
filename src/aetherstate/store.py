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
from pathlib import Path
from typing import Callable, Optional

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
               ("sessions", "genesis", "TEXT DEFAULT ''"),  # Q23: '' | rules | done | skipped
               ("sessions", "mode", "TEXT DEFAULT 'enriched'"),  # 05 SS7: enriched|passthrough
               ("sessions", "label", "TEXT DEFAULT ''")]  # user-facing friendly name (rename)


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
        self._lock = threading.Lock()  # single-process; per-call critical sections are tiny

    # -- sessions / branches -------------------------------------------------
    def get_or_create_session(self, external_id: str) -> sqlite3.Row:
        with self._lock, self.db:
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
        with self._lock, self.db:
            sid, bid, now = _ulid(), _ulid(), time.time()
            self.db.execute("INSERT INTO branches(branch_id, session_id) VALUES(?,?)", (bid, sid))
            self.db.execute(
                "INSERT INTO sessions(session_id, external_id, anchor_hash, frontend,"
                " active_branch, created_at, last_seen) VALUES(?,?,?,?,?,?,?)",
                (sid, external_id, anchor_hash, frontend, bid, now, now))
            return sid, bid

    def touch_session(self, session_id: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE session_id=?",
                            (time.time(), session_id))

    def relink_external(self, session_id: str, external_id: str) -> None:
        """08 S4: chat rename — L3 chain evidence outranks a never-seen L1 id."""
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET external_id=NULL WHERE external_id=?",
                            (external_id,))
            self.db.execute("UPDATE sessions SET external_id=? WHERE session_id=?",
                            (external_id, session_id))

    # -- canonical transcript (L3 spine, 03 SS2.2) -------------------------------
    def append_msgs(self, branch_id: str, start_pos: int,
                    rows: list[tuple[str, str, str]]) -> None:
        """rows = [(role, content_hash, chain_hash)] appended at start_pos.."""
        with self._lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO branch_msgs(branch_id, pos, role, content_hash,"
                " chain_hash) VALUES(?,?,?,?,?)",
                [(branch_id, start_pos + i, r, ch, kh) for i, (r, ch, kh) in enumerate(rows)])

    def truncate_msgs(self, branch_id: str, keep: int) -> None:
        with self._lock, self.db:
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

    def fork_branch(self, source_branch: str, at_pos: int, fork_turn: int,
                    new_session_id: Optional[str] = None,
                    kill_source: bool = False, prune_keep: int = 3) -> str:
        """03 SS2.3 case 3 / 08 S5: copy transcript prefix [0, at_pos) + turns <= fork_turn."""
        with self._lock, self.db:
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
            self.db.execute("UPDATE sessions SET active_branch=? WHERE session_id=?", (bid, sid))
            if kill_source:
                self.db.execute("UPDATE branches SET status='dead' WHERE branch_id=?",
                                (source_branch,))
                dead = self.db.execute(
                    "SELECT branch_id FROM branches WHERE session_id=? AND status='dead'"
                    " ORDER BY forked_at DESC", (row["session_id"],)).fetchall()
                for d in dead[prune_keep:]:   # prune K oldest-dead (03 SS2.3, Q3)
                    self.db.execute("DELETE FROM branch_msgs WHERE branch_id=?",
                                    (d["branch_id"],))
                    self.db.execute("DELETE FROM turns WHERE branch_id=?", (d["branch_id"],))
                    self.db.execute("DELETE FROM branches WHERE branch_id=?", (d["branch_id"],))
            return bid

    # -- turns -----------------------------------------------------------------
    def record_turn(self, branch_id: str, turn_index: int, klass: str, gen_type: str) -> None:
        with self._lock, self.db:
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
        with self._lock, self.db:
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

    # -- versioning spine (03 SS3.3) --------------------------------------------
    def journal(self, branch_id: str, turn_lo: int, turn_hi: int,
                ops: list[dict], source: str) -> None:
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO ops_journal(branch_id, turn_lo, turn_hi, ops, source, ts)"
                " VALUES(?,?,?,?,?,?)",
                (branch_id, turn_lo, turn_hi, json.dumps(ops), source, time.time()))

    def checkpoint(self, branch_id: str, turn_index: int, state: dict) -> None:
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET frozen=? WHERE session_id=?",
                            (1 if frozen else 0, session_id))

    def write_slice(self, session_id: str, for_turn: int, components: list) -> None:
        with self._lock, self.db:
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
        with self._lock, self.db:
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

    def mark_extraction(self, branch_id: str, lo: int, hi: int, status: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE turns SET extraction=? WHERE branch_id=?"
                            " AND turn_index>=? AND turn_index<=?", (status, branch_id, lo, hi))

    def settle_head(self, branch_id: str) -> bool:
        """Idle settle (2026-07-04): the head turn normally settles only when the NEXT
        request arrives (lag-1 swipe protection), so the newest turn never extracted
        until the user sent another message. After the idle debounce it is safe to
        settle it — a later swipe is handled by the existing rollback guard (08 E7)."""
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.execute("DELETE FROM ops_journal WHERE branch_id=? AND turn_hi>?",
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

    # -- memory index (02 SS10; retrieval metadata lives HERE, not in the journal) ----
    def memories_add(self, session_id: str, branch_id: str, tier: str, text: str,
                     participants: list, location_id: Optional[str], tags: list,
                     importance: int, created_turn: int, scene_index: int) -> str:
        mid = _ulid()
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.executemany("UPDATE memories SET last_accessed_turn=? WHERE memory_id=?",
                                [(turn, i) for i in ids])

    def memories_set_parent(self, ids: list[str], parent_id: str) -> None:
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET mode=? WHERE session_id=?",
                            (mode, session_id))

    def genesis_state(self, session_id: str) -> str:
        with self._lock:
            row = self.db.execute("SELECT genesis FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
        return (row["genesis"] or "") if row else "skipped"

    def genesis_mark(self, session_id: str, stage: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET genesis=? WHERE session_id=?",
                            (stage, session_id))

    def session_label_set(self, session_id: str, label: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET label=? WHERE session_id=?",
                            ((label or "")[:120], session_id))

    # -- creator presets (2026-07-06): named world/player docs, reusable across sessions --
    def preset_save(self, kind: str, name: str, doc: dict) -> int:
        """Upsert a named preset (kind: 'world' | 'player'). Returns preset_id."""
        now = time.time()
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.execute("DELETE FROM presets WHERE preset_id=?", (int(preset_id),))

    def session_delete(self, session_id: str) -> None:
        """Remove a session and everything under it (all branches + per-session rows)."""
        with self._lock, self.db:
            bids = [r["branch_id"] for r in self.db.execute(
                "SELECT branch_id FROM branches WHERE session_id=?", (session_id,)).fetchall()]
            ext = self.db.execute("SELECT external_id FROM sessions WHERE session_id=?",
                                  (session_id,)).fetchone()
            for b in bids:
                for tbl in ("turns", "ops_journal", "checkpoints", "branch_msgs",
                            "turn_texts", "memories", "lint", "director", "discovery"):
                    self.db.execute(f"DELETE FROM {tbl} WHERE branch_id=?", (b,))
                self.db.execute("DELETE FROM branches WHERE branch_id=?", (b,))
            for tbl in ("slices", "recall", "notes"):
                self.db.execute(f"DELETE FROM {tbl} WHERE session_id=?", (session_id,))
            self.db.execute("DELETE FROM embeddings WHERE memory_id NOT IN"
                            " (SELECT memory_id FROM memories)")
            if ext and ext["external_id"]:
                self.db.execute("DELETE FROM hints WHERE session_ext=?", (ext["external_id"],))
            self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def director_add(self, branch_id: str, turn_index: int, beat_id: str,
                     scene_index: int) -> None:
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
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
        with self._lock, self.db:
            self.db.execute("UPDATE caps SET failures=failures+1 WHERE base_url=? AND model=?",
                            (base_url, model))
            row = self.db.execute("SELECT failures FROM caps WHERE base_url=? AND model=?",
                                  (base_url, model)).fetchone()
            return row["failures"] if row else 0

    def caps_ok(self, base_url: str, model: str) -> None:
        with self._lock, self.db:
            self.db.execute("UPDATE caps SET failures=0 WHERE base_url=? AND model=?",
                            (base_url, model))

    # -- entity discovery (08 B2) ---------------------------------------------
    def discovery_bump(self, branch_id: str, name: str, turn_index: int) -> int:
        """Record one turn of evidence for a candidate name; returns distinct-turn count."""
        with self._lock, self.db:
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
        with self._lock, self.db:
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
