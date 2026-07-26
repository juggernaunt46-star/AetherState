from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aetherstate import chat_continuity, memory
from aetherstate.canon import CanonMsg, chain
from aetherstate.claim_frame import build_claim_frames, build_claim_record
from aetherstate.config import Config
from aetherstate.pipeline import Pipeline
from aetherstate.semantic_fabric import load_default_semantic_fabric
from aetherstate.state import apply_delta, apply_partitioned_delta, current_state
from aetherstate.store import Store
from tests.support.schema_history import rebuild_schema_fixture
from tests.test_chat_relationship_contract import (
    CHARACTER,
    OUTSIDE_ACTOR,
    PERSONA,
    _admit_chat_identity,
    _occurrence,
)


RESPONSE = "response:" + "e" * 64


def _accepted_turn(
    store: Store,
    branch_id: str,
    turn: int,
    *,
    user_text: str,
    assistant_text: str,
    response_id: str = RESPONSE,
) -> None:
    store.record_turn(branch_id, turn, "normal", "normal")
    store.write_turn_text(branch_id, turn, user_text=user_text)
    assert store.publish_chat_response_if_current(
        branch_id,
        turn,
        expected_swipe_count=0,
        assistant_text=assistant_text,
        assistant_hash="assistant:" + hashlib.sha256(
            assistant_text.encode("utf-8")
        ).hexdigest(),
        accepted_response_occurrence_id=response_id,
    )


def _proposal(
    text: str,
    evidence: str,
    *,
    source: str = "assistant_text",
    subject: str = CHARACTER,
    participant: str = PERSONA,
    action: str = "romantic_contact",
) -> dict:
    start = text.index(evidence)
    return {
        "source_span": {
            "source": source,
            "start": start,
            "end": start + len(evidence),
        },
        "subject_actor_id": subject,
        "action_code": action,
        "polarity": "positive",
        "modality": "actual",
        "participants": [{"kind": "actor", "actor_id": participant}],
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
        "motive_claim_ref": None,
    }


def _seal(
    *,
    user_text: str = "",
    assistant_text: str = "",
    proposals: list[dict],
    turn: int = 1,
    session_id: str = "session:final-review",
    branch_id: str = "branch:final-review",
    response_id: str = RESPONSE,
    continuity_state: dict | None = None,
    claim_records: list[dict] | None = None,
) -> dict[str, list[dict]]:
    return chat_continuity.seal_accepted_chat_proposals(
        user_text=user_text,
        assistant_text=assistant_text,
        proposals=proposals,
        session_id=session_id,
        branch_id=branch_id,
        turn_index=turn,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=response_id,
        continuity_state=continuity_state,
        character_display_name="Mara",
        claim_records=claim_records,
    )


def _records(partitions: dict[str, list[dict]], op: str) -> list[dict]:
    return [
        row["record"]
        for rows in partitions.values()
        for row in rows
        if row.get("op") == op
    ]


@pytest.mark.parametrize(
    ("message", "evidence"),
    [
        ("Only if you agree.\nI kiss you.", "I kiss you."),
        ("If you agree.\nI kiss you.", "I kiss you."),
        ("In my imagined version, this happens.\nI kiss you.", "I kiss you."),
        ("I imagined this.\nI kiss you.", "I kiss you."),
        ("This is Casey speaking.\nI kiss you.", "I kiss you."),
        ("Casey says.\nI kiss you.", "I kiss you."),
        ("I kiss you.\nNo, I only imagined that.", "I kiss you."),
        ("I kiss you.\nActually, that was only imagined.", "I kiss you."),
        ("Provided that you agree.\nI kiss you.", "I kiss you."),
        ("Let's pretend.\nI kiss you.", "I kiss you."),
        ("Speaking as Casey.\nI kiss you.", "I kiss you."),
        ("I kiss you.\nNo, I made that up.", "I kiss you."),
        ("I kiss you, if you agree.", "I kiss you"),
        ("Were you to agree.\nI kiss you.", "I kiss you."),
        ("On the condition that you agree.\nI kiss you.", "I kiss you."),
        ("For the sake of argument.\nI kiss you.", "I kiss you."),
        ("In a hypothetical scenario.\nI kiss you.", "I kiss you."),
        ("In a fantasy.\nI kiss you.", "I kiss you."),
        ("I kiss you.\nThat was fictional.", "I kiss you."),
        ("I kiss you, contingent on your agreement.", "I kiss you"),
        ("Channeling Casey.\nI kiss you.", "I kiss you."),
        (
            "Let us pretend.\nThe lights dim.\nI kiss you.",
            "I kiss you.",
        ),
        (
            "If you agree.\nThen the lights dim.\nI kiss you.",
            "I kiss you.",
        ),
        (
            "In my imagination, the lights dim.\n"
            "The room falls quiet.\nI kiss you.",
            "I kiss you.",
        ),
    ],
)
def test_complete_message_context_abstains_through_tier0_owner(
    message: str,
    evidence: str,
) -> None:
    partitions = _seal(
        assistant_text=message,
        proposals=[_proposal(message, evidence)],
    )
    assert _records(partitions, "social_occurrence_admit") == []


