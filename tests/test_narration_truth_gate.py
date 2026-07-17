from __future__ import annotations

from copy import deepcopy

import pytest

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.narration_truth_gate import (
    FALLOUT_FACT_SCHEMA,
    OPPOSITION_FACT_SCHEMA,
    TARGET_OUTCOME_SCHEMA,
    NarrationTruthGateError,
    assess_narration,
    build_narration_truth_contract,
    build_offline_expected_claim_graph,
    build_producer_claim_graph,
    build_verifier_claim_graph,
)
from aetherstate.narrator_realization import build_narrator_realization


def _fp(value: object) -> str:
    return content_fingerprint(value)


def _meaning(
    *,
    action_class: str,
    capability_id: str,
    target_id: str | None,
    seed: str,
) -> dict:
    return {
        "meaning_ref": _fp({"meaning": seed}),
        "actor_id": "player.arinvale",
        "capability_id": capability_id,
        "invoked_capability_ids": [],
        "action_class": action_class,
        "target_entity_id": target_id,
        "object_relation": {
            "object_kind_id": None,
            "linguistic_possessor_id": None,
            "resolved_instance_ids": [],
            "proven_owner_id": None,
            "part_id": None,
            "alignment_status": "none",
            "alignment_ref": None,
            "candidate_instance_ids": [],
        },
        "target_locus": None,
        "target_locus_owner_id": None,
        "assertion_status": "asserted",
        "embedding_kind": "none",
        "holder_role": "none",
        "holder_entity_id": None,
        "holder_candidates": [],
        "polarity": "positive",
        "modality": "actual",
        "time_scope": "current",
        "ambiguity_candidate_ids": [],
        "performance_mode": "may_perform",
    }


def _skill_row(target_id: str | None = "iven") -> dict:
    return {
        "event_ref": "settlement.skill",
        "adapter_id": "narrator.skill-check/1",
        "frame_ref": _fp({"frame": "skill"}),
        "event_meaning": _meaning(
            action_class="skill_check",
            capability_id="elementalism",
            target_id=target_id,
            seed="skill",
        ),
        "outcome_quality": "success",
        "impact_kind": "none",
        "impact_magnitude": "none",
        "target_state": "not_applicable",
        "settled_change_kinds": ["mastery"],
    }


def _weapon_row(*, target_id: str = "iven", defeated: bool = False) -> dict:
    return {
        "event_ref": "settlement.weapon",
        "adapter_id": "narrator.weapon-attack/1",
        "frame_ref": _fp({"frame": "weapon"}),
        "event_meaning": _meaning(
            action_class="weapon_attack",
            capability_id="weapon_attack",
            target_id=target_id,
            seed="weapon",
        ),
        "outcome_quality": "success",
        "impact_kind": "harm",
        "impact_magnitude": "decisive" if defeated else "solid",
        "target_state": "defeated" if defeated else "active",
        "settled_change_kinds": ["hp"],
    }


def _packet(
    *rows: dict,
    delivery_mode: str = "first_delivery",
    turn: int = 4,
) -> dict:
    return build_narrator_realization(
        turn,
        delivery_mode=delivery_mode,
        asserted_settled=rows,
    )


def _known(*extra: tuple[str, str, str]) -> list[dict]:
    rows = [
        {"entity_id": "player.arinvale", "label": "Arinvale", "scope": "current"},
        {"entity_id": "iven", "label": "Iven", "scope": "current"},
    ]
    rows.extend(
        {"entity_id": entity_id, "label": label, "scope": scope}
        for entity_id, label, scope in extra
    )
    return rows


def _lifecycle(packet: dict, seed: str = "main") -> dict:
    return {
        "branch_ref": f"branch.{seed}",
        "ledger_fingerprint": _fp({"ledger": seed}),
        "artifact_fingerprint": packet["fingerprint"],
    }


def _contract(
    packet: dict,
    text: str | None,
    *,
    known: list[dict] | None = None,
    opposition: list[dict] | None = None,
    fallout: list[dict] | None = None,
    outcomes: list[dict] | None = None,
    lifecycle: bool = True,
) -> dict:
    return build_narration_truth_contract(
        packet,
        known_entities=known if known is not None else _known(),
        opposition_facts=opposition or [],
        fallout_facts=fallout or [],
        settled_target_outcomes=outcomes or [],
        lifecycle_binding=_lifecycle(packet) if lifecycle else None,
        expected_realization_text=text,
    )


