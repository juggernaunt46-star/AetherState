"""Same-turn narration retries preserve code-owned mechanics and cannot reopen extraction."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from aetherstate.config import Config
from aetherstate.claim_ingress import claim_ops_from_text
from aetherstate.extraction import Endpoint, StateDelta
from aetherstate.jobs import Batch, JobRunner
from aetherstate.pipeline import Pipeline
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.extraction.mode = "assist"
    return cfg


def _seeded(cfg: Config, external_id: str) -> tuple[Store, str, str]:
    store = Store(":memory:")
    sid, branch = store.create_session(external_id=external_id)
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "entity_add", "name": "Mara Fen", "kind": "player"},
        {"op": "entity_add", "name": "Hoel", "kind": "character"},
        {"op": "player_seed", "entity": "Mara Fen", "card": {
            "stats": {"CUN": 14}, "skills": {"perception": 2},
            "resources": {"hp": {"max": 30}},
        }},
        {"op": "presence", "entity": "Mara Fen", "present": True},
    ], "genesis", cfg)
    assert seeded.applied
    return store, sid, branch


def _body() -> bytes:
    return json.dumps({
        "model": "m",
        "messages": [{"role": "user", "content": "I hold position."}],
    }).encode()


def _reply(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def _live_extraction_candidate() -> list[dict]:
    """The exact failure shape: state-idempotent presence plus an unsupported contact."""
    return [
        {"op": "presence", "entity": "Mara Fen", "present": True},
        {
            "op": "contact", "action": "start", "from_char": "Hoel",
            "to_char": "Mara Fen", "type": "unsupported_contact",
        },
    ]


def _durable_truth(store: Store, branch: str) -> dict[str, list[dict]]:
    tables = {
        "journal": ("ops_journal", "id"),
        "effect_receipts": ("effect_receipts", "effect_id"),
        "mechanic_receipts": ("mechanic_settlement_receipts", "settlement_ref"),
    }
    return {
        label: [dict(row) for row in store.db.execute(
            f"SELECT * FROM {table} WHERE branch_id=? ORDER BY {order}", (branch,)
        ).fetchall()]
        for label, (table, order) in tables.items()
    }


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _seed_code_owned_receipts(store: Store, branch: str) -> None:
    """Keep non-empty code-owned receipt rows under the swipe equality assertion."""
    receipt = {
        "schema": "mechanic-settlement/1",
        "settlement_ref": _hash("s"),
        "contract_id": "weapon_attack/1",
        "frame_ref": _hash("f"),
        "meaning_ref": _hash("m"),
        "outcome": "hit",
        "outcome_quality": "success",
        "requirement_fingerprint": _hash("q"),
        "accepted_group_fingerprint": _hash("a"),
        "receipt_fingerprint": _hash("c"),
        "applied_changes": [
            {"kind": "hp", "subject_id": "mara_fen", "delta": -1, "post": 29},
        ],
        "target_post_state": {"entity_id": "mara_fen", "hp": {"cur": 29, "max": 30}},
    }
    store.journal_with_receipts(
        branch, 1, 1,
        [{
            "op": "hp_adj", "char": "mara_fen", "delta": -1,
            "_effect_id": "dmg_swipe_live_proof", "_effect_owner": "code", "_turn": 1,
        }],
        "rule",
        [{
            "effect_id": "dmg_swipe_live_proof", "family": "hp_adj",
            "target": "mara_fen", "direction": "harm", "delta": -1,
            "payload_hash": "swipe-live-proof", "owner": "code",
        }],
        mechanic_receipts=[{
            "settlement_ref": receipt["settlement_ref"],
            "contract_id": receipt["contract_id"],
            "frame_ref": receipt["frame_ref"],
            "meaning_ref": receipt["meaning_ref"],
            "outcome": receipt["outcome"],
            "outcome_quality": receipt["outcome_quality"],
            "requirement_fingerprint": receipt["requirement_fingerprint"],
            "request_fingerprint": _hash("r"),
            "accepted_group_fingerprint": receipt["accepted_group_fingerprint"],
            "receipt_fingerprint": receipt["receipt_fingerprint"],
            "receipt": receipt,
        }],
    )


def test_live_candidate_is_state_idempotent_but_journal_mutating_without_retry_gate() -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "swipe-live-shape-probe")
    settled = apply_delta(
        store, sid, branch, 1,
        [{"op": "roll", "spec": "1d20", "result": 12}], "rule", cfg)
    assert settled.applied
    state_before = deepcopy(current_state(store, branch))
    durable_before = _durable_truth(store, branch)

    result = apply_delta(
        store, sid, branch, 1, _live_extraction_candidate(), "extraction", cfg, turn_lo=1)

    assert [op["op"] for op in result.applied] == ["presence"]
    assert len(result.quarantined) == 1
    assert result.quarantined[0]["op"]["op"] == "contact"
    assert "malformed op" in result.quarantined[0]["reason"]
    assert current_state(store, branch) == state_before
    durable_after = _durable_truth(store, branch)
    assert durable_after["journal"] != durable_before["journal"]
    assert durable_after["effect_receipts"] == durable_before["effect_receipts"]
    assert durable_after["mechanic_receipts"] == durable_before["mechanic_receipts"]


class _JobsSpy:
    def __init__(self) -> None:
        self.models: dict[str, str] = {}
        self.user_names: dict[str, str] = {}
        self.notifications: list[tuple[str, str, int]] = []

    def notify(self, session_id: str, branch_id: str, turn: int) -> None:
        self.notifications.append((session_id, branch_id, turn))


def test_swipe_response_stores_replacement_prose_but_skips_every_cold_state_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "swipe-cold-path")
    jobs = _JobsSpy()
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg, jobs=jobs)
    body = _body()
    _packet, first = pipe.process(
        Stamp(session="swipe-cold-path", gen_type="normal", turn=1, user="Mara Fen"), body)
    assert first is not None

    # Code-owned settlement and an original narrator proposal coexist on the same turn.
    apply_delta(store, sid, branch, first.turn_index,
                [{"op": "roll", "spec": "1d20", "result": 12}], "rule", cfg)
    apply_delta(store, sid, branch, first.turn_index, [{
        "op": "effect_add", "char": "Hoel", "effect": "Heavy Commitment",
        "kind": "status",
    }], "extraction", cfg)
    store.write_turn_text(
        branch, first.turn_index, user_text="Mara Fen: I hold position.",
        assistant_text="Narrator: original prose")
    store.mark_extraction(branch, first.turn_index, first.turn_index, "done")
    rule_before = deepcopy(store.rule_ops_at(branch, first.turn_index))
    assert "heavy_commitment" in current_state(store, branch)["effects"]["hoel"]

    _retry_packet, retry = pipe.process(
        Stamp(session="swipe-cold-path", gen_type="swipe", turn=1, user="Mara Fen"), body)
    assert retry is not None and retry.klass == "swipe"
    after_rollback = deepcopy(current_state(store, branch))
    assert "heavy_commitment" not in (after_rollback.get("effects", {}).get("hoel") or {})
    assert store.rule_ops_at(branch, first.turn_index) == rule_before
    cleared = store.get_turn_texts(branch, first.turn_index, first.turn_index)[0]
    assert cleared["assistant_text"] is None

    called: list[str] = []
    for name in ("_ingest_reply_tags", "_discover", "_recall_pass", "_lint_pass",
                 "_genesis_pass", "_evolve_pass"):
        original = getattr(pipe, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            called.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(pipe, name, wrapped)

    pipe.on_response(
        retry,
        _reply("Alternate prose. [status gained | Hoel | Heavy Commitment | negative]"),
        "application/json",
    )

    assert current_state(store, branch) == after_rollback
    assert store.rule_ops_at(branch, first.turn_index) == rule_before
    assert called == [] and jobs.notifications == []
    text = store.get_turn_texts(branch, first.turn_index, first.turn_index)[0]
    assert "Alternate prose" in text["assistant_text"] and "original prose" not in text["assistant_text"]
    turn = store.db.execute(
        "SELECT assistant_hash, extraction FROM turns WHERE branch_id=? AND turn_index=?",
        (branch, first.turn_index)).fetchone()
    assert turn["assistant_hash"] and turn["extraction"] == "skipped"


class _BlockingLadder:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.get_client = lambda: None

    async def extract(self, _ep, _snapshot, _characters, t0, t1, _exchange, *, context=""):
        assert (t0, t1) == (1, 1) and isinstance(context, str)
        self.started.set()
        await self.release.wait()
        return StateDelta.model_validate({
            "schema": "aetherstate/delta/1",
            "turn_range": [1, 1],
            "ops": _live_extraction_candidate(),
        })


class _BeliefLadder:
    def __init__(self) -> None:
        self.get_client = lambda: None

    async def extract(self, _ep, _snapshot, _characters, t0, t1, _exchange, *, context=""):
        assert (t0, t1) == (1, 1) and isinstance(context, str)
        return StateDelta.model_validate({
            "schema": "aetherstate/delta/2",
            "turn_range": [1, 1],
            "ops": [{
                "op": "belief_acquire",
                "holder": "Mara Fen",
                "statement": "the eastern gate is shut",
                "stance": "believes",
                "evidence_source": "told",
                "teller": "Hoel",
            }],
        })


class _UnrealizedProfessionalDeltaLadder:
    def __init__(self) -> None:
        self.get_client = lambda: None

    async def extract(self, _ep, _snapshot, _characters, t0, t1, _exchange, *, context=""):
        assert (t0, t1) == (1, 1) and isinstance(context, str)
        return StateDelta.model_validate({
            "schema": "aetherstate/delta/2",
            "turn_range": [1, 1],
            "ops": [
                {
                    "op": "affinity_adj",
                    "target": "Hoel",
                    "delta": 2,
                    "reason": "Accepted factual correction without dispute and amended his "
                              "own draft accurately",
                },
                {
                    "op": "memory_event",
                    "text": "The sealed order was handed to Hoel for direct delivery",
                },
            ],
        })


class _RealizationLeakLadder:
    def __init__(self) -> None:
        self.get_client = lambda: None

    async def extract(self, _ep, _snapshot, _characters, t0, t1, _exchange, *, context=""):
        assert (t0, t1) == (1, 1) and isinstance(context, str)
        return StateDelta.model_validate({
            "schema": "aetherstate/delta/2",
            "turn_range": [1, 1],
            "ops": [
                {
                    "op": "memory_event",
                    "text": "Mara found an invented Tribunal testimony scrap.",
                },
                {"op": "time_advance", "minutes": 30},
            ],
        })


@pytest.mark.asyncio
async def test_cold_extraction_links_belief_to_existing_claim_without_minting_another() -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "extraction-claim-link")
    store.record_turn(branch, 1, "new_turn", "normal")
    claim_result = apply_delta(
        store,
        sid,
        branch,
        1,
        claim_ops_from_text(
            "Hoel asserted that the eastern gate is shut.",
            ingress="narrator",
            source_id="narrator",
        ),
        "rule",
        cfg,
    )
    assert claim_result.applied and not claim_result.quarantined
    claims_before = store.claim_records(branch)
    assert len(claims_before) == 1
    store.write_turn_text(
        branch,
        1,
        user_text="Mara Fen: I listen.",
        assistant_text="Narrator: Hoel asserted that the eastern gate is shut.",
    )
    assert store.settle_head(branch) is True

    runner = JobRunner(store, cfg, _BeliefLadder())
    await runner._run_batch(
        Batch(sid, branch, 1, 1, 1),
        Endpoint(base_url="http://example.test", model="m"),
    )

    state = current_state(store, branch)
    belief = next(iter(state["beliefs"].values()))
    assert belief["claim_id"] == claims_before[0]["claim_id"]
    assert belief["ingress"] == "extraction"
    assert belief["authority_ceiling"] == "actor_epistemic_only"
    assert belief["establishes_truth"] is False
    assert store.claim_records(branch) == claims_before


@pytest.mark.asyncio
async def test_cold_extraction_rejects_unearned_correction_and_unrealized_handoff() -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "extraction-professional-handoff-guards")
    store.record_turn(branch, 1, "new_turn", "normal")
    store.write_turn_text(
        branch,
        1,
        user_text="Mara Fen: I identify the clerical error and wait.",
        assistant_text=(
            "Narrator: Hoel accepts the factual correction and amends his draft. "
            "His professional manner does not warm. Ivara extends the sealed order, and "
            "Hoel steps forward to receive it."
        ),
    )
    assert store.settle_head(branch) is True

    runner = JobRunner(store, cfg, _UnrealizedProfessionalDeltaLadder())
    await runner._run_batch(
        Batch(sid, branch, 1, 1, 1),
        Endpoint(base_url="http://example.test", model="m"),
    )

    state = current_state(store, branch)
    assert not state.get("affinity")
    assert not any(
        op.get("op") == "memory_event"
        for row in store.db.execute(
            "SELECT ops FROM ops_journal WHERE branch_id=?", (branch,)
        ).fetchall()
        for op in json.loads(row["ops"])
    )
    assert store.extraction_range_is(branch, 1, 1, "done")


@pytest.mark.asyncio
async def test_cold_extraction_cannot_reopen_a_current_code_owned_realization(
        monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "extraction-current-realization-fence")
    store.record_turn(branch, 1, "new_turn", "normal")
    store.write_turn_text(
        branch,
        1,
        user_text="Mara Fen: I hold position.",
        assistant_text="Narrator: The settled exchange ends. No item appears.",
    )
    assert store.settle_head(branch) is True
    monkeypatch.setattr(
        "aetherstate.jobs.build_narrator_realization_from_state",
        lambda _state: {
            "turn": 1,
            "forbidden_inference": [{
                "scope_ref": "turn:1",
                "code": "only_realized_changes_may_be_world_changes",
            }],
        },
    )

    runner = JobRunner(store, cfg, _RealizationLeakLadder())
    await runner._run_batch(
        Batch(sid, branch, 1, 1, 1),
        Endpoint(base_url="http://example.test", model="m"),
    )

    state = current_state(store, branch)
    assert not state.get("memories")
    assert state.get("clock", {}).get("minutes", 0) == 0
    extracted = [
        op
        for row in store.db.execute(
            "SELECT ops FROM ops_journal WHERE branch_id=? AND source='extraction'",
            (branch,),
        ).fetchall()
        for op in json.loads(row["ops"])
    ]
    assert extracted == []
    assert store.extraction_range_is(branch, 1, 1, "done")


@pytest.mark.asyncio
async def test_inflight_original_extraction_cannot_commit_after_swipe_retires_turn() -> None:
    cfg = _cfg()
    store, sid, branch = _seeded(cfg, "swipe-inflight")
    store.record_turn(branch, 1, "new_turn", "normal")
    _seed_code_owned_receipts(store, branch)
    store.write_turn_text(
        branch, 1, user_text="Mara Fen: I hold position.",
        assistant_text="Narrator: Mara remains present as Hoel reaches toward her.")
    assert store.settle_head(branch) is True
    assert store.extraction_pending_range(branch, 1, 1)

    ladder = _BlockingLadder()
    runner = JobRunner(store, cfg, ladder)
    task = asyncio.create_task(runner._run_batch(
        Batch(sid, branch, 1, 1, 1), Endpoint(base_url="http://example.test", model="m")))
    await asyncio.wait_for(ladder.started.wait(), timeout=1)

    # This is the hot-path order: the swipe retires and retracts before replacement prose exists.
    store.retract_extraction_at(branch, 1)
    assert store.extraction_range_is(branch, 1, 1, "skipped")
    state_after_swipe = deepcopy(current_state(store, branch))
    durable_after_swipe = _durable_truth(store, branch)
    assert len(durable_after_swipe["effect_receipts"]) == 1
    assert len(durable_after_swipe["mechanic_receipts"]) == 1
    ladder.release.set()
    await asyncio.wait_for(task, timeout=1)

    state = current_state(store, branch)
    assert state == state_after_swipe
    assert state["entities"]["mara_fen"]["present"] is True
    assert not state.get("contacts")
    assert _durable_truth(store, branch) == durable_after_swipe
    assert store.extraction_range_is(branch, 1, 1, "skipped")
    assert store.get_turn_texts(branch, 1, 1)[0]["assistant_text"] is None
    extraction_rows = store.db.execute(
        "SELECT COUNT(*) AS n FROM ops_journal WHERE branch_id=? AND source='extraction'",
        (branch,)).fetchone()
    assert extraction_rows["n"] == 0
