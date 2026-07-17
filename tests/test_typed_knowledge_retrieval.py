from __future__ import annotations

from copy import deepcopy

from aetherstate.knowledge import (
    normalize_actor_id,
    normalize_actor_scope,
    normalized_proposition,
    polarized_proposition_id,
    proposition_id,
    render_knowledge_context,
    select_knowledge,
)
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store
from aetherstate.world_events import build_world_event_record


WORLD = "world_" + "c" * 32
OTHER_WORLD = "world_" + "d" * 32
SESSION = "session-knowledge"
BRANCH = "branch-knowledge"


def _claim(
    claim_id: str,
    statement: str,
    *,
    turn: int,
    speaker: str = "mara",
    addressee: str | None = "player",
    visibility: str = "public",
    claim_proposition_id: str | None = None,
    world_id: str = WORLD,
    session_id: str = SESSION,
    branch_id: str = BRANCH,
) -> dict:
    neutral_id = proposition_id(statement)
    _, polarity = normalized_proposition(statement)
    return {
        "schema": "aetherstate-claim-record/1",
        "claim_id": claim_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "world_id": world_id,
        "turn": turn,
        "visibility": visibility,
        "scoped_actors": [value for value in (speaker, addressee) if value],
        "frame": {
            "schema": "aetherstate-claim-frame/2",
            "proposition": statement,
            "proposition_id": claim_proposition_id or polarized_proposition_id(statement),
            "proposition_identity": neutral_id,
            "proposition_polarity": polarity,
            "speech_act_polarity": "positive",
            "speaker": speaker,
            "addressee": addressee,
            "claim_class": "assertion",
            "modality": "asserted",
            "speaker_stance": {"value": "asserts"},
        },
    }


def _fact(
    fact_id: str,
    statement: str,
    *,
    turn: int,
    visibility: str = "public",
    world_id: str = WORLD,
    session_id: str = SESSION,
    branch_id: str = BRANCH,
) -> tuple[str, dict]:
    pid = polarized_proposition_id(statement)
    identity = proposition_id(statement)
    _, polarity = normalized_proposition(statement)
    return fact_id, {
        "schema": "aetherstate-fact-record/2",
        "fact_id": fact_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "world_id": world_id,
        "turn": turn,
        "proposition_id": pid,
        "proposition_identity": identity,
        "statement": statement,
        "proposition_polarity": polarity,
        "authority": "creator",
        "cause": "creator:test",
        "visibility": visibility,
        "status": "accepted",
    }


def _belief(
    belief_id: str,
    holder: str,
    statement: str,
    *,
    turn: int,
    stance: str = "believes",
    world_id: str = WORLD,
    session_id: str = SESSION,
    branch_id: str = BRANCH,
) -> tuple[str, dict]:
    pid = polarized_proposition_id(statement)
    identity = proposition_id(statement)
    _, polarity = normalized_proposition(statement)
    row = {
        "schema": "aetherstate-epistemic-record/2",
        "belief_id": belief_id,
        "session_id": session_id,
        "branch_id": branch_id,
        "world_id": world_id,
        "turn": turn,
        "holder": holder,
        "proposition_id": pid,
        "proposition_identity": identity,
        "statement": statement,
        "proposition_polarity": polarity,
        "stance": stance,
        "source": "told",
        "visibility": "actor_scoped",
        "scoped_actors": [holder],
        "status": "current",
    }
    return f"{holder}|{pid}", row


def _event(
    event_id: str,
    description: str,
    *,
    world_id: str = WORLD,
    branch_id: str = BRANCH,
    game_time: int = 0,
    start: int = 0,
    duration: int | None = None,
    reversible: bool = False,
    kind: str = "admission",
    relation_target: str | None = None,
    priority: int = 0,
    cause_visibility: str = "public",
    actor: object = None,
) -> dict:
    terminal = kind != "admission"
    return build_world_event_record(
        event_id=event_id,
        world_id=world_id,
        session_id=SESSION,
        branch_id=branch_id,
        turn=game_time,
        game_time=game_time,
        cause_id=f"creator:{event_id}",
        cause_authority="creator",
        cause_visibility=cause_visibility,
        actor=actor,
        affected_domains=[] if terminal else ["world"],
        effects=[] if terminal else [{
            "adapter": "world.circumstance/1",
            "domain": "world",
            "subject": "world",
            "field": "circumstance",
            "value": description,
            "supported": True,
            "lore": "",
        }],
        description=description,
        start=start,
        duration=duration,
        reversible=reversible,
        kind=kind,
        relation_target=relation_target,
        priority=priority,
    )