@pytest.mark.parametrize(
    "message",
    [
        'I kiss you. Mara imagined saying, "I consent to this."',
        "I kiss you. Let us pretend. I consent to this.",
        "I kiss you. Speaking as Casey, I consent to this.",
        (
            "Only if you agree.\n"
            "I hug you.\n"
            "I kiss you.\n"
            "I consent to this."
        ),
    ],
)
def test_nonactual_typed_subclaim_invalidates_the_complete_proposal(
    message: str,
) -> None:
    proposal = _proposal(message, "I kiss you.")
    proposals = [proposal]
    if "I hug you." in message:
        proposals.insert(0, _proposal(message, "I hug you."))
    start = message.index("I consent to this.")
    proposal["consent"] = [{
        "participant": {"kind": "actor", "actor_id": CHARACTER},
        "act": "romantic_contact",
        "status": "granted",
        "channel": "in_fiction",
        "source_span": {
            "source": "assistant_text",
            "start": start,
            "end": start + len("I consent to this."),
        },
    }]
    partitions = _seal(assistant_text=message, proposals=proposals)
    assert _records(partitions, "social_occurrence_admit") == []


def test_named_addressees_resolve_only_to_one_accepted_actor() -> None:
    state = {
        "entities": {
            CHARACTER: {"kind": "character", "name": "Mara", "aliases": []},
            PERSONA: {"kind": "persona", "name": "Bean", "aliases": []},
            OUTSIDE_ACTOR: {
                "kind": "character",
                "name": "Casey",
                "aliases": ["C.J."],
            },
        },
    }
    promise = "I promise Casey I will call."
    partitions = _seal(
        assistant_text=promise,
        proposals=[_proposal(
            promise,
            promise,
            participant=OUTSIDE_ACTOR,
            action="promise_make",
        )],
        continuity_state=state,
    )
    threads = _records(partitions, "continuity_thread_transition")
    assert len(threads) == 1
    assert threads[0]["promisor_actor_id"] == CHARACTER
    assert threads[0]["promisee_actor_id"] == OUTSIDE_ACTOR

    disclosure = "I tell Casey the rain key is under the blue cup."
    disclosed = _seal(
        assistant_text=disclosure,
        proposals=[_proposal(
            disclosure,
            disclosure,
            participant=OUTSIDE_ACTOR,
            action="disclosure",
        )],
        continuity_state=state,
    )
    epistemics = [
        row
        for row in disclosed["assistant_response"]
        if row.get("op") == "belief_acquire"
    ]
    assert len(epistemics) == 1
    assert epistemics[0]["holder"] == OUTSIDE_ACTOR
    assert epistemics[0]["stance"] == "was_told"
    assert epistemics[0]["source"] == "disclosed"
    assert epistemics[0]["evidence_ref"]["kind"] == "accepted_chat_disclosure"

    agreement = "I propose to Casey that we be exclusive."
    agreed = _seal(
        assistant_text=agreement,
        proposals=[_proposal(
            agreement,
            agreement,
            participant=OUTSIDE_ACTOR,
            action="agreement_create",
        )],
        continuity_state=state,
    )
    occurrences = _records(agreed, "social_occurrence_admit")
    revisions = _records(agreed, "relationship_agreement_revision")
    assert len(occurrences) == 1
    assert occurrences[0]["outside_participants"] == [{
        "kind": "actor",
        "actor_id": OUTSIDE_ACTOR,
    }]
    assert len(revisions) == 1
    assert {
        party["actor_id"]
        for party in revisions[0]["parties"]
    } == {CHARACTER, OUTSIDE_ACTOR}

    ambiguous = json.loads(json.dumps(state))
    ambiguous["entities"]["actor:" + "4" * 64] = {
        "kind": "character",
        "name": "Jordan",
        "aliases": ["Casey"],
    }
    assert _records(
        _seal(
            assistant_text=promise,
            proposals=[_proposal(
                promise,
                promise,
                participant=OUTSIDE_ACTOR,
                action="promise_make",
            )],
            continuity_state=ambiguous,
        ),
        "social_occurrence_admit",
    ) == []

    unknown = "I promise Rowan I will call."
    assert _records(
        _seal(
            assistant_text=unknown,
            proposals=[_proposal(
                unknown,
                unknown,
                participant=OUTSIDE_ACTOR,
                action="promise_make",
            )],
            continuity_state=state,
        ),
        "social_occurrence_admit",
    ) == []

    non_actor_state = json.loads(json.dumps(state))
    non_actor_state["entities"]["location:casey"] = {
        "kind": "location",
        "name": "Casey Place",
        "aliases": ["The Casey"],
    }
    non_actor_text = "I kiss The Casey."
    non_actor = _seal(
        assistant_text=non_actor_text,
        proposals=[_proposal(
            non_actor_text,
            non_actor_text,
            participant="location:casey",
        )],
        continuity_state=non_actor_state,
    )
    assert _records(non_actor, "social_occurrence_admit") == []