def _byte_span(text: str, phrase: str) -> tuple[int, int]:
    character_start = text.index(phrase)
    character_end = character_start + len(phrase)
    return (
        len(text[:character_start].encode("utf-8")),
        len(text[:character_end].encode("utf-8")),
    )


def _claim_from_expected(
    text: str,
    expected: dict,
    *,
    phrase: str | None = None,
    clause_index: int = 0,
    construction_seed: str = "candidate",
) -> dict:
    start, end = _byte_span(text, phrase or text)
    return {
        "span_start": start,
        "span_end": end,
        "clause_index": clause_index,
        "occurrence_ref": expected["occurrence_ref"],
        "cause_ref": expected["cause_ref"],
        "authority_ref": expected["construction_ref"],
        "construction_ref": _fp(
            {
                "construction": construction_seed,
                "claim": expected["claim_ref"],
                "span": [start, end],
            }
        ),
        "actor_id": expected["actor_id"],
        "subject_ids": expected["subject_ids"],
        "kind": expected["kind"],
        "polarity": expected["polarity"],
        "actuality": expected["actuality"],
        "time_scope": expected["time_scope"],
        "multiplicity": expected["multiplicity"],
        "detail": expected["detail"],
        "amount": expected["amount"],
    }


def _graphs(
    text: str,
    claims: list[dict],
    *,
    offline: bool = False,
) -> tuple[dict, dict, dict | None]:
    producer_claims = deepcopy(claims)
    verifier_claims = deepcopy(claims)
    offline_claims = deepcopy(claims)
    for index, claim in enumerate(producer_claims):
        claim["construction_ref"] = _fp({"producer": index, "base": claim["construction_ref"]})
    for index, claim in enumerate(verifier_claims):
        claim["construction_ref"] = _fp({"verifier": index, "base": claim["construction_ref"]})
    for index, claim in enumerate(offline_claims):
        claim["construction_ref"] = _fp({"offline": index, "base": claim["construction_ref"]})
    producer = build_producer_claim_graph(text, producer_claims)
    verifier = build_verifier_claim_graph(
        text,
        verifier_claims,
        issuer="independent.test-verifier/1",
    )
    expected = build_offline_expected_claim_graph(text, offline_claims) if offline else None
    return producer, verifier, expected


def _manual_claim(
    text: str,
    phrase: str,
    *,
    kind: str,
    subjects: list[str],
    polarity: str = "positive",
    actuality: str = "actual",
    time_scope: str = "current",
    actor_id: str | None = None,
    clause_index: int = 0,
    occurrence_ref: str | None = None,
    cause_ref: str | None = None,
    authority_ref: str | None = None,
    multiplicity: int | None = None,
    detail: str | None = None,
    amount: int | None = None,
) -> dict:
    start, end = _byte_span(text, phrase)
    return {
        "span_start": start,
        "span_end": end,
        "clause_index": clause_index,
        "occurrence_ref": occurrence_ref,
        "cause_ref": cause_ref,
        "authority_ref": authority_ref,
        "construction_ref": _fp(
            {"manual": phrase, "kind": kind, "subjects": subjects}
        ),
        "actor_id": actor_id,
        "subject_ids": sorted(subjects),
        "kind": kind,
        "polarity": polarity,
        "actuality": actuality,
        "time_scope": time_scope,
        "multiplicity": multiplicity or len(subjects),
        "detail": detail or kind,
        "amount": amount,
    }


def _assess_with_claims(
    text: str,
    contract: dict,
    claims: list[dict],
    *,
    offline: bool = False,
):
    producer, verifier, expected = _graphs(text, claims, offline=offline)
    return assess_narration(
        text,
        contract,
        producer_graph=producer,
        verifier_graph=verifier,
        offline_expected_graph=expected,
        offline_test_mode=offline,
    )


def test_settled_single_target_exact_graph_passes_and_receipt_binds_visible_surface():
    text = "Arinvale's settled strike harms Iven."
    packet = _packet(_weapon_row())
    contract = _contract(packet, text)
    claim = _claim_from_expected(text, contract["expected_claims"][0])

    decision = _assess_with_claims(text, contract, [claim], offline=True)

    assert decision.receipt["verdict"] == "allow"
    assert decision.visible_text == text
    assert decision.receipt["violation_codes"] == []
    assert decision.receipt["branch_fingerprint"].startswith("sha256:")
    assert decision.receipt["ledger_fingerprint"] == _lifecycle(packet)["ledger_fingerprint"]
    assert decision.receipt["candidate_content_type"] == "text/plain; charset=utf-8"
    assert decision.receipt["renderer_version"] == "aetherstate.safe-plain-text/1"
    assert decision.receipt["visible_graph_fingerprint"] == decision.visible_claim_graph["fingerprint"]


