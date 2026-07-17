from __future__ import annotations

from dataclasses import replace

import pytest

from aetherstate.capability_glossary import GlossaryMatch
from aetherstate.worldlex import (
    ADAPTER_CONTRACT_SCHEMA,
    CAPABILITY_POOL_SCHEMA,
    CONTEXT_FRAME_SCHEMA,
    DEFINITION_REF_SCHEMA,
    OWNER_REF_SCHEMA,
    POOL_STAGES,
    SUBJECT_REF_SCHEMA,
    TRANSLATION_RESULT_SCHEMA,
    WORLDLEX_NAME,
    WORLDLEX_SHORT_NAME,
    AdapterContract,
    AdapterRegistry,
    CapabilityPool,
    ContextFrame,
    DefinitionRef,
    OwnerRef,
    PoolMember,
    SubjectRef,
    WorldLexError,
    build_translation_result,
    validate_definition_ref,
    validate_adapter_contract,
    validate_pool,
    validate_pool_transition,
    validate_translation_result,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
WORLD = "world_" + "c" * 32


def _frame() -> ContextFrame:
    return ContextFrame(
        frame_id="frame-7",
        source_id="action-19",
        source_fingerprint=SHA_A,
        span_start=4,
        span_end=27,
        polarity="positive",
        modality="actual",
        time_scope="current",
        quoted=False,
    )


def _definition(
    definition_id: str = "policy.harbor_quarantine",
    revision: int = 3,
    fingerprint: str = SHA_B,
) -> DefinitionRef:
    return DefinitionRef(
        definition_schema="institution-policy-definition/1",
        definition_id=definition_id,
        revision=revision,
        fingerprint=fingerprint,
        world_id=WORLD,
        kind="institution_policy",
        owner=OwnerRef(kind="world", id=WORLD, world_id=WORLD),
    )


def _member(stage: str, definition: DefinitionRef | None = None) -> PoolMember:
    definition = definition or _definition()
    common = {
        "definition": definition,
        "recognized": True,
        "authorized": stage != "world_library",
        "executable": stage == "runtime",
        "assignment_ref": None if stage == "world_library" else "assignment:harbor-policy:7",
        "eligibility_ref": None if stage in {"world_library", "assigned"} else "eligibility:storm-season:11",
        "adapter_id": None if stage in {"world_library", "assigned"} else "institution-policy-adapter/1",
        "receipt_id": None if stage in {"world_library", "assigned"} else "institution-policy-receipt/1",
        "admission_ref": "admission:institution-policy:1" if stage == "runtime" else None,
        "classification": "executable" if stage == "runtime" else "recognized",
    }
    return PoolMember(**common)


def _pool(
    stage: str,
    member: PoolMember | None = None,
    *,
    parent_fingerprint: str | None = None,
) -> CapabilityPool:
    return CapabilityPool.create(
        pool_id="harbor-governance",
        stage=stage,
        world_id=WORLD,
        subject=SubjectRef(
            kind="institution",
            id="institution.harbor_authority",
            world_id=WORLD,
        ),
        members=[member or _member(stage)],
        parent_fingerprint=parent_fingerprint,
        context_fingerprint=SHA_C if stage in {"spawn_eligible", "runtime"} else None,
    )


def test_canonical_worldlex_names_and_schemas_are_explicit():
    assert WORLDLEX_NAME == "WorldLex Translation Memory"
    assert WORLDLEX_SHORT_NAME == "WorldLex"
    assert CONTEXT_FRAME_SCHEMA == "worldlex-context-frame/1"
    assert TRANSLATION_RESULT_SCHEMA == "worldlex-translation-result/1"
    assert SUBJECT_REF_SCHEMA == "worldlex-subject-ref/1"
    assert OWNER_REF_SCHEMA == "worldlex-owner-ref/1"
    assert DEFINITION_REF_SCHEMA == "worldlex-definition-ref/1"
    assert CAPABILITY_POOL_SCHEMA == "capability-pool/1"
    assert ADAPTER_CONTRACT_SCHEMA == "worldlex-adapter-contract/1"
    assert POOL_STAGES == ("world_library", "assigned", "spawn_eligible", "runtime")


def test_translation_result_abstains_without_inventing_meaning_or_authority():
    result = build_translation_result(
        frame=_frame(),
        matches=[],
        memory_fingerprints=[SHA_D],
        abstention_reason="No canonical concept survived contextual evidence.",
    )

    assert result.abstained is True
    assert result.candidates == ()
    assert result.abstention_reason.startswith("No canonical concept")
    assert validate_translation_result(result.as_dict()) == result


def test_glossary_match_remains_recognized_but_never_authorized_or_executable():
    match = GlossaryMatch(
        concept_id="skill.navigation",
        label="Navigation",
        categories=("movement_travel", "crafting_survival_logistics"),
        concept_type="skill",
        meaning_facets={
            "semantic_role": "capability_identity",
            "target_cardinality": "unspecified",
            "spatial_extent": "unspecified",
            "world_scope": "unspecified",
        },
        meaning_fingerprint=SHA_A,
        matched_phrase="plot a safe route",
        score=82,
        genre_ids=("pirate_nautical",),
        baseline="registry_corpus",
        receipt_ids=(),
    )
    result = build_translation_result(
        frame=_frame(),
        matches=[match],
        memory_fingerprints=[SHA_D],
    )
    candidate = result.candidates[0]

    assert candidate.recognized is True
    assert candidate.authorized is False
    assert candidate.executable is False
    assert candidate.requires_context_binding is True

    forged = result.as_dict()
    forged["candidates"][0]["authorized"] = True
    with pytest.raises(WorldLexError, match="cannot authorize or execute"):
        validate_translation_result(forged)


def test_definition_reference_requires_exact_revision_fingerprint_and_shape():
    reference = _definition()
    assert validate_definition_ref(reference.as_dict()) == reference
    assert reference.revision == 3

    forged_fingerprint = reference.as_dict()
    forged_fingerprint["fingerprint"] = "sha256:not-a-real-fingerprint"
    with pytest.raises(WorldLexError, match="fingerprint"):
        validate_definition_ref(forged_fingerprint)

    latest_alias = reference.as_dict()
    latest_alias["latest"] = True
    with pytest.raises(WorldLexError, match="unexpected fields"):
        validate_definition_ref(latest_alias)

    missing_revision = reference.as_dict()
    del missing_revision["revision"]
    with pytest.raises(WorldLexError, match="revision"):
        validate_definition_ref(missing_revision)


def test_pool_members_preserve_exact_definition_revision_and_reject_forgery():
    pool = _pool("world_library")
    assert pool.members[0].definition.revision == 3
    assert pool.members[0].definition.fingerprint == SHA_B
    assert validate_pool(pool.as_dict()) == pool

    forged = pool.as_dict()
    forged["members"][0]["definition"]["revision"] = 4
    with pytest.raises(WorldLexError, match="pool fingerprint"):
        validate_pool(forged)


def test_pool_stage_transition_must_be_one_step_with_exact_parent_and_members():
    library = _pool("world_library")
    assigned = _pool(
        "assigned",
        parent_fingerprint=library.fingerprint,
    )
    eligible = _pool(
        "spawn_eligible",
        parent_fingerprint=assigned.fingerprint,
    )
    runtime = _pool(
        "runtime",
        parent_fingerprint=eligible.fingerprint,
    )

    validate_pool_transition(library, assigned)
    validate_pool_transition(assigned, eligible)
    validate_pool_transition(eligible, runtime)

    with pytest.raises(WorldLexError, match="one stage"):
        validate_pool_transition(library, runtime)

    wrong_parent = _pool("spawn_eligible", parent_fingerprint=SHA_D)
    with pytest.raises(WorldLexError, match="parent fingerprint"):
        validate_pool_transition(assigned, wrong_parent)

    changed_definition = _member("spawn_eligible", _definition(revision=4, fingerprint=SHA_D))
    changed_pool = _pool(
        "spawn_eligible",
        changed_definition,
        parent_fingerprint=assigned.fingerprint,
    )
    with pytest.raises(WorldLexError, match="cannot add"):
        validate_pool_transition(assigned, changed_pool)


def test_pool_stages_may_narrow_but_never_invent_members():
    one = _definition("policy.harbor_quarantine", fingerprint=SHA_B)
    two = _definition("policy.emergency_rations", fingerprint=SHA_D)
    library = CapabilityPool.create(
        pool_id="harbor-world-library",
        stage="world_library",
        world_id=WORLD,
        subject=SubjectRef("world", WORLD, WORLD),
        members=[_member("world_library", one), _member("world_library", two)],
    )
    assigned = CapabilityPool.create(
        pool_id="harbor-governance",
        stage="assigned",
        world_id=WORLD,
        subject=SubjectRef("institution", "institution.harbor_authority", WORLD),
        members=[_member("assigned", one)],
        parent_fingerprint=library.fingerprint,
    )
    validate_pool_transition(library, assigned)

    invented = CapabilityPool.create(
        pool_id="harbor-governance",
        stage="assigned",
        world_id=WORLD,
        subject=assigned.subject,
        members=[_member("assigned", _definition("policy.invented", fingerprint=SHA_C))],
        parent_fingerprint=library.fingerprint,
    )
    with pytest.raises(WorldLexError, match="cannot add"):
        validate_pool_transition(library, invented)


def test_stage_status_never_promotes_recognition_without_required_external_evidence():
    with pytest.raises(WorldLexError, match="world_library"):
        _pool("world_library", replace(_member("world_library"), authorized=True))

    with pytest.raises(WorldLexError, match="assignment_ref"):
        _pool(
            "assigned",
            replace(_member("assigned"), assignment_ref=None),
            parent_fingerprint=SHA_A,
        )

    with pytest.raises(WorldLexError, match="cannot be executable"):
        _pool(
            "spawn_eligible",
            replace(
                _member("spawn_eligible"),
                executable=True,
                classification="executable",
            ),
            parent_fingerprint=SHA_A,
        )

    with pytest.raises(WorldLexError, match="admission_ref"):
        _pool(
            "runtime",
            replace(_member("runtime"), admission_ref=None),
            parent_fingerprint=SHA_A,
        )


def test_adapter_identity_is_versioned_and_distinct_from_reducer_receipt_identity():
    contract = AdapterContract.create(
        adapter_id="institution-policy-adapter/1",
        receipt_id="institution-policy-receipt/1",
        definition_schemas=["institution-policy-definition/1"],
        definition_kinds=["institution_policy"],
        consumed_fields=["jurisdiction", "duration", "resource_budget"],
    )
    registry = AdapterRegistry([contract])

    assert validate_adapter_contract(contract.as_dict()) == contract

    assert (
        registry.require_binding(
            adapter_id="institution-policy-adapter/1",
            receipt_id="institution-policy-receipt/1",
            definition=_definition(),
        )
        == contract
    )

    with pytest.raises(WorldLexError, match="distinct"):
        AdapterContract.create(
            adapter_id="institution-policy/1",
            receipt_id="institution-policy/1",
            definition_schemas=["institution-policy-definition/1"],
            definition_kinds=["institution_policy"],
            consumed_fields=["jurisdiction"],
        )

    with pytest.raises(WorldLexError, match="receipt identity"):
        registry.require_binding(
            adapter_id="institution-policy-adapter/1",
            receipt_id="some-other-receipt/1",
            definition=_definition(),
        )

    with pytest.raises(WorldLexError, match="versioned"):
        AdapterContract.create(
            adapter_id="unversioned-adapter",
            receipt_id="institution-policy-receipt/1",
            definition_schemas=["institution-policy-definition/1"],
            definition_kinds=["institution_policy"],
            consumed_fields=["jurisdiction"],
        )

    forged = contract.as_dict()
    forged["consumed_fields"] = ["jurisdiction"]
    with pytest.raises(WorldLexError, match="fingerprint"):
        validate_adapter_contract(forged)


def test_pool_fingerprints_are_deterministic_across_member_input_order():
    one = _definition("policy.harbor_quarantine", fingerprint=SHA_B)
    two = _definition("policy.emergency_rations", fingerprint=SHA_D)
    left = CapabilityPool.create(
        pool_id="harbor-governance",
        stage="world_library",
        world_id=WORLD,
        subject=SubjectRef("institution", "institution.harbor_authority", WORLD),
        members=[_member("world_library", one), _member("world_library", two)],
    )
    right = CapabilityPool.create(
        pool_id="harbor-governance",
        stage="world_library",
        world_id=WORLD,
        subject=SubjectRef("institution", "institution.harbor_authority", WORLD),
        members=[_member("world_library", two), _member("world_library", one)],
    )

    assert left.fingerprint == right.fingerprint
    assert left.as_dict() == right.as_dict()


def test_noncombat_institution_pool_uses_generic_shape_without_combat_assumptions():
    library = _pool("world_library")
    assigned = _pool("assigned", parent_fingerprint=library.fingerprint)
    eligible = _pool("spawn_eligible", parent_fingerprint=assigned.fingerprint)
    runtime = _pool("runtime", parent_fingerprint=eligible.fingerprint)
    payload = runtime.as_dict()

    assert payload["subject"] == {
        "schema": SUBJECT_REF_SCHEMA,
        "kind": "institution",
        "id": "institution.harbor_authority",
        "world_id": WORLD,
    }
    assert payload["members"][0]["definition"]["kind"] == "institution_policy"
    assert payload["members"][0]["authorized"] is True
    assert payload["members"][0]["executable"] is True
    serialized_keys = repr(payload).casefold()
    for combat_only_key in ("moves", "kit_size", "tier", "damage", "hp", "pending_intent"):
        assert combat_only_key not in serialized_keys
