from __future__ import annotations

from pathlib import Path

from aetherstate.canon import canonicalize, chain
from aetherstate.capability_glossary import CapabilityGlossary
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.worldlex import DefinitionRef, OwnerRef, SubjectRef


ROOT = Path(__file__).resolve().parents[1]
WORLD = "world_" + "c" * 32


def _definition(world_id: str, definition_id: str = "harbor_inquiry") -> dict:
    glossary = CapabilityGlossary.load(ROOT / "corpus" / "capability-glossary")
    return glossary.freeze_definition({
        "kind": "skill",
        "name": "Harbor Inquiry",
        "definition_id": definition_id,
        "world_id": world_id,
        "owner_scope": "world",
        "owner_id": world_id,
        "concept_ids": ["skill.investigation"],
        "grounding_evidence": ["The harbor authority trains and records this practice."],
        "description": "Audit manifests, interview witnesses, and reconcile missing cargo.",
    })


def _ref(record: dict) -> dict:
    return DefinitionRef(
        definition_schema=record["schema"],
        definition_id=record["definition_id"],
        revision=record["revision"],
        fingerprint=record["fingerprint"],
        world_id=record["world_id"],
        kind=record["kind"],
        owner=OwnerRef(record["owner_scope"], record["owner_id"], record["world_id"]),
    ).as_dict()


def test_journaled_assignment_is_exact_replayable_and_caller_cannot_bake_authority():
    cfg = Config()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="worldlex-runtime")
    seeded = apply_delta(store, sid, branch, 0, [
        {"op": "world_identity_set", "world_id": WORLD},
        {"op": "entity_add", "name": "Mara", "entity": "actor_mara"},
    ], "user", cfg)
    assert len(seeded.applied) == 2

    record = _definition(WORLD)
    store.worldlex.append_definition(record, expected_world_id=WORLD)
    op = {
        "op": "capability_assign",
        "definition": _ref(record),
        "subject": SubjectRef("actor", "actor_mara", WORLD).as_dict(),
        "acquisition_source": "earned.case_7",
        "_assignment": {"forged": True},
    }
    result = apply_delta(store, sid, branch, 1, [op], "rule", cfg)
    assert not result.quarantined
    assignment = result.applied[0]["_assignment"]
    assert assignment["recognized"] is True
    assert assignment["authorized"] is True
    assert assignment["executable"] is False
    assert assignment["definition_snapshot"] == record
    assert assignment != {"forged": True}
    assert result.state["capability_assignments"][assignment["assignment_id"]] == assignment

    replay = current_state(store, branch)
    assert replay["capability_assignments"] == result.state["capability_assignments"]
    transcript = canonicalize([
        {"role": "user", "content": "Create Mara in this world."},
        {"role": "user", "content": "Grant Mara Harbor Inquiry after case seven."},
    ])
    heads = chain(transcript)
    store.append_msgs(
        branch,
        0,
        [
            (message.role, message.content_hash, head)
            for message, head in zip(transcript, heads)
        ],
    )
    for turn, message in enumerate(transcript):
        store.record_turn(branch, turn, "normal", "normal")
        store.write_turn_hashes(branch, turn, user_hash=message.content_hash)

    fork = store.fork_branch(branch, at_pos=len(transcript), fork_turn=1)
    assert current_state(store, fork)["capability_assignments"] == replay["capability_assignments"]


def test_assignment_requires_privileged_cause_exact_world_and_existing_actor():
    cfg = Config()
    store = Store(":memory:")
    sid, branch = store.create_session(external_id="worldlex-guards")
    apply_delta(store, sid, branch, 0,
                [{"op": "world_identity_set", "world_id": WORLD}], "user", cfg)
    record = _definition(WORLD, "guarded_inquiry")
    store.worldlex.append_definition(record, expected_world_id=WORLD)
    base = {
        "op": "capability_assign",
        "definition": _ref(record),
        "subject": SubjectRef("actor", "actor.missing", WORLD).as_dict(),
        "acquisition_source": "asserted_by_prose",
    }

    untrusted = apply_delta(store, sid, branch, 1, [base], "extraction", cfg)
    assert not untrusted.applied
    assert "only the engine or user" in untrusted.quarantined[0]["reason"]

    missing = apply_delta(store, sid, branch, 1, [base], "rule", cfg)
    assert not missing.applied
    assert "does not exist" in missing.quarantined[0]["reason"]
    assert "capability_assignments" not in current_state(store, branch)


async def test_worldlex_definition_then_noncombat_assignment_api(client):
    saved = await client.post("/aether/session/worldlex-api/world", json={"world": {
        "name": "Tide Ledger", "genre": "modern", "setting": "A working harbor city."
    }})
    assert saved.status_code == 200
    world_id = saved.json()["world_id"]
    record = _definition(world_id, "port_audit")

    stored = await client.post(
        "/aether/session/worldlex-api/worldlex/definitions", json={"definition": record}
    )
    assert stored.status_code == 200
    assert stored.json()["authorized"] is False
    assert stored.json()["executable"] is False

    assigned = await client.post(
        "/aether/session/worldlex-api/worldlex/assignments",
        json={
            "definition_ref": stored.json()["definition_ref"],
            "subject": SubjectRef(
                "institution", "institution.harbor_authority", world_id
            ).as_dict(),
            "acquisition_source": "world_genesis.charter",
        },
    )
    assert assigned.status_code == 200, assigned.text
    payload = assigned.json()
    assert payload["authorized"] is True
    assert payload["executable"] is False
    assert payload["assignment"]["subject"]["kind"] == "institution"
    assert payload["assignment"]["definition_snapshot"] == record