def test_arinvale_no_hp_skill_check_quarantines_invented_damage():
    text = "Arinvale's spell burns Iven."
    packet = _packet(_skill_row())
    contract = _contract(packet, text)
    invented = _manual_claim(
        text,
        text,
        kind="harm",
        subjects=["iven"],
        actor_id="player.arinvale",
        occurrence_ref="candidate.unsettled-harm",
        cause_ref="candidate.unsettled-harm",
        authority_ref=_fp({"forged": "hp"}),
        detail="harm",
    )

    decision = _assess_with_claims(text, contract, [invented], offline=True)

    assert decision.receipt["verdict"] == "fallback"
    assert "unsupported_named_target_harm" in decision.receipt["violation_codes"]
    assert decision.visible_text != text
    assert decision.visible_claim_graph["role"] == "fallback"


def test_x6_mass_casualty_claim_fails_even_with_matching_empty_graphs():
    text = "All six Holloweavings burn and die."
    packet = _packet(_skill_row())
    contract = _contract(
        packet,
        text,
        known=_known(("baser_hollow_x6", "Baser Hollow x6", "current")),
    )

    decision = _assess_with_claims(text, contract, [], offline=True)

    assert decision.receipt["verdict"] == "fallback"
    assert "mass_or_aggregate_casualty" in decision.receipt["violation_codes"]
    assert "claim_extraction_uncertain" in decision.receipt["violation_codes"]


def test_required_enemy_action_is_exactly_consumed_once():
    text = "Baser Hollow x6's Void Rake hits Arinvale for 2 HP of harm."
    packet = _packet(_skill_row(target_id=None))
    opposition = [
        {
            "schema": OPPOSITION_FACT_SCHEMA,
            "occurrence_ref": "opposition.turn-4",
            "intent_ref": "intent.turn-3",
            "construction_ref": _fp({"opposition": "turn-4"}),
            "actor_id": "baser_hollow_x6",
            "actor_label": "Baser Hollow x6",
            "target_id": "player.arinvale",
            "target_label": "Arinvale",
            "move_id": "void_rake",
            "move_label": "Void Rake",
            "outcome": "hit",
            "effects": [{"kind": "harm", "detail": "hp", "amount": -2}],
        }
    ]
    contract = _contract(
        packet,
        text,
        known=_known(("baser_hollow_x6", "Baser Hollow x6", "current")),
        opposition=opposition,
    )
    claims = [
        _claim_from_expected(text, expected)
        for expected in contract["expected_claims"]
    ]

    decision = _assess_with_claims(text, contract, claims, offline=True)

    assert decision.receipt["verdict"] == "allow"
    assert decision.receipt["expected_claim_count"] == 2

    repeated = text + " Baser Hollow x6's Void Rake hits Arinvale again."
    repeated_contract = _contract(
        packet,
        repeated,
        known=_known(("baser_hollow_x6", "Baser Hollow x6", "current")),
        opposition=opposition,
    )
    repeated_claims = [
        _claim_from_expected(repeated, expected, phrase=text)
        for expected in repeated_contract["expected_claims"]
    ]
    repeated_decision = _assess_with_claims(
        repeated, repeated_contract, repeated_claims, offline=True
    )
    assert repeated_decision.receipt["verdict"] == "fallback"
    assert "autonomous_intent_repeated" in repeated_decision.receipt["violation_codes"]


def test_missing_or_misattributed_enemy_action_falls_back():
    text = "Arinvale's blade hits Baser Hollow x6."
    packet = _packet()
    opposition = [
        {
            "schema": OPPOSITION_FACT_SCHEMA,
            "occurrence_ref": "opposition.turn-4",
            "intent_ref": "intent.turn-3",
            "construction_ref": _fp({"opposition": "turn-4"}),
            "actor_id": "baser_hollow_x6",
            "actor_label": "Baser Hollow x6",
            "target_id": "player.arinvale",
            "target_label": "Arinvale",
            "move_id": "void_rake",
            "move_label": "Void Rake",
            "outcome": "hit",
            "effects": [{"kind": "harm", "detail": "hp", "amount": -2}],
        }
    ]
    contract = _contract(
        packet,
        text,
        known=_known(("baser_hollow_x6", "Baser Hollow x6", "current")),
        opposition=opposition,
    )
    producer, verifier, offline = _graphs(text, [], offline=True)

    decision = assess_narration(
        text,
        contract,
        producer_graph=producer,
        verifier_graph=verifier,
        offline_expected_graph=offline,
        offline_test_mode=True,
    )

    assert decision.receipt["verdict"] == "fallback"
    assert "opposition_action_omitted" in decision.receipt["violation_codes"]
    assert "expected_claim_omitted" in decision.receipt["violation_codes"]


