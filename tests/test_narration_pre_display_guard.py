"""Pre-display prevention for mechanically false RPG narration."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from aetherstate.capability_glossary import content_fingerprint
from aetherstate.config import Config
from aetherstate.narration_pre_display_guard import (
    NARRATION_PRE_DISPLAY_GUARD_SCHEMA,
    NarrationGuardBasis,
    build_narration_guard_basis,
    guard_narration_story,
    narration_contradictions,
    narration_guard_state_fingerprint,
)
from aetherstate.narrator_realization import build_narrator_realization
from aetherstate.pipeline import Pipeline, PostContext
from aetherstate.proxy import make_relay_router
from aetherstate.response_wire import decode_chat_story, encode_chat_story
from aetherstate.session_engine import SessionEngine
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta
from tests.mock_upstream import MockUpstream, Reply
from tests.test_skill_check_settlement_state import _ops, _runtime


TURN = 7


def _fp(seed: str) -> str:
    return content_fingerprint({"guard-test": seed})


def _meaning(*, capability: str, action_class: str, target: str | None) -> dict:
    return {
        "meaning_ref": _fp(f"meaning:{capability}:{target}"),
        "actor_id": "seraphine",
        "capability_id": capability,
        "invoked_capability_ids": [],
        "action_class": action_class,
        "target_entity_id": target,
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


def _skill_realization(*, outcome: str = "success") -> dict:
    return build_narrator_realization(
        TURN,
        asserted_settled=[{
            "event_ref": "event.elementalism",
            "adapter_id": "narrator.skill-check/1",
            "frame_ref": _fp("frame:elementalism"),
            "event_meaning": _meaning(
                capability="elementalism", action_class="skill_check", target=None,
            ),
            "outcome_quality": outcome,
            "impact_kind": "none",
            "impact_magnitude": "none",
            "target_state": "not_applicable",
            "settled_change_kinds": ["mastery"],
        }],
    )


def _weapon_realization(*, target: str = "hollowed") -> dict:
    return build_narrator_realization(
        TURN,
        asserted_settled=[{
            "event_ref": "event.weapon",
            "adapter_id": "narrator.weapon-attack/1",
            "frame_ref": _fp(f"frame:weapon:{target}"),
            "event_meaning": _meaning(
                capability="weapon_attack", action_class="weapon_attack", target=target,
            ),
            "outcome_quality": "success",
            "impact_kind": "harm",
            "impact_magnitude": "solid",
            "target_state": "active",
            "settled_change_kinds": ["hp"],
        }],
    )


def _combat_state() -> dict:
    return {
        "meta": {"turn": TURN},
        "entities": {
            "seraphine": {"name": "Seraphine", "present": True},
        },
        "player": {"seraphine": {}},
        "rolls": [{
            "turn": TURN,
            "skill": "elementalism",
            "tier": "success",
            "result": 11,
            "target": None,
        }],
        "combat": {
            "active": True,
            "combatants": {
                "hollowed": {
                    "id": "hollowed",
                    "name": "Hollowed",
                    "side": "enemy",
                    "defeated": False,
                    "hp": {"cur": 6, "max": 6},
                    "cohort": {"ref": "hollowed_x4", "index": 1, "total": 4},
                },
                "hollowed#2": {
                    "id": "hollowed#2",
                    "name": "Hollowed",
                    "side": "enemy",
                    "defeated": False,
                    "hp": {"cur": 6, "max": 6},
                    "cohort": {"ref": "hollowed_x4", "index": 2, "total": 4},
                },
            },
        },
    }


def _large_cohort_state() -> dict:
    state = _combat_state()
    state["combat"]["combatants"] = {
        f"baser_hollow#{index}": {
            "id": f"baser_hollow#{index}",
            "name": "Baser Hollow",
            "side": "enemy",
            "defeated": False,
            "hp": {"cur": 6, "max": 6},
            "cohort": {"ref": "baser_hollow_x27", "index": index, "total": 27},
        }
        for index in range(1, 28)
    }
    return state


def _cfg() -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    return cfg


def test_no_impact_skill_blocks_clear_enemy_harm_and_death() -> None:
    reasons = narration_contradictions(
        _skill_realization(),
        _combat_state(),
        "The ice spike punches clean through Hollowed #1 and kills it instantly.",
        _cfg(),
        turn_index=TURN,
    )

    assert "unsettled_combatant_impact" in reasons
    assert "unsettled_combatant_defeat" in reasons


def test_enemy_as_actor_is_not_misread_as_damage_to_that_enemy() -> None:
    for story in (
        "Hollowed #1 hits Seraphine across the shoulder.",
        "Hollowed #1 wounded Seraphine across the shoulder.",
    ):
        assert narration_contradictions(
            _skill_realization(),
            _combat_state(),
            story,
            _cfg(),
            turn_index=TURN,
        ) == (), story


def test_completed_enemy_clause_does_not_retarget_a_later_enemy_subject() -> None:
    state = _combat_state()
    realization = _skill_realization()

    assert narration_contradictions(
        realization,
        state,
        "Hollowed #1 strikes Seraphine, and Hollowed #2 circles warily.",
        _cfg(),
        turn_index=TURN,
    ) == ()
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        state,
        "Hollowed #1 strikes Seraphine, then the blade cuts Hollowed #2.",
        _cfg(),
        turn_index=TURN,
    )


def test_departure_is_not_defeat_without_a_death_state_complement() -> None:
    state = _combat_state()
    realization = _skill_realization()

    assert narration_contradictions(
        realization,
        state,
        "Seraphine leaves Hollowed #1 behind to guard the doorway.",
        _cfg(),
        turn_index=TURN,
    ) == ()
    assert "unsettled_combatant_defeat" in narration_contradictions(
        realization,
        state,
        "Seraphine leaves Hollowed #1 dead at the doorway.",
        _cfg(),
        turn_index=TURN,
    )


def test_existing_wound_state_is_not_reclassified_as_a_fresh_impact() -> None:
    state = _combat_state()
    state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 4
    realization = _skill_realization()

    for story in (
        "Hollowed #1 is wounded but still advances.",
        "The wounded Hollowed #1 still advances.",
        "Hollowed #1, already wounded, still advances.",
    ):
        assert narration_contradictions(
            realization,
            state,
            story,
            _cfg(),
            turn_index=TURN,
        ) == (), story

    full_health = _combat_state()
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        full_health,
        "Hollowed #1 is wounded but still advances.",
        _cfg(),
        turn_index=TURN,
    )
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        state,
        "Hollowed #1 is wounded by the new ice spike.",
        _cfg(),
        turn_index=TURN,
    )


def test_existing_wound_does_not_authorize_fresh_result_causality() -> None:
    state = _combat_state()
    state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 4
    realization = _skill_realization()
    result_claims = (
        "bleeds",
        "screams in pain",
        "clutches its wound",
    )
    fresh_causes = (
        "from the new hit",
        "from this strike",
        "because of the current attack",
        "due to that projectile",
    )

    for result_claim in result_claims:
        for cause in fresh_causes:
            story = f"Hollowed #1 {result_claim} {cause}."
            assert "unsettled_combatant_impact" in narration_contradictions(
                realization, state, story, _cfg(), turn_index=TURN,
            ), story

    for injury in ("wounded", "injured", "bleeding", "burned"):
        for cause in (
            "by the new ice spike",
            "because of the current attack",
            "due to this projectile",
        ):
            story = f"Hollowed #1 is {injury} {cause}."
            assert "unsettled_combatant_impact" in narration_contradictions(
                realization, state, story, _cfg(), turn_index=TURN,
            ), story


def test_fresh_result_cause_is_replaced_before_display_despite_prior_wound() -> None:
    state = _combat_state()
    state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 4
    safe_story = "Seraphine's Elementalism check resolves without combatant impact."
    basis = NarrationGuardBasis(
        schema=NARRATION_PRE_DISPLAY_GUARD_SCHEMA,
        turn_index=TURN,
        state_fingerprint=narration_guard_state_fingerprint(state),
        realization=_skill_realization(),
        fallback_story=safe_story,
    )
    candidate = "Hollowed #1 bleeds from the new hit."

    decision = guard_narration_story(basis, state, candidate, _cfg())

    assert decision.accepted is False
    assert decision.story == safe_story
    assert candidate not in decision.story
    assert "unsettled_combatant_impact" in decision.reasons


def test_existing_wound_still_authorizes_plain_and_historical_harm_state() -> None:
    state = _combat_state()
    state["combat"]["combatants"]["hollowed"]["hp"]["cur"] = 4
    realization = _skill_realization()

    for story in (
        "Hollowed #1 bleeds from the wound.",
        "Hollowed #1 bleeds from a prior wound.",
        "Hollowed #1 screams in pain because of the earlier attack.",
        "Hollowed #1 clutches its wound due to injuries from yesterday's battle.",
        "Hollowed #1 is bleeding from an old wound.",
        "The blade sliced Hollowed #1 during the previous battle.",
    ):
        assert narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ) == (), story


def test_direct_harm_and_definite_defeat_lexical_families_are_guarded() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for verb, complement in (
        ("slices", "open"),
        ("sliced", "across the chest"),
        ("is slicing", "open"),
        ("cleaves", "through the shoulder"),
    ):
        story = f"The blade {verb} Hollowed #1 {complement}."
        assert "unsettled_combatant_impact" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story

    for story in (
        "Hollowed #1 perishes.",
        "Hollowed #1 perished.",
        "Hollowed #1 expires.",
        "Hollowed #1 succumbs.",
        "Hollowed #1 drops, unmoving.",
        "Hollowed #1 falls, motionless.",
    ):
        assert "unsettled_combatant_defeat" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story


def test_defeat_family_preserves_negation_and_non_actual_controls() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for story in (
        "Hollowed #1 does not perish.",
        "Hollowed #1 nearly perishes.",
        "Hollowed #1 would have perished.",
        "Hollowed #1 drops its blade, unmoving for a moment.",
    ):
        assert narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ) == (), story


def test_immediate_unique_combatant_pronoun_claims_are_guarded() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for continuation in (
        "The new ice spike pierces it through the chest.",
        "The blade slices it open.",
        "It bleeds from the new hit.",
    ):
        story = f"Hollowed #1 waits. {continuation}"
        assert "unsettled_combatant_impact" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story

    for continuation in (
        "The blow kills it.",
        "It perishes.",
        "It drops, unmoving.",
    ):
        story = f"Hollowed #1 waits. {continuation}"
        assert "unsettled_combatant_defeat" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story


def test_sentence_pronoun_oracle_refuses_ambiguous_or_non_actual_reference() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for story in (
        "Hollowed #1 watches Hollowed #2. The blade pierces it.",
        "The Hollowed waits. The blade pierces it.",
        "Hollowed #1 waits. Snow falls. The blade pierces it.",
        "Hollowed #1 waits. The spike does not pierce it.",
        "Hollowed #1 waits. The spike nearly pierces it.",
        "Hollowed #1 waits. The blade cuts the air before it.",
        "Hollowed #1 raises a shield. The blade pierces it.",
        "Hollowed #1 watches a bird. It drops, unmoving.",
    ):
        assert narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ) == (), story

    assert narration_contradictions(
        _weapon_realization(target="hollowed"),
        state,
        "Hollowed #1 waits. The blade slices it open.",
        _cfg(),
        turn_index=TURN,
    ) == ()


def test_state_owned_high_ordinal_surfaces_preserve_exact_guard_identity() -> None:
    state = _large_cohort_state()
    exact_harm = _weapon_realization(target="baser_hollow#21")

    for ordinal in ("twenty first", "twenty-first", "21st", "21"):
        story = f"The blade slices the {ordinal} Baser Hollow open."
        assert narration_contradictions(
            exact_harm, state, story, _cfg(), turn_index=TURN,
        ) == (), story


def test_no_impact_blocks_every_high_ordinal_surface() -> None:
    state = _large_cohort_state()
    realization = _skill_realization()

    for ordinal in ("twenty first", "twenty-first", "21st", "21"):
        story = f"The {ordinal} Baser Hollow perishes."
        assert "unsettled_combatant_defeat" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story


def test_named_target_then_later_harm_is_still_blocked() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for story in (
        "Hollowed #1 staggers as the spike pierces its chest.",
        "Hollowed #1 reels, wounded by the ice.",
        "Hollowed #1's chest is pierced.",
    ):
        assert "unsettled_combatant_impact" in narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ), story


def test_named_target_then_later_death_is_still_blocked() -> None:
    reasons = narration_contradictions(
        _skill_realization(),
        _combat_state(),
        "The first Hollowed is impaled through the chest and drops dead.",
        _cfg(),
        turn_index=TURN,
    )

    assert "unsettled_combatant_impact" in reasons
    assert "unsettled_combatant_defeat" in reasons


def test_named_enemy_actor_and_non_contact_attempts_remain_allowed() -> None:
    state = _combat_state()
    realization = _skill_realization()

    for story in (
        "Hollowed #1 lunges and pierces Seraphine.",
        "Seraphine tries to pierce Hollowed #1.",
        "The spike does not pierce Hollowed #1.",
        "The spike would have pierced Hollowed #1.",
        "The spike nearly pierces Hollowed #1.",
        "The blade cuts the air before Hollowed #1.",
        "The spike misses Hollowed #1.",
    ):
        assert narration_contradictions(
            realization, state, story, _cfg(), turn_index=TURN,
        ) == (), story


def test_correct_rich_prose_without_uncommitted_impact_passes_exactly() -> None:
    story = (
        "Frost gathers into a bright lance, and Hollowed #1 recoils from the sudden cold. "
        "The creature remains on its feet, untouched but wary."
    )

    assert narration_contradictions(
        _skill_realization(), _combat_state(), story, _cfg(), turn_index=TURN,
    ) == ()


def test_exact_settled_target_can_be_hurt_but_same_name_neighbor_cannot() -> None:
    realization = _weapon_realization(target="hollowed")
    state = _combat_state()

    assert narration_contradictions(
        realization,
        state,
        "The blade cuts Hollowed #1 across the chest.",
        _cfg(),
        turn_index=TURN,
    ) == ()
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        state,
        "The blade cuts Hollowed #1, then the arc cuts Hollowed #2 as well.",
        _cfg(),
        turn_index=TURN,
    )


def test_same_name_bare_and_ordinal_mentions_preserve_exact_target_identity() -> None:
    realization = _weapon_realization(target="hollowed")
    state = _combat_state()

    assert narration_contradictions(
        realization,
        state,
        "The blade cuts the first Hollowed across the chest.",
        _cfg(),
        turn_index=TURN,
    ) == ()
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        state,
        "The blade cuts the second Hollowed across the chest.",
        _cfg(),
        turn_index=TURN,
    )
    assert "unsettled_combatant_impact" in narration_contradictions(
        realization,
        state,
        "The blade cuts the Hollowed across the chest.",
        _cfg(),
        turn_index=TURN,
    )


def test_roll_result_conflict_is_a_pre_display_contradiction() -> None:
    state = _combat_state()
    state["rolls"][0]["tier"] = "fail"

    assert "roll_outcome_conflict" in narration_contradictions(
        _skill_realization(outcome="fail"),
        state,
        "Seraphine's Elementalism check succeeds completely.",
        _cfg(),
        turn_index=TURN,
    )


def test_negated_failure_word_does_not_contradict_a_successful_check() -> None:
    state = _combat_state()
    realization = _skill_realization(outcome="success")

    for story in (
        "Elementalism does not fail Seraphine.",
        "Elementalism doesn't fail Seraphine.",
        "Elementalism cannot possibly fail Seraphine.",
    ):
        assert narration_contradictions(
            realization,
            state,
            story,
            _cfg(),
            turn_index=TURN,
        ) == (), story
    assert "roll_outcome_conflict" in narration_contradictions(
        realization,
        state,
        "Elementalism fails Seraphine completely.",
        _cfg(),
        turn_index=TURN,
    )


def test_basis_builds_a_truthful_code_fallback_from_exact_journal() -> None:
    cfg, store, session_id, branch_id = _runtime("narration-guard-basis")
    applied = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )
    assert not applied.quarantined
    state = deepcopy(applied.state)
    state["combat"] = deepcopy(_combat_state()["combat"])

    basis = build_narration_guard_basis(
        state,
        branch_id=branch_id,
        turn_index=1,
        journal_rows=store.diagnostic_turn(branch_id, 1)["journal"],
    )

    assert basis is not None
    assert "Stealth check" in basis.fallback_story
    assert "success" in basis.fallback_story
    assert "damage" not in basis.fallback_story.lower()
    assert "dead" not in basis.fallback_story.lower()


def test_stale_prior_mechanics_do_not_arm_guard_on_a_pure_roleplay_turn() -> None:
    cfg, store, session_id, branch_id = _runtime("narration-guard-current-only")
    applied = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )

    assert build_narration_guard_basis(
        applied.state,
        branch_id=branch_id,
        turn_index=2,
        journal_rows=(),
    ) is None


def test_exact_lost_reply_retry_reuses_source_turn_authority() -> None:
    cfg, store, session_id, branch_id = _runtime("narration-guard-retry")
    applied = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )
    retry_state = deepcopy(applied.state)
    retry_state["_settled_retry"] = {"kind": "lost_reply", "source_turn": 1}

    basis = build_narration_guard_basis(
        retry_state,
        branch_id=branch_id,
        turn_index=2,
        journal_rows=store.diagnostic_turn(branch_id, 1)["journal"],
    )

    assert basis is not None and basis.turn_index == 1
    assert "Stealth check" in basis.fallback_story


def test_pipeline_arms_only_current_narrator_mechanics_and_leaves_following_rp_unbuffered() -> None:
    cfg, store, _session_id, _branch_id = _runtime("narration-guard-pipeline-current")
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    first_user = "I slip unseen past the watch. ((aether.check stealth vs 9))"
    first_body = json.dumps({
        "model": "narrator",
        "messages": [{"role": "user", "content": first_user}],
    }).encode()
    first_stamp = Stamp(
        session="narration-guard-pipeline-current",
        turn=1,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Kael",
    )

    _packet, first_ctx = pipe.process(first_stamp, first_body)

    assert first_ctx is not None and first_ctx.narration_guard is not None
    prior_story = "Shadows deepen around Kael while the watch scans the opposite archway."
    prior = encode_chat_story(
        prior_story, model="narrator", stream=False, artifact_ref="guard-prior-reply",
    )
    guarded, guarded_type = pipe.guard_response(first_ctx, prior.raw, prior.content_type)
    assert guarded == prior.raw and guarded_type == prior.content_type
    pipe.on_response(first_ctx, prior.raw, prior.content_type)

    second_body = json.dumps({
        "model": "narrator",
        "messages": [
            {"role": "user", "content": first_user},
            {"role": "assistant", "content": prior_story},
            {"role": "user", "content": "I sit quietly beside the rain-streaked window."},
        ],
    }).encode()
    second_stamp = Stamp(
        session="narration-guard-pipeline-current",
        turn=2,
        gen_type="normal",
        speaker="Narrator",
        card_role="narrator",
        user="Kael",
    )

    _packet, second_ctx = pipe.process(second_stamp, second_body)

    assert second_ctx is not None
    assert second_ctx.narration_guard is None


def test_none_specialization_never_arms_pre_display_guard() -> None:
    cfg, store, _session_id, _branch_id = _runtime("narration-guard-none")
    cfg.specialization.name = "none"
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    body = json.dumps({
        "model": "narrator",
        "messages": [{
            "role": "user",
            "content": "I slip unseen past the watch. ((aether.check stealth vs 9))",
        }],
    }).encode()

    _packet, ctx = pipe.process(
        Stamp(
            session="narration-guard-none",
            turn=1,
            speaker="Narrator",
            card_role="narrator",
            user="Kael",
        ),
        body,
    )

    assert ctx is not None and ctx.narration_guard is None


class _RecordVisibility:
    def __init__(self, app, events: list) -> None:
        self.app = app
        self.events = events
        self.started = False
        self.seen_body = False

    async def __call__(self, scope, receive, send) -> None:
        async def record(message):
            if message["type"] == "http.response.start" and not self.started:
                self.started = True
                self.events.append(("response_start",))
            if message["type"] == "http.response.body" and message.get("body") \
                    and not self.seen_body:
                self.seen_body = True
                self.events.append(("first_byte", bytes(message["body"])))
            await send(message)

        await self.app(scope, receive, record)


class _GuardPipeline:
    def __init__(self, events: list, *, replacement: bytes | None = None) -> None:
        self.events = events
        self.replacement = replacement
        self.ctx = SimpleNamespace(
            semantic_gate=False,
            semantic_replay=None,
            semantic_error="",
            local_response=None,
            narration_guard=True,
            narration_guard_replaced=False,
            narration_guard_reasons=(),
        )

    def process(self, _stamp, body: bytes):
        self.events.append(("process", body))
        return body, self.ctx

    def guard_response(self, ctx, raw: bytes, content_type: str):
        self.events.append(("guard", raw, content_type))
        if self.replacement is None:
            return raw, content_type
        ctx.narration_guard_replaced = True
        ctx.narration_guard_reasons = ("unsettled_combatant_impact",)
        return self.replacement, "application/json"

    def on_response(self, _ctx, raw: bytes, content_type: str) -> None:
        self.events.append(("on_response", raw, content_type))

    def on_upstream_error(self, _ctx, status: int, raw: bytes) -> None:
        self.events.append(("upstream_error", status, raw))

    def record_response_trace(self, _ctx, **fields) -> None:
        self.events.append(("trace", fields))


def _guard_app(cfg: Config, pipeline: _GuardPipeline, upstream_client, events: list):
    app = FastAPI()
    app.include_router(
        make_relay_router(lambda: upstream_client, cfg, pipeline=pipeline)
    )
    return _RecordVisibility(app, events)


async def test_proxy_streams_authoritative_turn_verbatim_before_cold_advisory() -> None:
    events: list = []
    story = "The Hollowed hold their ground."
    raw = encode_chat_story(
        story, model="narrator", stream=True, artifact_ref="accepted-rich-prose",
    ).raw
    upstream = MockUpstream()
    upstream.enqueue(Reply(
        headers={"content-type": "text/event-stream"},
        sse_chunks=[raw[:17], raw[17:71], raw[71:]],
    ))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream), base_url="http://upstream",
    )
    cfg = _cfg()
    cfg.upstream.base_url = "http://upstream/v1"
    pipeline = _GuardPipeline(events)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_guard_app(cfg, pipeline, upstream_client, events)
            ),
            base_url="http://proxy",
        ) as client:
            response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')
    finally:
        await upstream_client.aclose()

    assert response.content == raw
    assert [event[0] for event in events] == [
        "process", "response_start", "first_byte", "on_response", "trace",
    ]
    assert raw.startswith(events[2][1])
    assert events[3][1] == raw


async def test_proxy_fail_open_never_replaces_successful_upstream_narration(caplog) -> None:
    events: list = []
    secret_story = "The ice spike kills Hollowed #1."
    secret = encode_chat_story(
        secret_story, model="narrator", stream=False, artifact_ref="rejected-secret",
    ).raw
    upstream = MockUpstream()
    upstream.enqueue(Reply(
        headers={"content-type": "application/json"},
        body=secret,
    ))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream), base_url="http://upstream",
    )
    cfg = _cfg()
    cfg.upstream.base_url = "http://upstream/v1"
    pipeline = _GuardPipeline(events, replacement=b"forbidden replacement")

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_guard_app(cfg, pipeline, upstream_client, events)
            ),
            base_url="http://proxy",
        ) as client:
            response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')
    finally:
        await upstream_client.aclose()

    assert response.content == secret
    assert secret_story.encode() in response.content
    assert response.headers["content-type"] == "application/json"
    assert [event[0] for event in events] == [
        "process", "response_start", "first_byte", "on_response", "trace",
    ]
    assert events[2] == ("first_byte", secret)
    assert events[3][1:] == (secret, "application/json")
    assert events[4][1]["content_sha256"] \
        == __import__("hashlib").sha256(secret).hexdigest()
    assert secret_story not in json.dumps(events[4][1], sort_keys=True)
    assert secret_story not in caplog.text


async def test_guarded_context_still_relays_upstream_errors_verbatim() -> None:
    events: list = []
    error = b'{"error":{"message":"provider refused"}}'
    upstream = MockUpstream()
    upstream.enqueue(Reply(status=429, body=error))
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream), base_url="http://upstream",
    )
    cfg = _cfg()
    cfg.upstream.base_url = "http://upstream/v1"
    pipeline = _GuardPipeline(events)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_guard_app(cfg, pipeline, upstream_client, events)
            ),
            base_url="http://proxy",
        ) as client:
            response = await client.post("/v1/chat/completions", content=b'{"messages":[]}')
    finally:
        await upstream_client.aclose()

    assert response.status_code == 429
    assert response.content == error
    assert "guard" not in [event[0] for event in events]
    assert "upstream_error" in [event[0] for event in events]


def test_pipeline_guard_failure_is_advisory_and_preserves_original_wire() -> None:
    cfg, store, session_id, branch_id = _runtime("narration-guard-wire")
    applied = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )
    basis = build_narration_guard_basis(
        applied.state,
        branch_id=branch_id,
        turn_index=1,
        journal_rows=store.diagnostic_turn(branch_id, 1)["journal"],
    )
    assert basis is not None
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(
        session_id,
        branch_id,
        1,
        "new_turn",
        card_role="narrator",
        request_model="narrator",
        narration_guard=basis,
    )
    malformed = b'data: {"choices":[{"delta":{"content":"false harm"}}]}\n\n'

    guarded, content_type = pipe.guard_response(ctx, malformed, "text/event-stream")
    assert guarded == malformed
    assert content_type == "text/event-stream"
    assert ctx.narration_guard_replaced is False
    assert ctx.narration_guard_reasons == ("candidate_wire_or_guard_unavailable",)
    assert b"false harm" in guarded


def test_pipeline_guard_releases_verified_rich_wire_byte_exact() -> None:
    cfg, store, session_id, branch_id = _runtime("narration-guard-wire-accepted")
    applied = apply_delta(
        store, session_id, branch_id, 1, _ops(cfg, store, branch_id), "rule", cfg,
    )
    basis = build_narration_guard_basis(
        applied.state,
        branch_id=branch_id,
        turn_index=1,
        journal_rows=store.diagnostic_turn(branch_id, 1)["journal"],
    )
    assert basis is not None
    pipe = Pipeline(store, SessionEngine(store, cfg.session), cfg)
    ctx = PostContext(
        session_id,
        branch_id,
        1,
        "new_turn",
        card_role="narrator",
        request_model="narrator",
        narration_guard=basis,
    )
    artifact = encode_chat_story(
        "Kael's Stealth check resolves as success while the watch remains uncertain.",
        model="narrator",
        stream=True,
        artifact_ref="verified-rich-wire",
    )

    guarded, content_type = pipe.guard_response(ctx, artifact.raw, artifact.content_type)

    assert guarded == artifact.raw
    assert content_type == artifact.content_type
    assert ctx.narration_guard_replaced is False