def test_one_role_cannot_manufacture_agreement_and_two_exact_messages_preserve_terms() -> None:
    fabricated = "We agree to be exclusive."
    assert _records(
        _seal(
            assistant_text=fabricated,
            proposals=[_proposal(
                fabricated,
                fabricated,
                action="agreement_create",
            )],
        ),
        "relationship_agreement_revision",
    ) == []

    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="agreement-protocol")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    proposal_text = "I propose to you that we be exclusive."
    first_response = "response:" + "1" * 64
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="What kind of relationship do you want?",
        assistant_text=proposal_text,
        response_id=first_response,
    )
    proposal_parts = chat_continuity.seal_accepted_chat_proposals(
        user_text="What kind of relationship do you want?",
        assistant_text=proposal_text,
        proposals=[_proposal(
            proposal_text,
            proposal_text,
            action="agreement_create",
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=1,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=first_response,
        continuity_state=current_state(store, branch_id),
    )
    pending = _records(
        proposal_parts,
        "relationship_agreement_revision",
    )
    assert len(pending) == 1
    assert pending[0]["exclusivity"] == "exclusive"
    assert pending[0]["assent"] == [{
        "party": {"kind": "actor", "actor_id": CHARACTER},
        "status": "proposed",
        "evidence": pending[0]["assent"][0]["evidence"],
    }]
    proposed_result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        1,
        proposal_parts,
        cfg,
        expected_response_occurrence_id=first_response,
    )
    assert not proposed_result.atomic_group_failures

    acceptance = "I accept your proposal that we be exclusive."
    second_response = "response:" + "2" * 64
    _accepted_turn(
        store,
        branch_id,
        2,
        user_text=acceptance,
        assistant_text="I understand.",
        response_id=second_response,
    )
    accepted_parts = chat_continuity.seal_accepted_chat_proposals(
        user_text=acceptance,
        assistant_text="I understand.",
        proposals=[_proposal(
            acceptance,
            acceptance,
            source="user_text",
            subject=PERSONA,
            participant=CHARACTER,
            action="agreement_create",
        )],
        session_id=session_id,
        branch_id=branch_id,
        turn_index=2,
        character_actor_id=CHARACTER,
        persona_actor_id=PERSONA,
        response_occurrence_id=second_response,
        continuity_state=proposed_result.state,
    )
    revisions = _records(
        accepted_parts,
        "relationship_agreement_revision",
    )
    assert len(revisions) == 1
    assert revisions[0]["exclusivity"] == "exclusive"
    assert {
        row["party"]["actor_id"]
        for row in revisions[0]["assent"]
        if row["status"] == "accepted"
    } == {CHARACTER, PERSONA}
    assert len({
        row["evidence"]["fingerprint"]
        for row in revisions[0]["assent"]
    }) == 2


def test_open_agreement_requires_exact_allowed_acts() -> None:
    generic = "I propose to you that we be open."
    generic_parts = _seal(
        assistant_text=generic,
        proposals=[_proposal(
            generic,
            generic,
            action="agreement_create",
        )],
    )
    assert _records(generic_parts, "social_occurrence_admit") == []
    assert _records(generic_parts, "relationship_agreement_revision") == []

    exact = "I propose to you that we be open to romantic contact."
    exact_parts = _seal(
        assistant_text=exact,
        proposals=[_proposal(
            exact,
            exact,
            action="agreement_create",
        )],
    )
    revisions = _records(
        exact_parts,
        "relationship_agreement_revision",
    )
    assert len(revisions) == 1
    assert revisions[0]["exclusivity"] == "open"
    assert revisions[0]["allowed_outside_acts"] == ["romantic_contact"]

    combined = (
        "I propose to you that we be open to romantic and sexual contact."
    )
    combined_parts = _seal(
        assistant_text=combined,
        proposals=[_proposal(
            combined,
            combined,
            action="agreement_create",
        )],
    )
    combined_revisions = _records(
        combined_parts,
        "relationship_agreement_revision",
    )
    assert len(combined_revisions) == 1
    assert combined_revisions[0]["allowed_outside_acts"] == [
        "romantic_contact",
        "sexual_contact",
    ]


def test_agreement_amend_requires_two_exact_messages_and_keeps_prior_terms_active() -> None:
    proposal = "I propose to you that we be exclusive."
    proposal_row = _records(
        _seal(
            assistant_text=proposal,
            proposals=[_proposal(
                proposal,
                proposal,
                action="agreement_create",
            )],
        ),
        "relationship_agreement_revision",
    )[0]
    pending_create = chat_continuity.bake_agreement_revision(
        proposal_row,
        [],
        admitted_turn=1,
    )
    acceptance = "I accept your proposal that we be exclusive."
    accepted_row = _records(
        _seal(
            user_text=acceptance,
            proposals=[_proposal(
                acceptance,
                acceptance,
                source="user_text",
                subject=PERSONA,
                participant=CHARACTER,
                action="agreement_create",
            )],
            turn=2,
            continuity_state={
                "relationship_agreements": {
                    pending_create["agreement_id"]: [pending_create],
                },
            },
        ),
        "relationship_agreement_revision",
    )[0]
    accepted = chat_continuity.bake_agreement_revision(
        accepted_row,
        [pending_create],
        admitted_turn=2,
    )

    amendment = (
        "I propose to you that we amend our agreement "
        "to be open to romantic contact."
    )
    amendment_row = _records(
        _seal(
            assistant_text=amendment,
            proposals=[_proposal(
                amendment,
                amendment,
                action="agreement_amend",
            )],
            turn=3,
            continuity_state={
                "relationship_agreements": {
                    accepted["agreement_id"]: [pending_create, accepted],
                },
            },
        ),
        "relationship_agreement_revision",
    )[0]
    pending_amendment = chat_continuity.bake_agreement_revision(
        amendment_row,
        [pending_create, accepted],
        admitted_turn=3,
    )
    assert pending_amendment["allowed_outside_acts"] == [
        "romantic_contact",
    ]
    assert [row["status"] for row in pending_amendment["assent"]] == [
        "proposed",
    ]

    occurrence = _occurrence("occurrence.pending-amendment")
    occurrence["occurred_turn"] = 3
    occurrence = chat_continuity.bake_social_occurrence(occurrence, [])
    assessment = chat_continuity.assess_infidelity(
        [pending_create, accepted, pending_amendment],
        occurrence,
    )
    assert assessment["status"] == "violated"
    assert assessment["agreement_ref"]["fingerprint"] == accepted["fingerprint"]

    amendment_acceptance = (
        "I accept your proposal to amend our agreement "
        "to be open to romantic contact."
    )
    accepted_amendment_row = _records(
        _seal(
            user_text=amendment_acceptance,
            proposals=[_proposal(
                amendment_acceptance,
                amendment_acceptance,
                source="user_text",
                subject=PERSONA,
                participant=CHARACTER,
                action="agreement_amend",
            )],
            turn=4,
            continuity_state={
                "relationship_agreements": {
                    accepted["agreement_id"]: [
                        pending_create,
                        accepted,
                        pending_amendment,
                    ],
                },
            },
        ),
        "relationship_agreement_revision",
    )[0]
    accepted_amendment = chat_continuity.bake_agreement_revision(
        accepted_amendment_row,
        [pending_create, accepted, pending_amendment],
        admitted_turn=4,
    )
    assert accepted_amendment["allowed_outside_acts"] == [
        "romantic_contact",
    ]
    assert {
        row["party"]["actor_id"]
        for row in accepted_amendment["assent"]
        if row["status"] == "accepted"
    } == {CHARACTER, PERSONA}


def test_rules_off_passes_current_accepted_artifact_lineage_to_both_derivations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config()
    cfg.extraction.mode = "off"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="rules-off-lineage")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    assistant_text = "Mara: I remember the lighthouse key."
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="Do you remember?",
        assistant_text=assistant_text,
    )
    captured: dict[str, dict] = {}

    def fake_reflect(*args, **kwargs):
        captured["reflect"] = kwargs.get("artifact_lineage")
        return 0

    def fake_recall(*args, **kwargs):
        captured["recall"] = kwargs.get("artifact_lineage")

    monkeypatch.setattr(memory, "reflect", fake_reflect)
    monkeypatch.setattr(memory, "precompute_recall", fake_recall)
    pipeline = object.__new__(Pipeline)
    pipeline.store = store
    pipeline.cfg = cfg
    pipeline._recall_pass(
        SimpleNamespace(
            session_id=session_id,
            branch_id=branch_id,
            turn_index=1,
            experience_mode="chat",
        ),
        cfg,
    )
    expected = {
        "turn": 1,
        "lifecycle_source": "assistant_response",
        "response_occurrence_id": RESPONSE,
        "source_message_fingerprint": store.turn_text_source_fingerprint(
            branch_id,
            1,
            "assistant_response",
        ),
    }
    assert captured == {"reflect": expected, "recall": expected}