def test_negative_clause_does_not_lend_burn_to_the_next_target():
    text = "I don't burn Ana; I praise Bo."
    packet = _packet()
    contract = _contract(
        packet,
        text,
        known=_known(
            ("ana", "Ana", "current"),
            ("bo", "Bo", "current"),
        ),
    )
    negative = _manual_claim(
        text,
        "I don't burn Ana;",
        kind="harm",
        subjects=["ana"],
        polarity="negative",
        clause_index=0,
        detail="harm",
    )

    decision = _assess_with_claims(text, contract, [negative], offline=True)

    assert decision.receipt["verdict"] == "allow"
    assert decision.receipt["candidate_claim_count"] == 1


def test_empty_truth_graph_still_quarantines_mechanics_and_runtime_requires_plan():
    clean = "Light bleeds across the clouds. “I could kill a god,” Ana jokes. Bo laughs."
    packet = _packet()
    planned_contract = _contract(
        packet,
        clean,
        known=_known(("ana", "Ana", "current"), ("bo", "Bo", "current")),
    )
    clean_decision = _assess_with_claims(clean, planned_contract, [], offline=True)
    assert clean_decision.receipt["verdict"] == "allow"

    unplanned_contract = _contract(
        packet,
        None,
        known=_known(("ana", "Ana", "current"), ("bo", "Bo", "current")),
    )
    producer, verifier, _offline = _graphs(clean, [])
    runtime = assess_narration(
        clean,
        unplanned_contract,
        producer_graph=producer,
        verifier_graph=verifier,
    )
    assert runtime.receipt["verdict"] == "fallback"
    assert "missing_code_authored_realization_plan" in runtime.receipt["violation_codes"]

    mechanical = "Ana dies."
    mechanical_contract = _contract(
        packet,
        mechanical,
        known=_known(("ana", "Ana", "current")),
    )
    claim = _manual_claim(
        mechanical,
        mechanical,
        kind="defeat",
        subjects=["ana"],
        occurrence_ref="candidate.unsupported",
        cause_ref="candidate.unsupported",
        authority_ref=_fp({"forged": "defeat"}),
        detail="defeated",
    )
    blocked = _assess_with_claims(mechanical, mechanical_contract, [claim], offline=True)
    assert "unsupported_named_target_defeat" in blocked.receipt["violation_codes"]


@pytest.mark.parametrize(
    ("text", "kind", "subjects", "detail", "amount", "code"),
    [
        (
            "Ana is stunned.",
            "status",
            ["ana"],
            "stunned",
            None,
            "unsupported_named_target_status",
        ),
        (
            "Ana loses mana.",
            "resource",
            ["ana"],
            "mana",
            -1,
            "unsupported_named_target_resource",
        ),
        (
            "An hour passes.",
            "time",
            ["world"],
            "world clock",
            1,
            "unsupported_time_change",
        ),
        (
            "Ana is teleported.",
            "movement",
            ["ana"],
            "elsewhere",
            None,
            "unsupported_named_target_movement",
        ),
        (
            "The city collapses.",
            "world",
            ["world"],
            "city collapses",
            None,
            "unsupported_world_change",
        ),
    ],
)
def test_empty_truth_graph_rejects_each_unsupported_mechanic_kind(
    text: str,
    kind: str,
    subjects: list[str],
    detail: str,
    amount: int | None,
    code: str,
):
    packet = _packet()
    contract = _contract(
        packet,
        text,
        known=_known(("ana", "Ana", "current")),
    )
    claim = _manual_claim(
        text,
        text,
        kind=kind,
        subjects=subjects,
        occurrence_ref=f"candidate.{kind}",
        cause_ref=f"candidate.{kind}",
        authority_ref=_fp({"forged": kind}),
        detail=detail,
        amount=amount,
    )
    decision = _assess_with_claims(text, contract, [claim], offline=True)
    assert decision.receipt["verdict"] == "fallback"
    assert code in decision.receipt["violation_codes"]