def test_relevance_outweighs_recency_across_typed_record_families() -> None:
    unrelated_id, unrelated = _fact(
        "fact.weather", "Rain fell over the distant western coast", turn=99
    )
    state = {
        "meta": {"turn": 100},
        "claims": [_claim("claim.gate", "The eastern gate is open", turn=2)],
        "facts": {unrelated_id: unrelated},
    }

    selected = select_knowledge(state, query="eastern gate", limit=1)

    assert [row["id"] for row in selected["claims"]] == ["claim.gate"]
    assert selected["facts"] == []


def test_opposite_claims_keep_distinct_assertions_in_one_neutral_conflict_group() -> None:
    positive = "The eastern gate is open"
    negative = "The eastern gate is not open"
    neutral_id = proposition_id(positive)
    assert proposition_id(negative) == neutral_id
    state = {
        "claims": [
            _claim(
                "claim.gate.positive",
                positive,
                turn=1,
                claim_proposition_id="claim-proposition:positive",
            ),
            _claim(
                "claim.gate.negative",
                negative,
                turn=2,
                claim_proposition_id="claim-proposition:negative",
            ),
        ],
    }

    rows = select_knowledge(state, query="eastern gate", limit=8)["claims"]

    assert {row["proposition_identity"] for row in rows} == {neutral_id}
    assert {row["claim_proposition_id"] for row in rows} == {
        "claim-proposition:positive",
        "claim-proposition:negative",
    }
    assert {row["proposition_polarity"] for row in rows} == {"positive", "negative"}


def test_fact_and_belief_projection_preserves_polarity_separate_from_identity() -> None:
    fact_id, fact = _fact("fact.gate", "The eastern gate is open", turn=1)
    belief_key, belief = _belief(
        "belief.gate", "player", "The eastern gate is not open", turn=2, stance="doubts"
    )
    state = {"facts": {fact_id: fact}, "beliefs": {belief_key: belief}}

    selected = select_knowledge(state, audience="player", actor_id="player", limit=8)

    assert selected["facts"][0]["proposition_id"] != selected["epistemics"][0][
        "proposition_id"
    ]
    assert selected["facts"][0]["proposition_identity"] == selected["epistemics"][0][
        "proposition_identity"
    ]
    assert selected["facts"][0]["proposition_polarity"] == "positive"
    assert selected["epistemics"][0]["proposition_polarity"] == "negative"


def test_legacy_neutral_fact_and_belief_ids_keep_their_record_ids_but_project_safe_joins() -> None:
    positive = "The eastern gate is open"
    negative = "The eastern gate is not open"
    neutral_id = proposition_id(positive)
    fact_id, fact = _fact("fact.legacy", positive, turn=1)
    belief_key, belief = _belief("belief.legacy", "player", negative, turn=2)
    for row in (fact, belief):
        row["proposition_id"] = neutral_id
        row.pop("proposition_identity", None)
    state = {"facts": {fact_id: fact}, "beliefs": {belief_key: belief}}

    selected = select_knowledge(state, audience="player", actor_id="player", limit=8)
    fact_view = selected["facts"][0]
    belief_view = selected["epistemics"][0]

    assert fact_view["record_proposition_id"] == neutral_id
    assert belief_view["record_proposition_id"] == neutral_id
    assert fact_view["proposition_id"] == polarized_proposition_id(positive)
    assert belief_view["proposition_id"] == polarized_proposition_id(negative)
    assert fact_view["proposition_id"] != belief_view["proposition_id"]
    assert fact_view["proposition_identity"] == belief_view["proposition_identity"] == neutral_id


def test_actor_scoped_beliefs_do_not_leak_between_player_and_npc() -> None:
    player_key, player_belief = _belief(
        "belief.player", "player", "The bell is a warning", turn=3
    )
    mara_key, mara_belief = _belief(
        "belief.mara", "mara", "The bell is a decoy", turn=4
    )
    state = {"beliefs": {player_key: player_belief, mara_key: mara_belief}}

    player_view = select_knowledge(state, audience="player", actor_id="player", limit=8)
    mara_view = select_knowledge(state, audience="player", actor_id="mara", limit=8)
    narrator_for_player = select_knowledge(
        state, audience="narrator", actor_id="player", limit=8
    )

    assert [row["holder"] for row in player_view["epistemics"]] == ["player"]
    assert [row["holder"] for row in mara_view["epistemics"]] == ["mara"]
    assert [row["holder"] for row in narrator_for_player["epistemics"]] == ["player"]


def test_actor_scoped_claims_do_not_leak_to_an_unscoped_player() -> None:
    claim = _claim(
        "claim.mara.private",
        "The bell is a decoy",
        turn=4,
        speaker="mara",
        addressee=None,
        visibility="actor_scoped",
    )
    state = {"claims": [claim]}

    player_view = select_knowledge(state, audience="player", actor_id="player", limit=8)
    mara_view = select_knowledge(state, audience="player", actor_id="mara", limit=8)

    assert player_view["claims"] == []
    assert [row["id"] for row in mara_view["claims"]] == ["claim.mara.private"]