def _motive_claim(
    text: str,
    *,
    session_id: str = "session:final-review",
    branch_id: str = "branch:final-review",
    turn: int = 1,
    source_id: str = CHARACTER,
    scoped_actors: list[str] | None = None,
) -> dict:
    fabric = load_default_semantic_fabric()
    frames = build_claim_frames(
        text,
        fabric.translate(text),
        ingress="npc",
        source_id=source_id,
    )
    assert frames
    return build_claim_record(
        frames[0],
        session_id=session_id,
        branch_id=branch_id,
        world_id="world_unbound",
        turn=turn,
        source=source_id,
        visibility="actor_scoped",
        scoped_actors=scoped_actors or [CHARACTER],
        lifecycle_source="assistant_response",
        response_occurrence_id=RESPONSE,
    )


def test_motive_requires_exact_actor_assertion_and_action_relation_but_survives_fork() -> None:
    message = (
        "I kiss you. "
        "I asserted that I kissed you because I wanted reassurance."
    )
    valid = _motive_claim(message)
    proposal = _proposal(message, "I kiss you.")
    proposal["motive_claim_ref"] = {
        "claim_id": valid["claim_id"],
        "fingerprint": valid["fingerprint"],
    }
    accepted = _seal(
        assistant_text=message,
        proposals=[proposal],
        claim_records=[valid],
    )
    occurrence = _records(accepted, "social_occurrence_admit")[0]
    assert occurrence["motive_claim_ref"] == proposal["motive_claim_ref"]

    unrelated_text = (
        "I kiss you. "
        "I asserted that the locked drawer contains the rain key."
    )
    unrelated = _motive_claim(unrelated_text)
    unrelated_proposal = _proposal(unrelated_text, "I kiss you.")
    unrelated_proposal["motive_claim_ref"] = {
        "claim_id": unrelated["claim_id"],
        "fingerprint": unrelated["fingerprint"],
    }
    rejected = _seal(
        assistant_text=unrelated_text,
        proposals=[unrelated_proposal],
        claim_records=[unrelated],
    )
    assert _records(rejected, "social_occurrence_admit")[0][
        "motive_claim_ref"
    ] is None

    wrong_speaker_text = (
        "I kiss you. "
        "Casey asserted that I kissed you because I wanted reassurance."
    )
    wrong_speaker = _motive_claim(
        wrong_speaker_text,
        source_id=OUTSIDE_ACTOR,
    )
    wrong_proposal = _proposal(wrong_speaker_text, "I kiss you.")
    wrong_proposal["motive_claim_ref"] = {
        "claim_id": wrong_speaker["claim_id"],
        "fingerprint": wrong_speaker["fingerprint"],
    }
    wrong = _seal(
        assistant_text=wrong_speaker_text,
        proposals=[wrong_proposal],
        claim_records=[wrong_speaker],
    )
    assert _records(wrong, "social_occurrence_admit")[0][
        "motive_claim_ref"
    ] is None

    for mismatched_text in (
        "I kiss you. I asserted that I did not kiss you because I wanted reassurance.",
        "I kiss you. I asserted that I might kiss you because I wanted reassurance.",
        "I kiss you. I asserted that Casey kissed you because I wanted reassurance.",
        "I kiss you. I asserted that I hugged you because I wanted reassurance.",
    ):
        mismatched_claim = _motive_claim(mismatched_text)
        mismatched_proposal = _proposal(mismatched_text, "I kiss you.")
        mismatched_proposal["motive_claim_ref"] = {
            "claim_id": mismatched_claim["claim_id"],
            "fingerprint": mismatched_claim["fingerprint"],
        }
        mismatched = _seal(
            assistant_text=mismatched_text,
            proposals=[mismatched_proposal],
            claim_records=[mismatched_claim],
        )
        assert _records(mismatched, "social_occurrence_admit")[0][
            "motive_claim_ref"
        ] is None