def test_mechanic_tag_is_never_visible_even_when_claim_graphs_are_empty():
    text = "The light flickers. [hp target=iven delta=-2]"
    packet = _packet()
    with pytest.raises(NarrationTruthGateError, match="unsafe"):
        _contract(packet, text)

    contract = _contract(packet, "The light flickers.")
    decision = _assess_with_claims(text, contract, [], offline=True)
    assert decision.receipt["verdict"] == "fallback"
    assert "mechanic_tag" in decision.receipt["violation_codes"]


def test_exact_compound_multiplicity_passes_but_queued_fourth_target_fails():
    packet = _packet(_weapon_row())
    exact_text = "Arinvale's bound volley harms Iven, Mara, and Nera."
    exact_outcomes = [
        {
            "schema": TARGET_OUTCOME_SCHEMA,
            "outcome_ref": f"outcome.{target}",
            "source_event_ref": "settlement.weapon",
            "construction_ref": _fp({"target": target}),
            "target_id": target,
            "target_label": label,
            "effects": [{"kind": "harm", "detail": "hp", "amount": -1}],
        }
        for target, label in (("iven", "Iven"), ("mara", "Mara"), ("nera", "Nera"))
    ]
    known = _known(
        ("mara", "Mara", "current"),
        ("nera", "Nera", "current"),
        ("queued_hollow", "Queued Hollow", "queued"),
    )
    contract = _contract(packet, exact_text, known=known, outcomes=exact_outcomes)
    claims = [
        _claim_from_expected(exact_text, expected)
        for expected in contract["expected_claims"]
    ]
    exact = _assess_with_claims(exact_text, contract, claims, offline=True)
    assert exact.receipt["verdict"] == "allow"
    assert {claim["multiplicity"] for claim in contract["expected_claims"]} == {3}

    bad_text = (
        "Arinvale's bound volley harms Iven, Mara, Nera, and Queued Hollow."
    )
    bad_contract = _contract(packet, bad_text, known=known, outcomes=exact_outcomes)
    bad_claims = [
        _claim_from_expected(bad_text, expected)
        for expected in bad_contract["expected_claims"]
    ]
    bad_claims.append(
        _manual_claim(
            bad_text,
            bad_text,
            kind="harm",
            subjects=["queued_hollow"],
            actor_id="player.arinvale",
            occurrence_ref="settlement.weapon",
            cause_ref="settlement.weapon",
            authority_ref=_fp({"queued": "no-authority"}),
            multiplicity=4,
            detail="hp",
            amount=-1,
        )
    )
    bad = _assess_with_claims(bad_text, bad_contract, bad_claims, offline=True)
    assert bad.receipt["verdict"] == "fallback"
    assert "unsupported_named_target_harm" in bad.receipt["violation_codes"]


def test_producer_verifier_and_offline_graphs_must_agree_exactly():
    text = "Arinvale's settled strike harms Iven."
    packet = _packet(_weapon_row())
    contract = _contract(packet, text)
    correct = _claim_from_expected(text, contract["expected_claims"][0])
    producer, verifier, offline = _graphs(text, [correct], offline=True)
    forged = deepcopy(verifier)
    forged["claims"] = []
    payload = {key: forged[key] for key in forged if key != "fingerprint"}
    forged["fingerprint"] = content_fingerprint(payload)

    decision = assess_narration(
        text,
        contract,
        producer_graph=producer,
        verifier_graph=forged,
        offline_expected_graph=offline,
        offline_test_mode=True,
    )

    assert decision.receipt["verdict"] == "fallback"
    assert "producer_verifier_graph_disagreement" in decision.receipt["violation_codes"]
    assert "offline_graph_disagreement" in decision.receipt["violation_codes"]
    assert "expected_claim_omitted" in decision.receipt["violation_codes"]