def test_actor_scope_normalization_matches_stable_ids_and_rejects_forged_typed_refs() -> None:
    event = _event(
        "event.actor-ref", "The harbor watch changed.",
        cause_visibility="actor_scoped", actor="mara",
    )
    typed_actor = event["actor"]
    forged_actor = deepcopy(typed_actor)
    forged_actor["id"] = "player"

    assert normalize_actor_id(" NPC:MARA ") == "mara"
    assert normalize_actor_id(typed_actor, typed_only=True) == "mara"
    assert normalize_actor_id(forged_actor, typed_only=True) is None
    assert normalize_actor_scope(["actor:Mara", typed_actor, 7]) == frozenset({"mara"})

    claim = _claim(
        "claim.mara.typed-scope", "The watch changed", turn=4,
        speaker="mara", addressee=None, visibility="actor_scoped",
    )
    claim["scoped_actors"] = ["npc:MARA"]
    belief_key, belief = _belief(
        "belief.mara.typed-scope", "actor:Mara", "The watch changed", turn=4,
    )
    state = {
        "world_identity": {"world_id": WORLD},
        "clock": {"minutes": 0},
        "claims": [claim],
        "beliefs": {belief_key: belief},
        "world_events": [event],
    }

    mara = select_knowledge(state, audience="player", actor_id="actor:mara", limit=8)
    player = select_knowledge(state, audience="player", actor_id="player", limit=8)

    assert [row["id"] for row in mara["claims"]] == ["claim.mara.typed-scope"]
    assert [row["id"] for row in mara["epistemics"]] == ["belief.mara.typed-scope"]
    assert player["claims"] == [] and player["epistemics"] == []
    assert mara["events"][0]["cause_visible"] is True
    assert player["events"][0]["cause_visible"] is False
    assert player["events"][0]["cause"] is None


def test_live_fact_and_belief_records_receive_world_scope_and_store_view_scope() -> None:
    cfg = Config()
    cfg.specialization.name = "rpg"
    store = Store(":memory:")
    session_id, branch_id = store.create_session(external_id="knowledge-live-scope")
    seeded = apply_delta(
        store,
        session_id,
        branch_id,
        0,
        [{"op": "world_identity_set", "world_id": WORLD}],
        "user",
        cfg,
    )
    assert seeded.applied and not seeded.quarantined
    admitted = apply_delta(
        store,
        session_id,
        branch_id,
        1,
        [
            {
                "op": "fact_admit",
                "statement": "The cistern is under the refuge",
                "cause": "creator:test:cistern",
                "authority": "creator",
            },
            {
                "op": "belief_acquire",
                "holder": "player",
                "statement": "The eastern report may be false",
                "stance": "doubts",
                "evidence_source": "Ryn's report",
            },
        ],
        "user",
        cfg,
    )
    assert len(admitted.applied) == 2 and not admitted.quarantined

    state = current_state(store, branch_id)

    assert state["knowledge_record_scope"] == {
        "session_id": session_id,
        "branch_id": branch_id,
        "source_branch_ids": [],
    }
    assert {row["world_id"] for row in state["facts"].values()} == {WORLD}
    assert {row["world_id"] for row in state["beliefs"].values()} == {WORLD}


def test_claim_fact_and_belief_retrieval_rejects_wrong_record_scope() -> None:
    current_claim = _claim("claim.current", "Current claim", turn=1)
    current_fact_id, current_fact = _fact("fact.current", "Current fact", turn=1)
    current_belief_key, current_belief = _belief(
        "belief.current", "player", "Current belief", turn=1
    )
    parent_claim = _claim(
        "claim.parent", "Inherited claim", turn=0,
        branch_id="branch-parent", session_id="parent-session",
    )
    parent_fact_id, parent_fact = _fact(
        "fact.parent", "Inherited fact", turn=0,
        branch_id="branch-parent", session_id="parent-session",
    )
    parent_belief_key, parent_belief = _belief(
        "belief.parent", "player", "Inherited belief", turn=0,
        branch_id="branch-parent", session_id="parent-session",
    )
    wrong_claims = [
        _claim("claim.wrong-world", "Wrong world claim", turn=2, world_id=OTHER_WORLD),
        _claim("claim.wrong-session", "Wrong session claim", turn=2, session_id="other-session"),
        _claim("claim.wrong-branch", "Wrong branch claim", turn=2, branch_id="branch-sibling"),
    ]
    wrong_facts = dict([
        _fact("fact.wrong-world", "Wrong world fact", turn=2, world_id=OTHER_WORLD),
        _fact("fact.wrong-session", "Wrong session fact", turn=2, session_id="other-session"),
        _fact("fact.wrong-branch", "Wrong branch fact", turn=2, branch_id="branch-sibling"),
    ])
    wrong_beliefs = dict([
        _belief("belief.wrong-world", "player", "Wrong world belief", turn=2, world_id=OTHER_WORLD),
        _belief("belief.wrong-session", "player", "Wrong session belief", turn=2, session_id="other-session"),
        _belief("belief.wrong-branch", "player", "Wrong branch belief", turn=2, branch_id="branch-sibling"),
    ])
    state = {
        "world_identity": {"world_id": WORLD},
        "knowledge_record_scope": {
            "session_id": SESSION,
            "branch_id": BRANCH,
            "source_branch_ids": ["branch-parent"],
        },
        "claims": [current_claim, parent_claim, *wrong_claims],
        "facts": {
            current_fact_id: current_fact, parent_fact_id: parent_fact, **wrong_facts,
        },
        "beliefs": {
            current_belief_key: current_belief,
            parent_belief_key: parent_belief,
            **wrong_beliefs,
        },
    }

    selected = select_knowledge(state, audience="player", actor_id="player", limit=64)

    assert [row["id"] for row in selected["claims"]] == [
        "claim.current", "claim.parent",
    ]
    assert [row["id"] for row in selected["facts"]] == [
        "fact.current", "fact.parent",
    ]
    assert [row["id"] for row in selected["epistemics"]] == [
        "belief.current", "belief.parent",
    ]