@pytest.mark.parametrize(
    ("action_text", "matching_predicate", "mismatched_predicate"),
    [
        (
            "I have sex with you.",
            "I had sex with you because I wanted reassurance.",
            "I touched you sexually because I wanted reassurance.",
        ),
        (
            "I touch you sexually.",
            "I touched you sexually because I wanted reassurance.",
            "I had sex with you because I wanted reassurance.",
        ),
    ],
)
def test_sexual_motive_requires_the_exact_concrete_contact_act(
    action_text: str,
    matching_predicate: str,
    mismatched_predicate: str,
) -> None:
    for predicate, expected in (
        (matching_predicate, True),
        (mismatched_predicate, False),
    ):
        message = f"{action_text} I asserted that {predicate}"
        claim = _motive_claim(message)
        proposal = _proposal(
            message,
            action_text,
            action="sexual_contact",
        )
        proposal["motive_claim_ref"] = {
            "claim_id": claim["claim_id"],
            "fingerprint": claim["fingerprint"],
        }
        occurrence = _records(
            _seal(
                assistant_text=message,
                proposals=[proposal],
                claim_records=[claim],
            ),
            "social_occurrence_admit",
        )[0]
        assert bool(occurrence["motive_claim_ref"]) is expected


def test_motive_claim_survives_a_real_cross_session_accepted_prefix_fork() -> None:
    store = Store(":memory:")
    parent_session, parent_branch = store.create_session(
        external_id="motive-parent",
    )
    message = (
        "I kiss you. "
        "I asserted that I kissed you because I wanted reassurance."
    )
    canonical = CanonMsg("user", "", "1" * 16)
    store.append_msgs(
        parent_branch,
        0,
        [(canonical.role, canonical.content_hash, next(iter(chain([canonical]))))],
    )
    _accepted_turn(
        store,
        parent_branch,
        0,
        user_text="Stay close.",
        assistant_text=message,
    )
    claim = _motive_claim(
        message,
        session_id=parent_session,
        branch_id=parent_branch,
        turn=0,
    )
    store.journal(
        parent_branch,
        0,
        0,
        [{"op": "claim_record", "frame": claim["frame"], "_record": claim}],
        "extraction",
        claim_records=[claim],
        lifecycle_source="assistant_response",
        response_occurrence_id=RESPONSE,
    )

    child_session, empty_child = store.create_session(
        external_id="motive-child",
    )
    child_branch = store.fork_branch(
        parent_branch,
        at_pos=1,
        fork_turn=0,
        new_session_id=child_session,
        discard_empty_branch=empty_child,
    )
    proposal = _proposal(message, "I kiss you.")
    proposal["motive_claim_ref"] = {
        "claim_id": claim["claim_id"],
        "fingerprint": claim["fingerprint"],
    }
    inherited = _seal(
        assistant_text=message,
        proposals=[proposal],
        session_id=child_session,
        branch_id=child_branch,
        turn=0,
        continuity_state=current_state(store, child_branch),
        claim_records=store.claim_records(child_branch),
    )
    assert _records(inherited, "social_occurrence_admit")[0][
        "motive_claim_ref"
    ] == proposal["motive_claim_ref"]


