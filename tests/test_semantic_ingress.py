"""Out-of-band semantic ingress authority: syntax is never permission by itself."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from aetherstate import tier0
from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.semantic_ingress import (
    CAPABILITY_ADMISSION_DECLARATION_ID,
    CAPABILITY_AUTHORITY_GRAMMAR_ID,
    CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
    COHORT_DECLARATION_ID,
    COHORT_GRAMMAR_ID,
    COHORT_GRAMMAR_VERSION,
    OP_BATTLE_COHORT_DECLARATION,
    OP_CAPABILITY_ADMISSION,
    CapabilityIngressAuthority,
    IngressScope,
    ResourceAuthorityRef,
    SemanticIngressAuthority,
    SemanticIngressContext,
    SemanticIngressError,
    issue_semantic_ingress_authority,
    parse_authorized_cohort_declaration,
    rehydrate_semantic_ingress_authority,
    trusted_replay_rehydrator,
    validate_semantic_ingress_authority,
)
from aetherstate.state import (
    apply_delta,
    battle_ops,
    combat_ops,
    current_state,
    empty_state,
    reduce_state,
)
from aetherstate.store import Store


D = b"[battle | Caravan Ambush | Baser Hollow swarm]\n" b"[foe | Baser Hollow x6 | minion | claws]"

ISSUERS = (
    ("control", "control.console"),
    ("creator", "creator.session"),
    ("genesis", "genesis.batch"),
    ("player", "player.bean"),
    ("narrator", "narrator.main"),
    ("extractor", "extractor.cold"),
    ("rule", "rule.reducer"),
    ("display", "display.surface"),
)
CHANNELS = (
    "trusted_control",
    "creator",
    "genesis",
    "player_input",
    "narrator_candidate",
    "extraction",
    "rule_internal",
    "prompt_display",
    "hud_display",
)
PHASES = (
    "bootstrap",
    "pre_settlement",
    "candidate_proposal",
    "gate_admission",
    "cold_extraction",
    "replay",
    "display_only",
)


def _scope(**changes) -> IngressScope:
    values = {
        "session_id": "session.ingress",
        "branch_id": "branch.ingress",
        "turn_index": 4,
        "attempt_id": "attempt.1",
        "source_start": 0,
        "source_end": len(D),
    }
    values.update(changes)
    return IngressScope(**values)


def _context(
    issuer_kind: str = "control",
    issuer_id: str = "control.console",
    channel: str = "trusted_control",
    authoring_phase: str = "bootstrap",
    *,
    scope: IngressScope | None = None,
) -> SemanticIngressContext:
    return SemanticIngressContext(
        issuer_kind=issuer_kind,
        issuer_id=issuer_id,
        channel=channel,
        authoring_phase=authoring_phase,
        scope=scope or _scope(),
    )


def _cohort_receipt(context: SemanticIngressContext | None = None):
    return issue_semantic_ingress_authority(
        D,
        context=context or _context(),
        grammar_id=COHORT_GRAMMAR_ID,
        grammar_version=COHORT_GRAMMAR_VERSION,
        declaration_id=COHORT_DECLARATION_ID,
        operation_families=(OP_BATTLE_COHORT_DECLARATION,),
    )


def _state_fixture():
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="semantic-ingress-state")
    seeded = apply_delta(
        store,
        sid,
        bid,
        0,
        [
            {"op": "entity_add", "name": "Kael", "kind": "player"},
            {
                "op": "player_seed",
                "entity": "Kael",
                "card": {
                    "stats": {"STR": 14},
                    "skills": {"melee": 3},
                    "resources": {"hp": {"max": 24}},
                },
            },
        ],
        "genesis",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    scope = IngressScope(
        session_id=sid,
        branch_id=bid,
        turn_index=1,
        attempt_id="attempt.state.1",
        source_start=0,
        source_end=len(D),
    )
    context = SemanticIngressContext(
        issuer_kind="narrator",
        issuer_id="narrator.main",
        channel="narrator_candidate",
        authoring_phase="candidate_proposal",
        scope=scope,
    )
    authority = issue_semantic_ingress_authority(
        D,
        context=context,
        grammar_id=COHORT_GRAMMAR_ID,
        grammar_version=COHORT_GRAMMAR_VERSION,
        declaration_id=COHORT_DECLARATION_ID,
        operation_families=(OP_BATTLE_COHORT_DECLARATION,),
    )
    declaration = tier0.parse_authorized_cohort_tags(
        D,
        current_state(store, bid),
        authority=authority,
        expected_context=context,
    )
    return cfg, store, sid, bid, scope, context, authority, declaration


@pytest.mark.parametrize(
    ("issuer_kind", "issuer_id", "channel", "phase"),
    [
        ("control", "control.console", "trusted_control", "bootstrap"),
        ("creator", "creator.session", "creator", "bootstrap"),
        ("genesis", "genesis.batch", "genesis", "bootstrap"),
        ("narrator", "narrator.main", "narrator_candidate", "candidate_proposal"),
    ],
    ids=("trusted-control", "creator", "genesis", "authorized-narrator-proposal"),
)
def test_byte_identical_strict_count_six_declaration_only_parses_on_mapped_routes(
    issuer_kind: str,
    issuer_id: str,
    channel: str,
    phase: str,
):
    context = _context(issuer_kind, issuer_id, channel, phase)
    receipt = _cohort_receipt(context)
    state = empty_state()
    before = deepcopy(state)

    declaration = parse_authorized_cohort_declaration(
        D,
        state,
        authority=receipt,
        expected_context=context,
    )

    assert state == before
    assert declaration.receipt_fingerprint == receipt.fingerprint
    assert declaration.operation_family == OP_BATTLE_COHORT_DECLARATION
    operations = declaration.operations
    start = next(op for op in operations if op["op"] == "battle_start")
    spawns = [op for op in operations if op["op"] == "combatant_spawn"]
    assert start["cohort"]["name"] == "Baser Hollow"
    assert start["cohort"]["total"] == 6
    assert [op["cohort_index"] for op in spawns] == [1, 2, 3]


@pytest.mark.parametrize(
    ("issuer_kind", "issuer_id", "channel", "phase"),
    [
        ("player", "player.bean", "player_input", "pre_settlement"),
        ("player", "player.bean", "player_input", "candidate_proposal"),
        ("display", "display.prompt", "prompt_display", "display_only"),
        ("display", "display.hud", "hud_display", "display_only"),
        ("extractor", "extractor.cold", "extraction", "cold_extraction"),
        ("rule", "rule.reducer", "rule_internal", "gate_admission"),
        ("narrator", "narrator.main", "narrator_candidate", "pre_settlement"),
    ],
    ids=(
        "ordinary-player",
        "quoted-player",
        "prompt",
        "hud",
        "extraction",
        "rule-internal-text",
        "narrator-wrong-phase",
    ),
)
def test_byte_identical_declaration_has_zero_authority_on_unmapped_routes(
    issuer_kind: str,
    issuer_id: str,
    channel: str,
    phase: str,
):
    context = _context(issuer_kind, issuer_id, channel, phase)

    with pytest.raises(SemanticIngressError, match="not an authorized ingress route"):
        _cohort_receipt(context)


def test_closed_cohort_route_table_has_no_unintended_producer_channel_phase_combination():
    allowed = {
        ("control", "trusted_control", "bootstrap"),
        ("creator", "creator", "bootstrap"),
        ("genesis", "genesis", "bootstrap"),
        ("narrator", "narrator_candidate", "candidate_proposal"),
    }
    for issuer_kind, issuer_id in ISSUERS:
        for channel in CHANNELS:
            for phase in PHASES:
                context = _context(issuer_kind, issuer_id, channel, phase)
                expected = (issuer_kind, channel, phase) in allowed
                try:
                    _cohort_receipt(context)
                except SemanticIngressError:
                    actual = False
                else:
                    actual = True
                assert actual is expected, (issuer_kind, channel, phase)


def test_parser_rejects_wrong_producer_channel_session_turn_attempt_and_source_span():
    receipt = _cohort_receipt()
    controls = [
        _context("narrator", "narrator.main", "narrator_candidate", "candidate_proposal"),
        _context(issuer_id="control.other"),
        _context(channel="creator"),
        _context(authoring_phase="candidate_proposal"),
        _context(scope=_scope(session_id="session.other")),
        _context(scope=_scope(branch_id="branch.other")),
        _context(scope=_scope(turn_index=5)),
        _context(scope=_scope(attempt_id="attempt.2")),
        _context(scope=_scope(source_start=1)),
    ]

    for expected in controls:
        with pytest.raises(SemanticIngressError):
            parse_authorized_cohort_declaration(
                D,
                empty_state(),
                authority=receipt,
                expected_context=expected,
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grammar_id", "other.cohort"),
        ("grammar_version", "2"),
        ("declaration_id", "battle-cohort.other/1"),
        ("operation_families", ("world_fact_proposal",)),
    ],
)
def test_wrong_grammar_declaration_or_operation_family_cannot_be_issued(
    field: str,
    value,
):
    arguments = {
        "grammar_id": COHORT_GRAMMAR_ID,
        "grammar_version": COHORT_GRAMMAR_VERSION,
        "declaration_id": COHORT_DECLARATION_ID,
        "operation_families": (OP_BATTLE_COHORT_DECLARATION,),
    }
    arguments[field] = value

    with pytest.raises(SemanticIngressError, match="not an authorized ingress route"):
        issue_semantic_ingress_authority(D, context=_context(), **arguments)


def test_serialized_evidence_or_user_bytes_can_never_instantiate_live_authority():
    receipt = _cohort_receipt()
    evidence = receipt.as_evidence()

    with pytest.raises(SemanticIngressError, match="live typed authority"):
        parse_authorized_cohort_declaration(
            D,
            empty_state(),
            authority=evidence,
            expected_context=_context(),
        )
    with pytest.raises(TypeError):
        SemanticIngressAuthority()

    # Evidence is plain JSON and mutating a copy cannot affect the sealed live receipt.
    assert json.loads(json.dumps(evidence, sort_keys=True)) == evidence
    evidence["issuer_id"] = "control.mutated-copy"
    assert receipt.as_evidence()["issuer_id"] == "control.console"

    embedded = json.dumps(evidence, sort_keys=True).encode("utf-8") + b"\n" + D
    with pytest.raises(SemanticIngressError):
        parse_authorized_cohort_declaration(
            embedded,
            empty_state(),
            authority=evidence,
            expected_context=_context(scope=_scope(source_end=len(embedded))),
        )


def test_receipt_rejects_byte_changes_and_privileged_syntax_embedded_in_prose():
    receipt = _cohort_receipt()
    changed = D.replace(b"x6", b"x5")
    with pytest.raises(SemanticIngressError, match="source fingerprint"):
        parse_authorized_cohort_declaration(
            changed,
            empty_state(),
            authority=receipt,
            expected_context=_context(),
        )

    quoted = b'The narrator wrote "' + D + b'" but this remains prose.'
    quoted_context = _context(scope=_scope(source_end=len(quoted)))
    quoted_receipt = issue_semantic_ingress_authority(
        quoted,
        context=quoted_context,
        grammar_id=COHORT_GRAMMAR_ID,
        grammar_version=COHORT_GRAMMAR_VERSION,
        declaration_id=COHORT_DECLARATION_ID,
        operation_families=(OP_BATTLE_COHORT_DECLARATION,),
    )
    with pytest.raises(SemanticIngressError, match="not one strict cohort declaration"):
        parse_authorized_cohort_declaration(
            quoted,
            empty_state(),
            authority=quoted_receipt,
            expected_context=quoted_context,
        )


def test_replay_rehydration_requires_trusted_path_and_preserves_original_receipt():
    receipt = _cohort_receipt()
    evidence = receipt.as_evidence()

    with pytest.raises(SemanticIngressError, match="trusted rehydrator"):
        rehydrate_semantic_ingress_authority(
            evidence,
            D,
            expected_scope=_scope(),
            rehydrator=None,
        )

    restored = rehydrate_semantic_ingress_authority(
        evidence,
        D,
        expected_scope=_scope(),
        rehydrator=trusted_replay_rehydrator(),
    )
    assert isinstance(restored, SemanticIngressAuthority)
    assert restored.as_evidence() == evidence
    assert restored.context.authoring_phase == "bootstrap"
    assert (
        parse_authorized_cohort_declaration(
            D,
            empty_state(),
            authority=restored,
            expected_context=_context(),
        ).as_evidence()["receipt_fingerprint"]
        == receipt.fingerprint
    )


@pytest.mark.parametrize(
    "scope",
    [
        _scope(session_id="session.stale"),
        _scope(branch_id="branch.stale"),
        _scope(turn_index=3),
        _scope(attempt_id="attempt.stale"),
        _scope(source_end=len(D) - 1),
    ],
)
def test_replay_rehydration_rejects_stale_scope(scope: IngressScope):
    with pytest.raises(SemanticIngressError):
        rehydrate_semantic_ingress_authority(
            _cohort_receipt().as_evidence(),
            D,
            expected_scope=scope,
            rehydrator=trusted_replay_rehydrator(),
        )


def test_replay_rehydration_rejects_forged_serialized_fingerprint():
    evidence = _cohort_receipt().as_evidence()
    evidence["issuer_id"] = "control.forged"

    with pytest.raises(SemanticIngressError, match="fingerprint"):
        rehydrate_semantic_ingress_authority(
            evidence,
            D,
            expected_scope=_scope(),
            rehydrator=trusted_replay_rehydrator(),
        )


def test_state_admission_requires_live_authority_and_journals_exact_json_evidence():
    cfg, store, sid, bid, scope, context, authority, declaration = _state_fixture()
    before = current_state(store, bid)
    operations = list(declaration.operations)

    raw = apply_delta(store, sid, bid, 1, operations, "rule", cfg)
    assert not raw.applied
    assert "complete live ingress context" in raw.quarantined[0]["reason"]
    assert current_state(store, bid) == before

    serialized_only = apply_delta(
        store,
        sid,
        bid,
        1,
        operations,
        "rule",
        cfg,
        semantic_declaration=declaration,
        semantic_authority=authority.as_evidence(),
        semantic_context=context,
        semantic_source=D,
    )
    assert not serialized_only.applied
    assert "typed live ingress objects" in serialized_only.quarantined[0]["reason"]
    assert current_state(store, bid) == before

    admitted = apply_delta(
        store,
        sid,
        bid,
        1,
        operations,
        "rule",
        cfg,
        semantic_declaration=declaration,
        semantic_authority=authority,
        semantic_context=context,
        semantic_source=D,
    )
    assert not admitted.quarantined
    assert [op["op"] for op in admitted.applied] == [
        "battle_start",
        "combatant_spawn",
        "combatant_spawn",
        "combatant_spawn",
    ]
    assert all(op["_semantic_ingress"] == authority.as_evidence()
               for op in admitted.applied)
    assert all(op["_semantic_declaration"] == declaration.as_evidence()
               for op in admitted.applied)
    assert json.loads(json.dumps(admitted.applied, ensure_ascii=False)) == admitted.applied

    journal = store.rule_ops_at(bid, 1)
    start = next(op for op in journal if op["op"] == "battle_start")
    restored = rehydrate_semantic_ingress_authority(
        start["_semantic_ingress"],
        D,
        expected_scope=scope,
        rehydrator=trusted_replay_rehydrator(),
    )
    assert restored.as_evidence() == authority.as_evidence()
    cohort = admitted.state["battle"]["cohort"]
    assert cohort["ingress"] == authority.as_evidence()
    assert cohort["declaration"] == declaration.as_evidence()
    assert cohort["initial_count"] == 3
    assert store.state_at(bid, 10**9, reduce_state, empty=empty_state()) == admitted.state


def test_state_admission_rejects_every_scope_or_route_mismatch_before_mutation():
    cfg, store, sid, bid, _scope_value, context, authority, declaration = _state_fixture()
    before = current_state(store, bid)
    operations = list(declaration.operations)
    controls = [
        replace(context, issuer_id="narrator.other"),
        replace(context, channel="creator"),
        replace(context, authoring_phase="bootstrap"),
        replace(context, scope=replace(context.scope, session_id="session.other")),
        replace(context, scope=replace(context.scope, branch_id="branch.other")),
        replace(context, scope=replace(context.scope, turn_index=2)),
        replace(context, scope=replace(context.scope, attempt_id="attempt.other")),
        replace(context, scope=replace(context.scope, source_start=1)),
    ]
    for wrong_context in controls:
        rejected = apply_delta(
            store,
            sid,
            bid,
            1,
            operations,
            "rule",
            cfg,
            semantic_declaration=declaration,
            semantic_authority=authority,
            semantic_context=wrong_context,
            semantic_source=D,
        )
        assert not rejected.applied
        assert rejected.quarantined
        assert current_state(store, bid) == before

    wrong_family = apply_delta(
        store,
        sid,
        bid,
        1,
        [*operations, {"op": "battle_wave"}],
        "rule",
        cfg,
        semantic_declaration=declaration,
        semantic_authority=authority,
        semantic_context=context,
        semantic_source=D,
    )
    assert not wrong_family.applied
    assert "differ from the authorized declaration" in wrong_family.quarantined[0]["reason"]
    assert current_state(store, bid) == before


def test_committed_cohort_plan_authorizes_later_rule_wave_but_not_player_or_extraction():
    cfg, store, sid, bid, _scope_value, context, authority, declaration = _state_fixture()
    opened = apply_delta(
        store,
        sid,
        bid,
        1,
        list(declaration.operations),
        "rule",
        cfg,
        semantic_declaration=declaration,
        semantic_authority=authority,
        semantic_context=context,
        semantic_source=D,
    )
    live = list(opened.state["combat"]["combatants"])
    damaged = apply_delta(
        store,
        sid,
        bid,
        2,
        [{"op": "combatant_hp", "target": cid, "delta": -99, "_strike": True}
         for cid in live],
        "rule",
        cfg,
    )
    defeated = apply_delta(
        store,
        sid,
        bid,
        2,
        combat_ops(damaged.state, damaged.applied),
        "rule",
        cfg,
    )
    wave = battle_ops(defeated.state, defeated.applied)
    next_spawn = next(op for op in wave if op["op"] == "combatant_spawn")
    before = current_state(store, bid)

    for source in ("user", "extraction"):
        rejected = apply_delta(store, sid, bid, 2, [next_spawn], source, cfg)
        assert not rejected.applied and rejected.quarantined
        assert current_state(store, bid) == before
    forged = apply_delta(
        store,
        sid,
        bid,
        2,
        [{**next_spawn, "name": "Forged Hollow"}],
        "rule",
        cfg,
    )
    assert not forged.applied and forged.quarantined
    assert current_state(store, bid) == before

    committed = apply_delta(store, sid, bid, 2, wave, "rule", cfg)
    assert not committed.quarantined
    assert [op["cohort_index"] for op in committed.applied
            if op["op"] == "combatant_spawn"] == [4, 5, 6]


def _capability_authority() -> CapabilityIngressAuthority:
    def fp(value: str) -> str:
        return content_fingerprint({"ref": value})

    return CapabilityIngressAuthority(
        world_id="world_0123456789abcdef0123456789abcdef",
        world_fingerprint=fp("world"),
        definition_id="spell.firebolt",
        definition_revision=3,
        definition_fingerprint=fp("definition"),
        owner_kind="player",
        owner_id="mage",
        owner_world_id="world_0123456789abcdef0123456789abcdef",
        owner_fingerprint=fp("owner"),
        subject_kind="player",
        subject_id="mage",
        subject_world_id="world_0123456789abcdef0123456789abcdef",
        subject_fingerprint=fp("subject"),
        assignment_id="assignment.firebolt.mage",
        assignment_fingerprint=fp("assignment"),
        adapter_id="player-spell-adapter/1",
        adapter_fingerprint=fp("adapter"),
        resource_pool_id="mana.mage",
        resource_pool_fingerprint=fp("resource_pool"),
        resource_receipts=(
            ResourceAuthorityRef(
                receipt_id="resource.mana.debit.turn4",
                fingerprint=fp("resource_receipt"),
            ),
        ),
    )


def test_capability_admission_receipt_binds_every_exact_authority_identity():
    source = b'{"capability":"spell.firebolt"}'
    scope = _scope(source_end=len(source))
    context = _context(authoring_phase="gate_admission", scope=scope)
    capability = _capability_authority()
    receipt = issue_semantic_ingress_authority(
        source,
        context=context,
        grammar_id=CAPABILITY_AUTHORITY_GRAMMAR_ID,
        grammar_version=CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
        declaration_id=CAPABILITY_ADMISSION_DECLARATION_ID,
        operation_families=(OP_CAPABILITY_ADMISSION,),
        capability_authority=capability,
    )

    validate_semantic_ingress_authority(
        receipt,
        source,
        expected_context=context,
        grammar_id=CAPABILITY_AUTHORITY_GRAMMAR_ID,
        grammar_version=CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
        declaration_id=CAPABILITY_ADMISSION_DECLARATION_ID,
        operation_family=OP_CAPABILITY_ADMISSION,
    )
    evidence = receipt.as_evidence()["capability_authority"]
    assert evidence == capability.as_dict()
    assert evidence["definition_fingerprint"] == capability.definition_fingerprint
    assert evidence["world_id"] == capability.world_id
    assert evidence["owner_id"] == "mage"
    assert evidence["assignment_id"] == "assignment.firebolt.mage"
    assert evidence["adapter_id"] == "player-spell-adapter/1"
    assert evidence["resource_receipts"][0]["receipt_id"] == "resource.mana.debit.turn4"


def test_capability_admission_cannot_issue_without_full_capability_authority():
    source = b'{"capability":"spell.firebolt"}'
    context = _context(
        authoring_phase="gate_admission",
        scope=_scope(source_end=len(source)),
    )

    with pytest.raises(SemanticIngressError, match="capability authority"):
        issue_semantic_ingress_authority(
            source,
            context=context,
            grammar_id=CAPABILITY_AUTHORITY_GRAMMAR_ID,
            grammar_version=CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
            declaration_id=CAPABILITY_ADMISSION_DECLARATION_ID,
            operation_families=(OP_CAPABILITY_ADMISSION,),
        )


def test_closed_capability_route_table_only_allows_trusted_control_gate_admission():
    source = b'{"capability":"spell.firebolt"}'
    capability = _capability_authority()
    for issuer_kind, issuer_id in ISSUERS:
        for channel in CHANNELS:
            for phase in PHASES:
                context = _context(
                    issuer_kind,
                    issuer_id,
                    channel,
                    phase,
                    scope=_scope(source_end=len(source)),
                )
                expected = (
                    issuer_kind == "control" and channel == "trusted_control" and phase == "gate_admission"
                )
                try:
                    issue_semantic_ingress_authority(
                        source,
                        context=context,
                        grammar_id=CAPABILITY_AUTHORITY_GRAMMAR_ID,
                        grammar_version=CAPABILITY_AUTHORITY_GRAMMAR_VERSION,
                        declaration_id=CAPABILITY_ADMISSION_DECLARATION_ID,
                        operation_families=(OP_CAPABILITY_ADMISSION,),
                        capability_authority=capability,
                    )
                except SemanticIngressError:
                    actual = False
                else:
                    actual = True
                assert actual is expected, (issuer_kind, channel, phase)


def test_capability_authority_rejects_cross_world_owner_or_subject():
    capability = _capability_authority()

    with pytest.raises(SemanticIngressError, match="same exact world"):
        replace(
            capability,
            owner_world_id="world_ffffffffffffffffffffffffffffffff",
        )
    with pytest.raises(SemanticIngressError, match="same exact world"):
        replace(
            capability,
            subject_world_id="world_ffffffffffffffffffffffffffffffff",
        )