def test_event_projection_excludes_cross_world_and_cross_branch_records() -> None:
    cross_world = _event(
        "event.cross-world", "Cross-world secret", world_id=OTHER_WORLD
    )
    cross_branch = _event(
        "event.cross-branch", "Sibling-branch secret", branch_id="branch-sibling"
    )
    current = _event("event.current", "Current-branch weather")
    state = {
        "world_identity": {"world_id": WORLD},
        "clock": {"minutes": 0},
        # The current record is last, matching project_state_overlay's branch-owned view.
        "world_events": [cross_world, cross_branch, current],
    }

    rows = select_knowledge(state, audience="player", actor_id="player", limit=8)["events"]

    assert [row["id"] for row in rows] == ["event.current"]


def test_hidden_event_cause_is_redacted_from_rows_and_prompt_text() -> None:
    hidden = _event(
        "event.hidden-cause",
        "The harbor gates closed",
        cause_visibility="hidden",
    )
    state = {
        "world_identity": {"world_id": WORLD},
        "clock": {"minutes": 0},
        "world_events": [hidden],
    }

    row = select_knowledge(state, audience="player", actor_id="player")["events"][0]
    rendered = render_knowledge_context(
        state, audience="player", actor_id="player", query="harbor"
    )

    assert row["cause"] is None
    assert row["cause_visible"] is False
    assert hidden["cause_id"] not in rendered
    assert "cause=not-visible" in rendered


def test_event_history_reports_expiry_supersession_and_terminal_conflict() -> None:
    admission = _event(
        "event.fog",
        "Fog covers the harbor",
        duration=10,
        reversible=True,
    )
    expiry = _event(
        "event.fog.expiry",
        "",
        game_time=3,
        start=3,
        kind="expiry",
        relation_target="event.fog",
        priority=2,
    )
    supersession = _event(
        "event.fog.supersession",
        "",
        game_time=3,
        start=3,
        kind="supersession",
        relation_target="event.fog",
        priority=3,
    )
    state = {
        "world_identity": {"world_id": WORLD},
        "clock": {"minutes": 4},
        "world_events": [admission, expiry, supersession],
    }

    current = select_knowledge(state, audience="player", actor_id="player", limit=8)
    history = select_knowledge(
        state, audience="player", actor_id="player", limit=8, include_history=True
    )
    statuses = {row["id"]: row["status"] for row in history["events"]}

    assert current["events"] == []
    assert statuses == {
        "event.fog": "supersession",
        "event.fog.expiry": "terminal_conflict_lost",
        "event.fog.supersession": "winning_terminal",
    }


def test_rendered_prompt_is_bounded_and_never_dumps_raw_conversation() -> None:
    facts = dict(
        _fact(
            f"fact.{index}",
            f"Typed fact {index} " + "carefully bounded detail " * 16,
            turn=index,
        )
        for index in range(12)
    )
    raw_secret = "RAW-CONVERSATION-MUST-NEVER-ENTER-TYPED-KNOWLEDGE"
    state = {
        "meta": {"turn": 12},
        "facts": facts,
        "messages": [{"role": "user", "content": raw_secret}],
        "conversation": raw_secret,
        "memories": [{"text": raw_secret, "turn": 12}],
    }

    rendered = render_knowledge_context(
        state,
        audience="narrator",
        actor_id="player",
        query="Typed fact",
        limit=10,
        char_cap=300,
    )

    assert rendered.startswith("[KNOWLEDGE")
    assert len(rendered) <= 300
    assert raw_secret not in rendered