def test_private_motive_reference_is_redacted_from_shared_occurrence_projection() -> None:
    message = (
        "I kiss you. "
        "I asserted that I kissed you because I wanted reassurance."
    )
    claim = _motive_claim(message, scoped_actors=[CHARACTER])
    occurrence = _occurrence("occurrence.private-motive")
    occurrence["visibility"] = "actor_scoped"
    occurrence["scoped_actors"] = [CHARACTER, PERSONA]
    occurrence["motive_claim_ref"] = {
        "claim_id": claim["claim_id"],
        "fingerprint": claim["fingerprint"],
    }
    state = {
        "claims": [claim],
        "social_occurrences": {
            occurrence["occurrence_id"]: [occurrence],
        },
    }
    character_view = chat_continuity.project_continuity(
        state,
        viewer_actor_id=CHARACTER,
        player_actor_id=PERSONA,
    )
    persona_view = chat_continuity.project_continuity(
        state,
        viewer_actor_id=PERSONA,
        player_actor_id=PERSONA,
    )
    assert character_view["social_occurrences"][
        occurrence["occurrence_id"]
    ][0]["motive_claim_ref"] is not None
    assert persona_view["social_occurrences"][
        occurrence["occurrence_id"]
    ][0]["motive_claim_ref"] is None


@pytest.mark.parametrize("action,text", [
    ("promise_fulfill", "I kept my promise."),
    ("promise_violate", "I broke my promise."),
])
def test_self_report_cannot_settle_a_promise(action: str, text: str) -> None:
    thread = {
        "schema": chat_continuity.THREAD_TRANSITION_SCHEMA,
        "thread_id": "thread:promise",
        "revision": 1,
        "action": "create",
        "kind": "promise",
        "summary": "I promise to call.",
        "participants": [
            {"kind": "actor", "actor_id": CHARACTER},
            {"kind": "actor", "actor_id": PERSONA},
        ],
        "promisor_actor_id": CHARACTER,
        "promisee_actor_id": PERSONA,
        "status": "open",
        "fingerprint": "sha256:" + "7" * 64,
    }
    partitions = _seal(
        assistant_text=text,
        proposals=[_proposal(text, text, action=action)],
        continuity_state={
            "continuity_threads": {thread["thread_id"]: [thread]},
        },
    )
    assert _records(partitions, "continuity_thread_transition") == []
    assert _records(partitions, "social_occurrence_admit") == []


def _baked_promise(text: str) -> dict:
    record = _records(
        _seal(
            assistant_text=text,
            proposals=[_proposal(
                text,
                text,
                action="promise_make",
            )],
        ),
        "continuity_thread_transition",
    )[0]
    record["lifecycle_source"] = "assistant_response"
    record["response_occurrence_id"] = RESPONSE
    return chat_continuity.bake_thread_transition(record, [])


def test_matching_admitted_action_fulfills_positive_promise_with_exact_cause() -> None:
    promise = _baked_promise(
        "I promise you that I will call you after shift.",
    )
    action = "I call you after shift."
    partitions = _seal(
        assistant_text=action,
        proposals=[_proposal(
            action,
            action,
            action="promise_fulfill",
        )],
        turn=2,
        continuity_state={
            "continuity_threads": {promise["thread_id"]: [promise]},
        },
    )
    occurrences = _records(partitions, "social_occurrence_admit")
    transitions = _records(partitions, "continuity_thread_transition")
    assert len(occurrences) == len(transitions) == 1
    assert transitions[0]["status"] == "fulfilled"
    assert transitions[0]["cause_ref"]["kind"] == "social_occurrence"
    expected = dict(occurrences[0])
    expected["lifecycle_source"] = "assistant_response"
    expected["response_occurrence_id"] = RESPONSE
    baked_occurrence = chat_continuity.bake_social_occurrence(expected, [])
    assert transitions[0]["cause_ref"]["fingerprint"] == baked_occurrence[
        "fingerprint"
    ]


