"""Durable Living Character relationship and social-continuity contract."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aetherstate.chat_card import (
    chat_envelope_fingerprint,
    core_fingerprint,
    world_fingerprint,
)
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


OUTSIDE_ACTOR = "actor:" + "3" * 64
CORE = {
    "schema": "aetherstate-character-core/1",
    "revision": 1,
    "name": "Mara",
    "description": "A private paramedic.",
    "personality": "Direct and observant.",
    "scenario": "Mara and the Persona share a home.",
    "first_message": "You are still awake?",
    "example_dialogue": "",
    "anchors": [],
    "boundaries": [],
}
CHARACTER = "character:" + hashlib.sha256(
    b"chat-character\0" + core_fingerprint(CORE).encode("utf-8"),
).hexdigest()
PERSONA_SOURCE = "relationship-matrix-persona.png"
PERSONA = "persona:" + hashlib.sha256(
    b"chat-persona\0" + PERSONA_SOURCE.encode("utf-8"),
).hexdigest()
ACTOR_CHARACTER = {"kind": "actor", "actor_id": CHARACTER}
ACTOR_PERSONA = {"kind": "actor", "actor_id": PERSONA}
PERSON_OUTSIDE = {
    "kind": "person",
    "person_id": "person:casey",
    "label": "Casey",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical(value)).hexdigest()


def _evidence(kind: str, text: str = "accepted evidence") -> dict:
    row = {
        "schema": "aetherstate-recognized-evidence/1",
        "kind": kind,
        "message_fingerprint": _fingerprint(b"message\0", text),
        "start": 0,
        "end": len(text),
        "accepted": True,
        "code_sealed": True,
    }
    row["fingerprint"] = _fingerprint(b"aetherstate-recognized-evidence/1\0", row)
    return row


def _agreement(
    *,
    agreement_id: str = "agreement.primary",
    revision: int = 1,
    action: str = "create",
    supersedes_fingerprint: str | None = None,
    exclusivity: str = "exclusive",
    allowed_acts: tuple[str, ...] = (),
    requires_disclosure: bool = False,
    disclosure_deadline: str | None = None,
    effective_turn: int = 1,
) -> dict:
    row = {
        "schema": "aetherstate-relationship-agreement/1",
        "agreement_id": agreement_id,
        "revision": revision,
        "action": action,
        "parties": [ACTOR_CHARACTER, ACTOR_PERSONA],
        "exclusivity": exclusivity,
        "allowed_outside_acts": list(allowed_acts),
        "requires_disclosure": requires_disclosure,
        "disclosure_deadline": disclosure_deadline,
        "effective_turn": effective_turn,
        "assent": [],
    }
    if supersedes_fingerprint is not None:
        row["supersedes_fingerprint"] = supersedes_fingerprint
    return row


def _occurrence(
    occurrence_id: str,
    *,
    outside: list[dict] | None = None,
    voluntariness: str = "voluntary",
    consent_channel: str = "in_fiction",
    disclosure: str | None = None,
    turn: int = 2,
) -> dict:
    row = {
        "schema": "aetherstate-social-occurrence/1",
        "occurrence_id": occurrence_id,
        "revision": 1,
        "action": "admit",
        "occurred_turn": turn,
        "act": "sex",
        "agreement_actor": ACTOR_CHARACTER,
        "outside_participants": outside if outside is not None else [PERSON_OUTSIDE],
        "voluntariness": [{
            "participant": ACTOR_CHARACTER,
            "status": voluntariness,
            "evidence": None if voluntariness == "unknown" else _evidence(
                "voluntariness", f"{occurrence_id}:voluntariness",
            ),
        }],
        "consent": [{
            "participant": ACTOR_CHARACTER,
            "act": "sex",
            "status": "granted",
            "channel": consent_channel,
            "evidence": _evidence("consent", f"{occurrence_id}:consent"),
        }],
        "disclosures": [],
        "motive_claim_ref": None,
        "summary": "A bounded synthetic social occurrence.",
    }
    if disclosure is not None:
        row["disclosures"].append({
            "agreement_id": "agreement.primary",
            "participant": ACTOR_CHARACTER,
            "status": disclosure,
            "evidence": None if disclosure == "unknown" else _evidence(
                "disclosure", f"{occurrence_id}:disclosure",
            ),
        })
    return row


def _flatten_agreements(state: dict) -> list[dict]:
    return [
        revision
        for revisions in state["relationship_agreements"].values()
        for revision in revisions
    ]


def _apply(
    store: Store,
    cfg: Config,
    session_id: str,
    branch_id: str,
    turn: int,
    op: dict,
    *,
    source: str = "user",
) -> dict:
    result = apply_delta(store, session_id, branch_id, turn, [op], source, cfg)
    assert not result.quarantined, result.quarantined
    assert len(result.applied) == 1
    return result.applied[0]


def _admit_chat_identity(
    store: Store,
    cfg: Config,
    session_id: str,
    branch_id: str,
) -> dict:
    """Exercise the same durable identity objects as the Task 2 admission route."""
    binding = store.experience_inference_set_unlocked(
        session_id, "chat", "card:character",
    )
    assert binding.mode == "chat"
    before = store.journal_high_water()
    admitted = _apply(
        store,
        cfg,
        session_id,
        branch_id,
        0,
        {
            "op": "chat_core_seed",
            "core": CORE,
            "core_fingerprint": core_fingerprint(CORE),
            "character_actor_id": CHARACTER,
            "persona_actor_id": PERSONA,
            "world": None,
            "world_fingerprint": world_fingerprint(None),
            "card_envelope_fingerprint": chat_envelope_fingerprint(CORE, None),
        },
        source="genesis",
    )
    journal = store.journal_window(
        branch_id,
        after_id=before,
        through_id=store.journal_high_water(),
    )
    assert len(journal) == 1
    receipt = store.persist_chat_core_receipt(
        session_id=session_id,
        branch_id=branch_id,
        journal_op_id=journal[0]["id"],
        core_fingerprint=admitted["core_fingerprint"],
        world_fingerprint=admitted["world_fingerprint"],
        card_envelope_fingerprint=admitted["card_envelope_fingerprint"],
        character_actor_id=admitted["character_actor_id"],
        persona_actor_id=admitted["persona_actor_id"],
        admitted_turn=0,
    )
    with store.transaction():
        store.db.execute(
            "UPDATE sessions SET experience_mode='chat',"
            " experience_mode_source='card:character', core_fingerprint=?,"
            " character_actor_id=?, persona_actor_id=? WHERE session_id=?",
            (core_fingerprint(CORE), CHARACTER, PERSONA, session_id),
        )
    bound = store.experience_binding(session_id)
    assert bound.mode == "chat"
    assert bound.core_fingerprint == receipt["core_fingerprint"]
    assert bound.character_actor_id == receipt["character_actor_id"]
    assert bound.persona_actor_id == receipt["persona_actor_id"]
    assert store.chat_core_receipt_for_session(session_id) == receipt
    return receipt


@pytest.mark.parametrize("op", [
    {"op": "scene_set", "location": "highmoor", "phase": "opening"},
    {"op": "time_advance", "to_time_of_day": "morning"},
])
def test_chat_continuity_inventory_preserves_zero_required_field_ops(op) -> None:
    from aetherstate.state import validate_op

    assert validate_op(op) == op


def test_continuity_requires_receipted_chat_identity_and_exact_admitted_actor_refs(
    tmp_path: Path,
) -> None:
    cfg = Config()
    cfg.specialization.name = "none"

    unbound = Store(tmp_path / "unreceipted.sqlite3")
    unbound_session, unbound_branch = unbound.create_session(external_id="unreceipted")
    _apply(
        unbound,
        cfg,
        unbound_session,
        unbound_branch,
        0,
        {
            "op": "chat_core_seed",
            "core": CORE,
            "core_fingerprint": core_fingerprint(CORE),
            "character_actor_id": CHARACTER,
            "persona_actor_id": PERSONA,
            "world": None,
            "world_fingerprint": world_fingerprint(None),
            "card_envelope_fingerprint": chat_envelope_fingerprint(CORE, None),
        },
        source="genesis",
    )
    rejected_unreceipted = apply_delta(
        unbound,
        unbound_session,
        unbound_branch,
        1,
        [{"op": "relationship_agreement_revision", "record": _agreement()}],
        "user",
        cfg,
    )
    assert not rejected_unreceipted.applied and rejected_unreceipted.quarantined
    unbound.close()

    store = Store(tmp_path / "actor-binding.sqlite3")
    session_id, branch_id = store.create_session(external_id="actor-binding")
    _admit_chat_identity(store, cfg, session_id, branch_id)
    fabricated_actor = {"kind": "actor", "actor_id": "actor:" + "9" * 64}

    forged_agreement = _agreement()
    forged_agreement["parties"][0] = fabricated_actor
    for op in (
        {"op": "relationship_agreement_revision", "record": forged_agreement},
        {
            "op": "continuity_thread_transition",
            "record": {
                "schema": "aetherstate-continuity-thread-transition/1",
                "thread_id": "thread.forged-actor",
                "revision": 1,
                "action": "create",
                "kind": "plan",
                "summary": "A fabricated actor cannot own an admitted thread.",
                "participants": [ACTOR_CHARACTER, fabricated_actor],
                "status": "open",
            },
        },
    ):
        rejected = apply_delta(
            store, session_id, branch_id, 1, [op], "user", cfg,
        )
        assert not rejected.applied and rejected.quarantined

    exact_agreement = _apply(
        store,
        cfg,
        session_id,
        branch_id,
        1,
        {"op": "relationship_agreement_revision", "record": _agreement()},
    )
    assert exact_agreement["_record"]["parties"] == [ACTOR_CHARACTER, ACTOR_PERSONA]

    forged_occurrence = _occurrence("occurrence.forged-agreement-actor")
    forged_occurrence["agreement_actor"] = fabricated_actor
    forged_occurrence["voluntariness"][0]["participant"] = fabricated_actor
    forged_occurrence["consent"][0]["participant"] = fabricated_actor
    rejected_occurrence = apply_delta(
        store,
        session_id,
        branch_id,
        2,
        [{"op": "social_occurrence_admit", "record": forged_occurrence}],
        "user",
        cfg,
    )
    assert not rejected_occurrence.applied and rejected_occurrence.quarantined

    anonymous = _apply(
        store,
        cfg,
        session_id,
        branch_id,
        2,
        {
            "op": "social_occurrence_admit",
            "record": _occurrence(
                "occurrence.forged-binding",
                outside=[{
                    "kind": "anonymous",
                    "occurrence_id": "occurrence.forged-binding",
                    "anonymous_id": "anon:one",
                    "label": "an unidentified person",
                }],
            ),
        },
    )["_record"]
    rejected_binding = apply_delta(
        store,
        session_id,
        branch_id,
        3,
        [{
            "op": "social_referent_bind",
            "record": {
                "schema": "aetherstate-social-referent-binding/1",
                "occurrence_id": anonymous["occurrence_id"],
                "anonymous_id": "anon:one",
                "actor_id": fabricated_actor["actor_id"],
                "cause_ref": {
                    "kind": "manual_identity_confirmation",
                    "fingerprint": _evidence("identity_confirmation")["fingerprint"],
                },
            },
        }],
        "user",
        cfg,
    )
    assert not rejected_binding.applied and rejected_binding.quarantined
    store.close()


SCENARIOS = [
    pytest.param("exclusive_voluntary", "violated", id="exclusive-voluntary-prohibited"),
    pytest.param("open_allowed", "not_violated", id="open-allows-act"),
    pytest.param("later_open_revision", "violated", id="later-open-does-not-rewrite"),
    pytest.param("ambiguous_active_agreements", "unresolved",
                 id="multiple-active-agreements-remain-unresolved"),
    pytest.param("ooc_content_consent", "violated", id="ooc-is-not-fiction-permission"),
    pytest.param("disclosure_matrix", ("not_violated", "violated", "unresolved", "unresolved"),
                 id="disclosure-timely-withheld-unknown"),
    pytest.param("coerced_victim", "not_violated", id="coerced-victim"),
    pytest.param("unknown_voluntariness", "unresolved", id="unknown-evidence"),
    pytest.param("anonymous_local", "violated", id="anonymous-cardless"),
    pytest.param("group_scope", "violated", id="group-cannot-consent"),
    pytest.param("collective_no_entities", ("violated", "violated", "violated"),
                 id="group-category-unknown-no-actors"),
    pytest.param("forgiveness_consequence", "violated", id="forgiveness-does-not-erase"),
    pytest.param("bad_predecessors", "violated", id="revision-gap-and-bad-predecessor"),
    pytest.param("occurrence_correction", ("violated", "not_violated"),
                 id="correction-appends-successor"),
    pytest.param("occurrence_retraction", "violated",
                 id="retraction-derives-predecessor-actor"),
    pytest.param("anonymous_binding", "violated", id="manual-anonymous-binding"),
]


@pytest.mark.parametrize(("case", "expected"), SCENARIOS)
def test_relationship_agreement_occurrence_consent_and_infidelity_matrix(
    tmp_path: Path,
    case: str,
    expected: str | tuple[str, ...],
) -> None:
    """Every case crosses Store apply, close/reopen, and replay before it passes."""
    from aetherstate import chat_continuity as continuity

    cfg = Config()
    cfg.specialization.name = "none"
    db_path = tmp_path / f"{case}.sqlite3"
    store = Store(db_path)
    receipt_columns = {
        row["name"]
        for row in store.db.execute("PRAGMA table_info(chat_continuity_seed_receipts)")
    }
    assert {
        "session_id", "record_fingerprint", "branch_id", "family", "record_json",
        "admitted_turn", "journal_op_id", "receipt_fingerprint", "committed_at",
    } <= receipt_columns
    session_id, branch_id = store.create_session(external_id=f"relationship-{case}")
    receipt = _admit_chat_identity(store, cfg, session_id, branch_id)
    assert receipt["branch_id"] == branch_id
    assert receipt["character_actor_id"] == CHARACTER
    assert receipt["persona_actor_id"] == PERSONA

    agreement = _agreement(
        exclusivity="open" if case in {"open_allowed", "disclosure_matrix"} else "exclusive",
        allowed_acts=("sex",) if case in {"open_allowed", "disclosure_matrix"} else (),
        requires_disclosure=case == "disclosure_matrix",
        disclosure_deadline="before_act" if case == "disclosure_matrix" else None,
    )
    continuity.validate_agreement_revision(agreement)
    applied_agreement = _apply(
        store,
        cfg,
        session_id,
        branch_id,
        1,
        {"op": "relationship_agreement_revision", "record": agreement},
    )["_record"]
    if case == "ambiguous_active_agreements":
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            1,
            {
                "op": "relationship_agreement_revision",
                "record": _agreement(agreement_id="agreement.secondary"),
            },
        )

    occurrence_rows: list[dict] = []
    projected_ids: list[str] = []
    if case == "disclosure_matrix":
        for index, disclosure in enumerate(("timely", None, "unknown"), start=1):
            occurrence = _occurrence(
                f"occurrence.disclosure.{index}",
                disclosure=disclosure,
                turn=index + 1,
            )
            continuity.validate_social_occurrence(occurrence)
            occurrence_rows.append(_apply(
                store,
                cfg,
                session_id,
                branch_id,
                index + 1,
                {"op": "social_occurrence_admit", "record": occurrence},
            )["_record"])
        unspecified = _agreement(
            revision=2,
            action="amend",
            supersedes_fingerprint=applied_agreement["fingerprint"],
            exclusivity="open",
            allowed_acts=("sex",),
            requires_disclosure=True,
            disclosure_deadline="unspecified",
            effective_turn=5,
        )
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            5,
            {"op": "relationship_agreement_revision", "record": unspecified},
        )
        occurrence_rows.append(_apply(
            store,
            cfg,
            session_id,
            branch_id,
            6,
            {
                "op": "social_occurrence_admit",
                "record": _occurrence("occurrence.disclosure.unspecified", turn=6),
            },
        )["_record"])
        caller_withheld = _occurrence(
            "occurrence.disclosure.forged-withheld",
            disclosure="unknown",
        )
        caller_withheld["disclosures"][0]["status"] = "withheld"
        with pytest.raises(ValueError, match="cannot propose withheld"):
            continuity.validate_social_occurrence(caller_withheld)
        for duplicate_status in ("timely", "late"):
            duplicate_disclosure = _occurrence(
                f"occurrence.disclosure.duplicate-{duplicate_status}",
                disclosure="timely",
            )
            duplicate_disclosure["disclosures"].append({
                **duplicate_disclosure["disclosures"][0],
                "status": duplicate_status,
                "evidence": _evidence(
                    "disclosure",
                    f"duplicate-{duplicate_status}:distinct-source-span",
                ),
            })
            with pytest.raises(ValueError, match="duplicate disclosure target"):
                continuity.validate_social_occurrence(duplicate_disclosure)
    elif case == "collective_no_entities":
        refs = [
            {"kind": "group", "label": "the visiting team", "count": 4},
            {"kind": "category", "label": "men"},
            {"kind": "unknown", "label": "unknown participants"},
        ]
        for index, ref in enumerate(refs, start=1):
            occurrence_rows.append(_apply(
                store,
                cfg,
                session_id,
                branch_id,
                index + 1,
                {
                    "op": "social_occurrence_admit",
                    "record": _occurrence(
                        f"occurrence.collective.{index}", outside=[ref], turn=index + 1,
                    ),
                },
            )["_record"])
    else:
        outside = None
        voluntariness = "voluntary"
        channel = "in_fiction"
        if case in {"coerced_victim", "occurrence_correction"}:
            voluntariness = "coerced" if case == "coerced_victim" else "voluntary"
        elif case == "unknown_voluntariness":
            voluntariness = "unknown"
        elif case in {"anonymous_local", "anonymous_binding"}:
            outside = [{
                "kind": "anonymous",
                "occurrence_id": f"occurrence.{case}",
                "anonymous_id": "anon:outside-1",
                "label": "an unidentified person",
            }]
        elif case == "group_scope":
            outside = [{"kind": "group", "label": "a touring band", "count": 5}]
        if case == "ooc_content_consent":
            channel = "ooc_content"
        occurrence = _occurrence(
            f"occurrence.{case}",
            outside=outside,
            voluntariness=voluntariness,
            consent_channel=channel,
        )
        continuity.validate_social_occurrence(occurrence)
        occurrence_rows.append(_apply(
            store,
            cfg,
            session_id,
            branch_id,
            2,
            {"op": "social_occurrence_admit", "record": occurrence},
        )["_record"])

    if case == "later_open_revision":
        revision = _agreement(
            revision=2,
            action="amend",
            supersedes_fingerprint=applied_agreement["fingerprint"],
            exclusivity="open",
            allowed_acts=("sex",),
            effective_turn=1,
        )
        baked_revision = _apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {"op": "relationship_agreement_revision", "record": revision},
        )
        assert baked_revision["_record"]["effective_turn"] == 3

    if case == "group_scope":
        bad_group_consent = _occurrence(
            "occurrence.bad-group-consent",
            outside=[{"kind": "group", "label": "a touring band", "count": 5}],
        )
        bad_group_consent["consent"] = [{
            "participant": {"kind": "group", "label": "a touring band", "count": 5},
            "act": "sex",
            "status": "granted",
            "channel": "in_fiction",
            "evidence": _evidence("consent"),
        }]
        with pytest.raises(ValueError, match="consent"):
            continuity.validate_social_occurrence(bad_group_consent)
        shared_span = _occurrence("occurrence.bad-shared-span")
        shared_span["voluntariness"][0]["evidence"] = _evidence(
            "voluntariness", "one generic occurrence span",
        )
        shared_span["consent"][0]["evidence"] = _evidence(
            "consent", "one generic occurrence span",
        )
        with pytest.raises(ValueError, match="distinct exact evidence spans"):
            continuity.validate_social_occurrence(shared_span)

    if case == "forgiveness_consequence":
        occurrence_fingerprint = occurrence_rows[0]["fingerprint"]
        wrong_family = apply_delta(
            store,
            session_id,
            branch_id,
            3,
            [{
                "op": "relationship_adj",
                "from_char": CHARACTER,
                "to_char": PERSONA,
                "dimension": "trust",
                "delta": 4,
                "quality": "forgiveness",
                "reason": "A typed reference cannot borrow another record family's identity.",
                "cause_ref": {
                    "kind": "claim_record",
                    "fingerprint": occurrence_fingerprint,
                },
            }],
            "rule",
            cfg,
        )
        assert not wrong_family.applied and wrong_family.quarantined
        cfg.manual_override.enabled = True
        for from_char, to_char in (
            ("Mara", "Persona"),
            ("the visiting team", PERSONA),
        ):
            unbound_endpoint = apply_delta(
                store,
                session_id,
                branch_id,
                3,
                [{
                    "op": "relationship_adj",
                    "from_char": from_char,
                    "to_char": to_char,
                    "dimension": "trust",
                    "delta": 4,
                    "quality": "forgiveness",
                    "reason": "Display and group labels are not admitted Chat actors.",
                    "cause_ref": {
                        "kind": "social_occurrence",
                        "fingerprint": occurrence_fingerprint,
                    },
                }],
                "user",
                cfg,
            )
            assert not unbound_endpoint.applied and unbound_endpoint.quarantined
        cfg.manual_override.enabled = False
        assert "the_visiting_team" not in current_state(store, branch_id)["entities"]
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {
                "op": "relationship_adj",
                "from_char": CHARACTER,
                "to_char": PERSONA,
                "dimension": "trust",
                "delta": 4,
                "quality": "forgiveness",
                "reason": "The Persona chose to forgive without denying what happened.",
                "cause_ref": {
                    "kind": "social_occurrence",
                    "fingerprint": occurrence_fingerprint,
                },
            },
            source="rule",
        )
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            4,
            {
                "op": "continuity_thread_transition",
                "record": {
                    "schema": "aetherstate-continuity-thread-transition/1",
                    "thread_id": "thread.talk-later",
                    "revision": 1,
                    "action": "create",
                    "kind": "unfinished_conversation",
                    "summary": "They agreed to revisit what rebuilding trust means.",
                    "participants": [ACTOR_CHARACTER, ACTOR_PERSONA],
                    "status": "open",
                },
            },
        )
        bad_thread = {
            "schema": "aetherstate-continuity-thread-transition/1",
            "thread_id": "thread.talk-later",
            "revision": 3,
            "action": "update",
            "supersedes_fingerprint": "sha256:" + "f" * 64,
            "kind": "unfinished_conversation",
            "summary": "A forged gap must not rewrite the open thread.",
            "participants": [ACTOR_CHARACTER, ACTOR_PERSONA],
            "status": "open",
        }
        rejected = apply_delta(
            store,
            session_id,
            branch_id,
            5,
            [{"op": "continuity_thread_transition", "record": bad_thread}],
            "user",
            cfg,
        )
        assert not rejected.applied and rejected.quarantined

    if case == "bad_predecessors":
        for revision, predecessor in (
            (3, applied_agreement["fingerprint"]),
            (2, "sha256:" + "f" * 64),
        ):
            rejected = apply_delta(
                store,
                session_id,
                branch_id,
                3,
                [{
                    "op": "relationship_agreement_revision",
                    "record": _agreement(
                        revision=revision,
                        action="amend",
                        supersedes_fingerprint=predecessor,
                        exclusivity="open",
                        allowed_acts=("sex",),
                        effective_turn=3,
                    ),
                }],
                "user",
                cfg,
            )
            assert not rejected.applied
            assert rejected.quarantined
        automatic = _agreement(
            agreement_id="agreement.automatic",
            effective_turn=10,
        )
        rejected = apply_delta(
            store,
            session_id,
            branch_id,
            4,
            [{"op": "relationship_agreement_revision", "record": automatic}],
            "extraction",
            cfg,
        )
        assert not rejected.applied and rejected.quarantined
        automatic["assent"] = [
            {
                "party": party,
                "status": "accepted",
                "evidence": _evidence("assent", f"assent-{index}"),
            }
            for index, party in enumerate((ACTOR_CHARACTER, ACTOR_PERSONA), start=1)
        ]
        accepted_assent = apply_delta(
            store,
            session_id,
            branch_id,
            4,
            [{"op": "relationship_agreement_revision", "record": automatic}],
            "extraction",
            cfg,
        )
        assert not accepted_assent.applied and accepted_assent.quarantined
        admission = {
            "schema": "aetherstate-social-occurrence-admission/1",
            "source": "accepted_response",
            "message_fingerprint": _fingerprint(b"message\0", "accepted occurrence"),
            "start": 0,
            "end": len("accepted occurrence"),
            "accepted": True,
            "code_sealed": True,
        }
        admission["fingerprint"] = _fingerprint(
            b"aetherstate-social-occurrence-admission/1\0",
            admission,
        )
        sealed_rule_occurrence = apply_delta(
            store,
            session_id,
            branch_id,
            5,
            [{
                "op": "social_occurrence_admit",
                "record": _occurrence("occurrence.forged-rule", turn=5),
                "admission_evidence": admission,
            }],
            "rule",
            cfg,
        )
        assert not sealed_rule_occurrence.applied
        assert sealed_rule_occurrence.quarantined
        for source in ("rule", "extraction"):
            for action in ("withdraw", "release", "end"):
                transition = _agreement(
                    revision=2,
                    action=action,
                    supersedes_fingerprint=applied_agreement["fingerprint"],
                    effective_turn=5,
                )
                transition["acting_party"] = ACTOR_CHARACTER
                transition["evidence"] = _evidence(
                    "agreement_transition",
                    f"{source}:{action}:caller-reproducible",
                )
                automatic_transition = apply_delta(
                    store,
                    session_id,
                    branch_id,
                    5,
                    [{
                        "op": "relationship_agreement_revision",
                        "record": transition,
                    }],
                    source,
                    cfg,
                )
                assert not automatic_transition.applied
                assert automatic_transition.quarantined

    if case == "occurrence_correction":
        original = occurrence_rows[0]
        supersession = {
            "schema": "aetherstate-social-occurrence-supersession/1",
            "occurrence_id": original["occurrence_id"],
            "revision": 2,
            "action": "correct",
            "supersedes_fingerprint": original["fingerprint"],
            "cause": _evidence("correction"),
            "replacement": _occurrence(
                original["occurrence_id"],
                voluntariness="coerced",
                turn=original["occurred_turn"],
            ),
        }
        continuity.validate_social_occurrence_supersession(supersession)
        occurrence_rows.append(_apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {"op": "social_occurrence_supersede", "record": supersession},
        )["_record"])

    if case == "occurrence_retraction":
        original = occurrence_rows[0]
        supersession = {
            "schema": "aetherstate-social-occurrence-supersession/1",
            "occurrence_id": original["occurrence_id"],
            "revision": 2,
            "action": "retract",
            "supersedes_fingerprint": original["fingerprint"],
            "cause": _evidence("correction", "privileged exact retraction"),
        }
        continuity.validate_social_occurrence_supersession(supersession)
        retracted = _apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {"op": "social_occurrence_supersede", "record": supersession},
        )["_record"]
        assert retracted["action"] == "retract"
        assert retracted["agreement_actor"] == original["agreement_actor"]

    if case == "anonymous_binding":
        original = occurrence_rows[0]
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {
                "op": "entity_add",
                "entity": OUTSIDE_ACTOR,
                "name": "Casey",
                "kind": "character",
                "present": False,
            },
        )
        binding = {
            "schema": "aetherstate-social-referent-binding/1",
            "occurrence_id": original["occurrence_id"],
            "anonymous_id": "anon:outside-1",
            "actor_id": OUTSIDE_ACTOR,
            "cause_ref": {
                "kind": "manual_identity_confirmation",
                "fingerprint": _evidence("identity_confirmation")["fingerprint"],
            },
        }
        continuity.validate_social_referent_binding(binding)
        _apply(
            store,
            cfg,
            session_id,
            branch_id,
            3,
            {"op": "social_referent_bind", "record": binding},
        )
        duplicate = apply_delta(
            store,
            session_id,
            branch_id,
            3,
            [{"op": "social_referent_bind", "record": binding}],
            "user",
            cfg,
        )
        assert not duplicate.applied and len(duplicate.duplicates) == 1
        conflicting = {
            **binding,
            "actor_id": "actor:" + "4" * 64,
        }
        rejected = apply_delta(
            store,
            session_id,
            branch_id,
            3,
            [{"op": "social_referent_bind", "record": conflicting}],
            "user",
            cfg,
        )
        assert not rejected.applied and rejected.quarantined
        projected_ids.append(OUTSIDE_ACTOR)

    live_state = current_state(store, branch_id)
    live_assessments = [
        continuity.assess_infidelity(_flatten_agreements(live_state), occurrence)
        for occurrence in occurrence_rows
    ]
    live_projection = [
        continuity.project_social_occurrence(live_state, occurrence)
        for occurrence in occurrence_rows
    ]

    if isinstance(expected, tuple):
        assert tuple(row["status"] for row in live_assessments) == expected
    else:
        assert live_assessments[0]["status"] == expected
    for assessment, occurrence in zip(live_assessments, occurrence_rows, strict=True):
        assert assessment["occurrence_ref"] == {
            "occurrence_id": occurrence["occurrence_id"],
            "revision": occurrence["revision"],
            "fingerprint": occurrence["fingerprint"],
        }

    if case == "later_open_revision":
        assert live_assessments[0]["agreement_ref"]["revision"] == 1
    if case == "ambiguous_active_agreements":
        assert live_assessments[0]["agreement_ref"] is None
        assert live_assessments[0]["candidate_agreement_refs"] == [
            {
                "agreement_id": "agreement.primary",
                "revision": 1,
                "fingerprint": live_state["relationship_agreements"][
                    "agreement.primary"
                ][0]["fingerprint"],
            },
            {
                "agreement_id": "agreement.secondary",
                "revision": 1,
                "fingerprint": live_state["relationship_agreements"][
                    "agreement.secondary"
                ][0]["fingerprint"],
            },
        ]
    if case == "forgiveness_consequence":
        causes = live_state["relationships"][f"{CHARACTER}->{PERSONA}"]["causes"]
        assert causes[-1]["quality"] == "forgiveness"
        assert causes[-1]["cause_ref"]["fingerprint"] == occurrence_rows[0]["fingerprint"]
        assert live_state["continuity_threads"]["thread.talk-later"][0]["status"] == "open"
    if case == "occurrence_correction":
        stored = live_state["social_occurrences"][occurrence_rows[0]["occurrence_id"]]
        assert len(stored) == 2
        assert stored[0]["fingerprint"] == occurrence_rows[0]["fingerprint"]
        assert stored[1]["supersedes_fingerprint"] == stored[0]["fingerprint"]
    if case == "occurrence_retraction":
        stored = live_state["social_occurrences"][occurrence_rows[0]["occurrence_id"]]
        assert len(stored) == 2
        assert stored[1]["action"] == "retract"
        assert stored[1]["agreement_actor"] == ACTOR_CHARACTER
        assert stored[1]["supersedes_fingerprint"] == stored[0]["fingerprint"]
    if case == "anonymous_binding":
        original = live_state["social_occurrences"][occurrence_rows[0]["occurrence_id"]][0]
        assert original["outside_participants"][0]["kind"] == "anonymous"
        assert live_projection[0]["outside_participants"][0]["actor_id"] == OUTSIDE_ACTOR
    if case in {"anonymous_local", "collective_no_entities"}:
        assert set(live_state["entities"]) == {CHARACTER, PERSONA}
        assert not live_state["beliefs"]
    if case == "anonymous_binding":
        assert set(live_state["entities"]) == {CHARACTER, PERSONA, OUTSIDE_ACTOR}
        assert live_state["entities"][OUTSIDE_ACTOR]["name"] == "Casey"
    if case == "collective_no_entities":
        assert all(row["motive_claim_ref"] is None for row in occurrence_rows)
        invalid_consent = apply_delta(
            store,
            session_id,
            branch_id,
            8,
            [{
                "op": "consent_set",
                "subject": "category:men",
                "partner": CHARACTER,
                "category": "kissing",
                "level": "granted",
            }],
            "user",
            cfg,
        )
        assert not invalid_consent.applied and invalid_consent.quarantined
        display_label_consent = apply_delta(
            store,
            session_id,
            branch_id,
            8,
            [{
                "op": "consent_set",
                "subject": "the visiting team",
                "partner": CHARACTER,
                "category": "kissing",
                "level": "granted",
            }],
            "user",
            cfg,
        )
        assert not display_label_consent.applied and display_label_consent.quarantined
        fabricated_actor_consent = apply_delta(
            store,
            session_id,
            branch_id,
            8,
            [{
                "op": "consent_set",
                "subject": "actor:" + "5" * 64,
                "partner": CHARACTER,
                "category": "kissing",
                "level": "granted",
            }],
            "user",
            cfg,
        )
        assert not fabricated_actor_consent.applied
        assert fabricated_actor_consent.quarantined
        assert set(current_state(store, branch_id)["entities"]) == {CHARACTER, PERSONA}

    from aetherstate.state import authority_violation

    assert authority_violation(
        {"op": "semantic_meaning_commit"}, "rule", live_state, cfg,
    ) is None
    assert authority_violation(
        {"op": "claim_record"}, "rule", live_state, cfg,
    ) is None
    assert "RPG-only" in authority_violation(
        {"op": "semantic_binding_commit"}, "rule", live_state, cfg,
    )
    rpg_cfg = Config()
    rpg_cfg.specialization.name = "rpg"
    assert "Chat-only" in authority_violation(
        {"op": "continuity_thread_transition", "record": {}},
        "user",
        live_state,
        rpg_cfg,
    )

    store.close()
    reopened = Store(db_path)
    replayed_state = current_state(reopened, branch_id)
    replayed_assessments = [
        continuity.assess_infidelity(_flatten_agreements(replayed_state), occurrence)
        for occurrence in occurrence_rows
    ]
    replayed_projection = [
        continuity.project_social_occurrence(replayed_state, occurrence)
        for occurrence in occurrence_rows
    ]
    assert replayed_state == live_state
    assert replayed_assessments == live_assessments
    assert replayed_projection == live_projection
    reopened.close()