@pytest.mark.parametrize(
    ("unsafe_text", "code"),
    [
        ("<span hidden>Arinvale harms Iven.</span>", "unsafe_visible_surface"),
        ("~~Arinvale harms Iven.~~", "unsafe_visible_surface"),
        ("Arinvale harms Iven.\u202e", "unsafe_visible_surface"),
        ("E\u0301ranmor harms Iven.", "normalization_drift"),
    ],
)
def test_visible_surface_rejects_semantics_changing_or_noncanonical_bytes(
    unsafe_text: str, code: str
):
    safe_plan = "Arinvale harms Iven."
    packet = _packet(_weapon_row())
    contract = _contract(packet, safe_plan)
    producer, verifier, offline = _graphs(unsafe_text, [], offline=True)

    decision = assess_narration(
        unsafe_text,
        contract,
        producer_graph=producer,
        verifier_graph=verifier,
        offline_expected_graph=offline,
        offline_test_mode=True,
    )

    assert decision.receipt["verdict"] == "fallback"
    assert code in decision.receipt["violation_codes"]
    assert "candidate_realization_plan_mismatch" in decision.receipt["violation_codes"]


def test_retry_equivalent_failures_have_identical_code_fallback_and_hash():
    text = "Arinvale's spell kills Iven."
    first_packet = _packet(_skill_row(), delivery_mode="first_delivery")
    retry_packet = _packet(_skill_row(), delivery_mode="regeneration_retry")
    first_contract = _contract(first_packet, text)
    retry_contract = _contract(retry_packet, text)
    claim = _manual_claim(
        text,
        text,
        kind="defeat",
        subjects=["iven"],
        actor_id="player.arinvale",
        occurrence_ref="candidate.unsupported",
        cause_ref="candidate.unsupported",
        authority_ref=_fp({"forged": "defeat"}),
        detail="defeated",
    )

    first = _assess_with_claims(text, first_contract, [claim], offline=True)
    retry = _assess_with_claims(text, retry_contract, [claim], offline=True)

    assert first.visible_text == retry.visible_text
    assert first.receipt["fallback_fingerprint"] == retry.receipt["fallback_fingerprint"]
    assert (
        first.receipt["fallback_graph_fingerprint"]
        == retry.receipt["fallback_graph_fingerprint"]
    )
    assert first.receipt["attempt_fingerprint"] != retry.receipt["attempt_fingerprint"]


def test_malformed_or_missing_inputs_fail_closed_with_content_free_receipt():
    decision = assess_narration("Ana dies.", {"bad": True})

    assert decision.receipt["verdict"] == "fallback"
    assert "malformed_contract" in decision.receipt["violation_codes"]
    assert "missing_producer_graph" in decision.receipt["violation_codes"]
    assert "missing_verifier_graph" in decision.receipt["violation_codes"]
    assert decision.visible_claim_graph["role"] == "fallback"
    assert "Ana dies" not in repr(decision.receipt)
    assert decision.receipt["fingerprint"].startswith("sha256:")


def test_no_hp_target_outcome_authority_cannot_be_forged_into_contract():
    packet = _packet(_skill_row())
    outcome = {
        "schema": TARGET_OUTCOME_SCHEMA,
        "outcome_ref": "outcome.iven",
        "source_event_ref": "settlement.skill",
        "construction_ref": _fp({"forged": "outcome"}),
        "target_id": "iven",
        "target_label": "Iven",
        "effects": [{"kind": "harm", "detail": "hp", "amount": -2}],
    }
    with pytest.raises(NarrationTruthGateError, match="no-impact skill check"):
        _contract(packet, "The light reaches Iven.", outcomes=[outcome])


def test_exact_fallout_cause_is_in_ledger_graph_and_fallback_graph():
    text = "Iven is defeated."
    packet = _packet()
    fallout = [
        {
            "schema": FALLOUT_FACT_SCHEMA,
            "fact_ref": "fallout.iven-defeat",
            "cause_ref": "opposition.turn-4",
            "construction_ref": _fp({"fallout": "iven"}),
            "subject_id": "iven",
            "subject_label": "Iven",
            "effects": [{"kind": "defeat", "detail": "defeated", "amount": None}],
        }
    ]
    contract = _contract(packet, text, fallout=fallout)
    expected = contract["expected_claims"][0]
    assert expected["cause_ref"] == "opposition.turn-4"
    claim = _claim_from_expected(text, expected)
    allowed = _assess_with_claims(text, contract, [claim], offline=True)
    assert allowed.receipt["verdict"] == "allow"

    producer, verifier, offline = _graphs(text, [], offline=True)
    blocked = assess_narration(
        text,
        contract,
        producer_graph=producer,
        verifier_graph=verifier,
        offline_expected_graph=offline,
        offline_test_mode=True,
    )
    assert blocked.receipt["verdict"] == "fallback"
    assert blocked.visible_claim_graph["claims"][0]["cause_ref"] == "opposition.turn-4"