def test_matching_admitted_action_violates_negative_promise_but_mismatch_abstains() -> None:
    promise = _baked_promise(
        "I promise you that I will not kiss Casey.",
    )
    state = {
        "entities": {
            OUTSIDE_ACTOR: {
                "kind": "character",
                "name": "Casey",
                "aliases": [],
            },
        },
        "continuity_threads": {promise["thread_id"]: [promise]},
    }
    action = "I kiss Casey."
    partitions = _seal(
        assistant_text=action,
        proposals=[_proposal(
            action,
            action,
            participant=OUTSIDE_ACTOR,
            action="promise_violate",
        )],
        turn=2,
        continuity_state=state,
    )
    transitions = _records(partitions, "continuity_thread_transition")
    assert len(transitions) == 1
    assert transitions[0]["status"] == "violated"
    assert transitions[0]["cause_ref"]["kind"] == "social_occurrence"

    mismatch = "I hug Casey."
    mismatched = _seal(
        assistant_text=mismatch,
        proposals=[_proposal(
            mismatch,
            mismatch,
            participant=OUTSIDE_ACTOR,
            action="promise_violate",
        )],
        turn=2,
        continuity_state=state,
    )
    assert _records(mismatched, "continuity_thread_transition") == []
    assert _records(mismatched, "social_occurrence_admit") == []


def test_thread_resolve_canonical_phrase_closes_one_exact_open_promise() -> None:
    promise = _baked_promise(
        "I promise you that I will call you after shift.",
    )
    text = "I consider my promise to you resolved."
    partitions = _seal(
        assistant_text=text,
        proposals=[_proposal(
            text,
            text,
            action="thread_resolve",
        )],
        turn=2,
        continuity_state={
            "continuity_threads": {promise["thread_id"]: [promise]},
        },
    )
    transitions = _records(partitions, "continuity_thread_transition")
    assert len(transitions) == 1
    assert transitions[0]["thread_id"] == promise["thread_id"]
    assert transitions[0]["status"] == "resolved"
    assert transitions[0]["supersedes_fingerprint"] == promise["fingerprint"]

    unauthorized = _seal(
        user_text=text,
        proposals=[_proposal(
            text,
            text,
            source="user_text",
            subject=PERSONA,
            participant=CHARACTER,
            action="thread_resolve",
        )],
        turn=2,
        continuity_state={
            "continuity_threads": {promise["thread_id"]: [promise]},
        },
    )
    assert _records(
        unauthorized,
        "continuity_thread_transition",
    ) == []


def test_mixed_failed_social_and_ordinary_memory_roll_back_as_one_job() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="whole-chat-job")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="Remember this.",
        assistant_text="I promise to remember.",
    )
    before = store.journal_high_water()
    partitions = {
        "deferred_extraction": [{
            "op": "social_occurrence_admit",
            "record": {"schema": "forged-social-record"},
            "_social_group_id": "social-group:failed",
        }, {
            "op": "memory_event",
            "text": "The rain key is under the blue cup.",
            "participants": [CHARACTER, PERSONA],
            "importance": 7,
            "tags": ["rain-key"],
        }],
    }
    result = apply_partitioned_delta(
        store,
        session_id,
        branch_id,
        1,
        partitions,
        cfg,
        expected_response_occurrence_id=RESPONSE,
    )
    assert result.atomic_group_failures
    assert result.applied == []
    assert store.journal_high_water() == before
    assert current_state(store, branch_id).get("memories", []) == []


def test_accepted_message_receipt_survives_prose_pruning() -> None:
    store = Store(":memory:")
    _session_id, branch_id = store.create_session(external_id="receipt-pruning")
    text = "Mara: I remember the lighthouse key."
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="Do you remember?",
        assistant_text=text,
    )
    expected = store.turn_text_source_fingerprint(
        branch_id,
        1,
        "assistant_response",
    )
    assert expected
    with store.transaction():
        store.db.execute(
            "UPDATE turn_texts SET assistant_text=NULL"
            " WHERE branch_id=? AND turn_index=?",
            (branch_id, 1),
        )
    assert store.turn_text_source_fingerprint(
        branch_id,
        1,
        "assistant_response",
    ) == expected


def test_true_pre_column_chat_memory_stays_legacy_and_fails_closed(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy-chat-memory.sqlite3"
    branch_id = "branch:legacy-pre-column"
    legacy = rebuild_schema_fixture(
        db_path, Path(__file__).parent / "fixtures" / "hardening" / "schema-history"
        / "1.23.0-final-34dfe8f" / "core.schema.sql"
    )
    try:
        legacy.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memory:legacy-pre-column",
                "session:legacy-pre-column",
                branch_id,
                "episodic",
                "The rain key is under the blue cup.",
                json.dumps([CHARACTER, PERSONA]),
                None,
                json.dumps(["rain-key"]),
                8,
                1,
                1,
                None,
                0,
                None,
            ),
            )
        legacy.commit()
    finally:
        legacy.close()
    migrated = Store(db_path)
    assert tuple(migrated.schema_migrations.applied()) == (
        (1, "store-core-1.24-baseline", "store-core"),
        (2, "worldlex-1.24-baseline", "worldlex"),
        (3, "turn-lifecycle-1.24-baseline", "turn-lifecycle"),
        (4, "store-chat-lineage-1.24-baseline", "store-core"),
    )
    row = migrated.memories_candidates(branch_id)[0]
    assert row["lifecycle_source"] == ""
    assert row["response_occurrence_id"] == ""
    assert row["journal_op_ref"] == ""
    assert row["source_message_fingerprint"] == ""
    assert not migrated.memory_artifact_lineage_current(branch_id, row)


def test_blank_memory_fingerprint_fails_closed_even_with_current_journal_ref() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="blank-memory-fp")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="Remember the rain key.",
        assistant_text="Mara: I remember the rain key.",
    )
    result = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{
            "op": "memory_event",
            "text": "The rain key is under the blue cup.",
            "participants": [CHARACTER, PERSONA],
            "importance": 8,
            "tags": ["rain-key"],
            "visibility": "actor_scoped",
            "scoped_actors": [CHARACTER],
        }],
        "extraction",
        cfg,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=RESPONSE,
    )
    memory.index_applied(
        store,
        session_id,
        branch_id,
        result.applied,
        result.state,
    )
    with store.transaction():
        store.db.execute(
            "UPDATE memories SET source_message_fingerprint=''"
            " WHERE branch_id=?",
            (branch_id,),
        )
    row = store.memories_candidates(branch_id)[0]
    assert row["journal_op_ref"]
    assert not store.memory_artifact_lineage_current(branch_id, row)


def test_missing_memory_receipt_fails_closed_even_while_prose_is_retained() -> None:
    cfg = Config()
    store = Store(":memory:")
    session_id, branch_id = store.create_session(
        external_id="missing-memory-receipt",
    )
    _admit_chat_identity(store, cfg, session_id, branch_id)
    _accepted_turn(
        store,
        branch_id,
        1,
        user_text="Remember the rain key.",
        assistant_text="Mara: I remember the rain key.",
    )
    result = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [{
            "op": "memory_event",
            "text": "The rain key is under the blue cup.",
            "participants": [CHARACTER, PERSONA],
            "importance": 8,
            "tags": ["rain-key"],
            "visibility": "actor_scoped",
            "scoped_actors": [CHARACTER],
        }],
        "extraction",
        cfg,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=RESPONSE,
    )
    memory.index_applied(
        store,
        session_id,
        branch_id,
        result.applied,
        result.state,
    )
    row = store.memories_candidates(branch_id)[0]
    assert store.memory_artifact_lineage_current(branch_id, row)
    expected_recall = ["- The rain key is under the blue cup. (just now)"]
    store.write_recall(
        session_id,
        2,
        expected_recall,
        branch_id=branch_id,
        source_turn=1,
        lifecycle_source="deferred_extraction",
        response_occurrence_id=RESPONSE,
        source_message_fingerprint=row["source_message_fingerprint"],
        journal_op_refs=[row["journal_op_ref"]],
    )
    assert store.read_recall(
        session_id,
        branch_id=branch_id,
        for_turn=2,
        experience_mode="chat",
    ) == expected_recall
    with store.transaction():
        store.db.execute(
            "DELETE FROM chat_accepted_message_receipts"
            " WHERE branch_id=? AND turn_index=?"
            " AND lifecycle_source='deferred_extraction'",
            (branch_id, 1),
        )
    assert store.get_turn_texts(branch_id, 1, 1)[0]["assistant_text"]
    assert not store.memory_artifact_lineage_current(branch_id, row)
    assert store.read_recall(
        session_id,
        branch_id=branch_id,
        for_turn=2,
        experience_mode="chat",
    ) == []


def test_chat_social_schema_is_exact_and_drops_unused_evidence_refs() -> None:
    from aetherstate.extraction import delta_json_schema

    proposal = delta_json_schema(chat=True)["schema"]["properties"][
        "social_occurrence_proposals"
    ]["items"]
    assert "evidence_refs" not in proposal["properties"]
    assert "evidence_refs" not in proposal["required"]
    evidence = proposal["properties"]["consent"]["items"]
    assert evidence["additionalProperties"] is False
    assert set(evidence["required"]) == {
        "participant",
        "act",
        "status",
        "channel",
        "source_span",
    }
    assert evidence["properties"]["status"]["enum"] == [
        "granted",
        "refused",
        "unknown",
    ]
    participant = proposal["properties"]["participants"]["items"]
    assert set(participant["required"]) == {"kind", "actor_id"}
    assert participant["properties"]["kind"]["enum"] == ["actor"]
    assert participant["additionalProperties"] is False
    assert proposal["properties"]["source_span"]["additionalProperties"] is False
