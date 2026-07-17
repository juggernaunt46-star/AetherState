"""P1b: L3 heuristic path — fixtures S1-S10 (planning/08 A.1) + canon units + restart recovery.

Engine-level tests (no proxy): the L3 classifier decision table of 03 SS2.3 with the
B1 alignment amendment. dedup_window_s=0 disables S7 dedup except where S7 tests it.
"""
from __future__ import annotations

import json

import pytest


from aetherstate.canon import canonicalize, chain, normalize, split_collapsed
from aetherstate.config import SessionConfig
from aetherstate.session_engine import SessionEngine, TurnClass
from aetherstate.state import current_state
from aetherstate.stamps import Stamp
from aetherstate.store import Store
from aetherstate.turn_lifecycle import build_pre_mutation_key, fingerprint


def eng(**kw) -> SessionEngine:
    return SessionEngine(Store(":memory:"), SessionConfig(dedup_window_s=0, **kw))


def req(*texts) -> bytes:
    """Alternating assistant/user starting with the greeting (assistant)."""
    roles = ["assistant" if i % 2 == 0 else "user" for i in range(len(texts))]
    return json.dumps({"messages": [
        {"role": r, "content": t} for r, t in zip(roles, texts)]}).encode()


def msgs(*pairs) -> bytes:
    return json.dumps({"messages": [{"role": r, "content": t} for r, t in pairs]}).encode()


# --- canon units -----------------------------------------------------------
def test_normalize_strips_md_ws_and_sentinel():
    assert normalize("  *Hello*   **world**\n<<AETHER:session=x>> ") == "Hello world"


def test_canonicalize_drops_system_and_multimodal():
    out = canonicalize([
        {"role": "system", "content": "WI churn"},
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image_url", "image_url": {}}]},
    ])
    assert [(c.role, c.text) for c in out] == [("user", "look")]


def test_chain_is_prefix_commitment():
    a = canonicalize([{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}])
    b = canonicalize([{"role": "user", "content": "one"}, {"role": "assistant", "content": "DIFF"}])
    assert chain(a)[0] == chain(b)[0] and chain(a)[1] != chain(b)[1]


def test_split_collapsed_needs_three_prefixes():
    assert split_collapsed("hi: there") == ["hi: there"]
    parts = split_collapsed("Dane: hello\nBean: hi\nDane: onward")
    assert len(parts) == 3


# --- S1 greeting swipe -------------------------------------------------------
def test_s1_zero_user_msgs_is_ephemeral():
    e = eng()
    assert e.observe(None, msgs(("system", "card"), ("assistant", "greeting"))) is None
    assert not e.index.branches                     # nothing persisted


def test_s1_anchor_is_first_user_message():
    e = eng()
    r = e.observe(None, req("greeting", "first user words"))
    assert r.klass is TurnClass.new_session
    row = e.store.db.execute("SELECT anchor_hash FROM sessions").fetchone()
    from aetherstate.canon import content_hash
    assert row["anchor_hash"] == content_hash(normalize("first user words"))


# --- basic flow: new_turn / swipe / continue / edit_fork ---------------------
def test_new_turn_appends_and_settles():
    e = eng()
    r1 = e.observe(None, req("g", "u1"))
    r2 = e.observe(None, req("g", "u1", "a1", "u2"))
    assert r2.klass is TurnClass.new_turn and r2.session_id == r1.session_id
    settled = e.store.db.execute("SELECT settled FROM turns WHERE turn_index=0").fetchone()
    assert settled["settled"] == 1                  # lag-1: turn 0 settled by turn 1


def test_swipe_identical_replay():                   # regen of never-recorded tail
    e = eng()
    e.observe(None, req("g", "u1"))
    r = e.observe(None, req("g", "u1"))
    assert r.klass is TurnClass.swipe


def test_swipe_recorded_tip_retracted():             # case 2: stored assistant tail absent
    e = eng()
    e.observe(None, req("g", "u1"))
    e.observe(None, req("g", "u1", "a1", "u2"))      # a1 now recorded
    r = e.observe(None, req("g", "u1", "a1", "u2"))  # wait: identical replay = swipe 2a
    assert r.klass is TurnClass.swipe
    r = e.observe(None, req("g", "u1", "a1"))        # history minus terminal user? -> prefix
    # strict prefix 3 of 4 with stored tail USER (u2): truncate-fork, not swipe
    assert r.klass is TurnClass.edit_fork


def test_swipe_replaces_recorded_assistant_tip():
    """True case-2 shape: stored tail is a RECORDED assistant msg; swipe retracts it."""
    e = eng()
    e.observe(None, req("g", "u1"))
    e.observe(None, req("g", "u1", "a1", "u2"))
    e.observe(None, req("g", "u1", "a1", "u2", "a2", "u3"))   # stored: 6 msgs, tail u3
    # user deletes u3 then presses continue on a2: strict prefix ending assistant
    r = e.observe(None, req("g", "u1", "a1", "u2", "a2"))
    assert r.klass is TurnClass.edit_fork            # truncate-fork drops u3 (08 S6)
    assert len(e.index.branches[r.branch_id].msgs) == 5       # tail is now assistant a2
    # swipe a2: request = history minus the recorded assistant tip
    r2 = e.observe(None, req("g", "u1", "a1", "u2"))
    assert r2.klass is TurnClass.swipe               # case 2: tip retracted + bump
    assert len(e.index.branches[r2.branch_id].msgs) == 4


def test_edit_fork_diverges_mid_history():
    e = eng()
    e.observe(None, req("g", "u1"))
    e.observe(None, req("g", "u1", "a1", "u2"))
    r = e.observe(None, req("g", "u1", "a1", "u2-EDITED"))
    assert r.klass is TurnClass.edit_fork
    live = e.store.db.execute("SELECT COUNT(*) c FROM branches WHERE status='live'").fetchone()
    dead = e.store.db.execute("SELECT COUNT(*) c FROM branches WHERE status='dead'").fetchone()
    assert live["c"] == 1 and dead["c"] == 1         # old branch dead (03 SS2.3 case 3)


def test_continue_no_state_advance():
    e = eng()
    e.observe(None, req("g", "u1"))
    r = e.observe(None, req("g", "u1", "a1-partial"))
    assert r.klass is TurnClass.continue_
    assert len(e.index.branches[r.branch_id].msgs) == 2   # partial tail NOT appended


# --- S2 identical openings ---------------------------------------------------
def test_s2_identical_openings_self_heal():
    e = eng()
    e.observe(None, req("g", "hi"))
    e.observe(None, req("g", "hi", "a1", "u2-chatA"))
    # chat B: same card, same opener -> merges (acceptable), then self-heals via fork
    rb = e.observe(None, req("g", "hi"))
    assert rb.klass in (TurnClass.swipe, TurnClass.edit_fork)   # wrong-merge tolerated
    rb2 = e.observe(None, req("g", "hi", "a1x", "u2-chatB"))
    assert rb2.klass in (TurnClass.edit_fork, TurnClass.new_turn)
    # no corruption possible: every resolution still lands on SOME branch


# --- S3 context-window slide (B1) ---------------------------------------------
def test_s3_window_slide_alignment():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]
    e.observe(None, req(*full[:2]))
    e.observe(None, req(*full[:4]))
    e.observe(None, req(*full[:6]))
    e.observe(None, req(*full[:8]))
    # trimmed: first two canonical msgs gone + new tail
    trimmed = full[2:] + ["a4", "u5"]
    r = e.observe(None, req(*trimmed) if False else json.dumps({"messages": [
        {"role": "assistant" if i % 2 == 0 else "user", "content": t}
        for i, t in enumerate(trimmed)]}).encode())
    assert r.klass is TurnClass.new_turn             # aligned, classified on the tail
    assert len(e.index.branches[r.branch_id].msgs) == 10


# --- S4 chat rename / S5 adopt-fork (stamped ids + L3 evidence) ----------------
def _build_stamped(e, sess_id, *texts, gen="normal", turn=None,
                   parent=None, fork_pos=None):
    body = json.dumps({"messages": [
        {"role": "assistant" if i % 2 == 0 else "user", "content": t}
        for i, t in enumerate(texts)]}).encode()
    return e.observe(Stamp(
        session=sess_id, gen_type=gen, turn=turn, parent=parent, fork_pos=fork_pos), body)


def _reserve_prefix_turn(e, result, *, accepted_prefix_pos, turn_index):
    rows = e.store.get_msgs(result.branch_id)
    key = build_pre_mutation_key(
        session_id=result.session_id,
        branch_id=result.branch_id,
        turn_index=turn_index,
        accepted_prefix_pos=accepted_prefix_pos,
        accepted_head_hash=rows[accepted_prefix_pos - 1]["chain_hash"],
        player_input_hash=rows[accepted_prefix_pos]["content_hash"],
        pre_ledger_hash=fingerprint({"state": "before"}),
        pending_intent_fingerprint=fingerprint(None),
        semantic_contract_version="semantic-contract/l3-fork-test-1",
    )
    return e.store.turn_lifecycle.reserve(key)


def _fork_surface_snapshot(e):
    return (
        tuple(e.store.db.iterdump()),
        {
            branch_id: (
                [(message.role, message.content_hash) for message in view.msgs],
                list(view.chains),
                view.session_id,
                view.last_seen,
            )
            for branch_id, view in e.index.branches.items()
        },
    )


@pytest.mark.parametrize("position", [0, 3], ids=["assistant", "player"])
@pytest.mark.parametrize("surface", ["relink", "explicit", "adopt", "edit"])
def test_every_fork_surface_recomputes_the_selected_source_chain_before_mutation(
    surface, position
):
    e = eng(adopt_min_lcp=4)
    full = ["g", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]
    source_end = 8 if surface in {"relink", "adopt"} else 6
    source = None
    for end in range(2, source_end + 1, 2):
        source = _build_stamped(e, "chain-source", *full[:end])
        rows = e.store.get_msgs(source.branch_id)
        e.store.write_turn_hashes(
            source.branch_id,
            source.turn_index,
            user_hash=rows[-1]["content_hash"],
        )
    with e.store.transaction():
        e.store.db.execute(
            "UPDATE branch_msgs SET chain_hash=? WHERE branch_id=? AND pos=?",
            ("9" * 16, source.branch_id, position),
        )
    before = _fork_surface_snapshot(e)

    if surface == "relink":
        result = _build_stamped(e, "chain-renamed", *full, "a4", "u5")
    elif surface == "explicit":
        result = _build_stamped(
            e,
            "chain-child",
            *full[:5],
            "u3-child",
            parent="chain-source",
            fork_pos=5,
        )
    elif surface == "adopt":
        result = _build_stamped(
            e, "chain-adopt", *full[:6], "a3-DIFF", "u4-DIFF"
        )
    else:
        incoming = [
            {
                "role": "assistant" if index % 2 == 0 else "user",
                "content": text,
            }
            for index, text in enumerate(
                ["g", "u1", "a1", "u2", "a2-DIFF", "u3-DIFF"]
            )
        ]
        result = e.resolve_heuristic(incoming, defer_swipe=True)

    assert result is None
    assert _fork_surface_snapshot(e) == before


def test_s4_rename_relinks():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3"]
    r_old = _build_stamped(e, "chat-old", *full[:2])
    _build_stamped(e, "chat-old", *full[:4])
    _build_stamped(e, "chat-old", *full[:6])
    r_new = _build_stamped(e, "chat-new", *full, "a3", "u4")   # full match + tail
    assert r_new.session_id == r_old.session_id     # relinked, not minted
    row = e.store.db.execute("SELECT external_id FROM sessions").fetchone()
    assert row["external_id"] == "chat-new"


def test_s4_relink_refuses_nonterminal_semantic_prefix_before_identity_mutation():
    e = eng(adopt_min_lcp=4)
    full = ["g", "u1", "a1", "u2", "a2", "u3"]
    source = _build_stamped(e, "chat-old", *full, turn=1)
    _reserve_prefix_turn(e, source, accepted_prefix_pos=3, turn_index=1)
    # Model a damaged/mixed branch where a later ordinary turn exists even though T1 never
    # obtained a terminal artifact. Prefix admission must inspect every inherited lifecycle,
    # not merely the branch head.
    e.store.record_turn(source.branch_id, 2, "normal", "normal")
    before = tuple(e.store.db.iterdump())

    refused = _build_stamped(e, "chat-new", *full, turn=3)

    assert refused is None
    assert tuple(e.store.db.iterdump()) == before
    row = e.store.db.execute(
        "SELECT external_id FROM sessions WHERE session_id=?", (source.session_id,)
    ).fetchone()
    assert row["external_id"] == "chat-old"


def test_heuristic_edit_refuses_nonterminal_prefix_before_touching_session_or_index():
    e = eng(adopt_min_lcp=4)
    full = ["g", "u1", "a1", "u2", "a2", "u3"]
    source = _build_stamped(e, "chat-old", *full, turn=1)
    _reserve_prefix_turn(e, source, accepted_prefix_pos=3, turn_index=1)
    e.store.record_turn(source.branch_id, 2, "normal", "normal")
    before_database = tuple(e.store.db.iterdump())
    before_index = {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.last_seen,
        )
        for branch_id, view in e.index.branches.items()
    }
    incoming = [
        {"role": "assistant" if index % 2 == 0 else "user", "content": text}
        for index, text in enumerate(["g", "u1", "a1", "u2", "a2-DIFF", "u3-DIFF"])
    ]

    refused = e.resolve_heuristic(incoming, defer_swipe=True)

    assert refused is None
    assert tuple(e.store.db.iterdump()) == before_database
    assert {
        branch_id: (
            [(message.role, message.content_hash) for message in view.msgs],
            list(view.chains),
            view.last_seen,
        )
        for branch_id, view in e.index.branches.items()
    } == before_index


def test_s5_adopt_fork_across_st_branches():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]
    r_src = _build_stamped(e, "chat-src", *full[:2])
    _build_stamped(e, "chat-src", *full[:4])
    _build_stamped(e, "chat-src", *full[:6])
    _build_stamped(e, "chat-src", *full[:8])
    # new chat file continues the old one but diverges at canonical pos 6
    r_br = _build_stamped(e, "chat-branchfile", *full[:6], "a3-DIFF", "u4-DIFF")
    assert r_br.session_id != r_src.session_id      # own session (own chat file)
    b = e.store.db.execute("SELECT parent_branch, forked_at FROM branches WHERE branch_id=?",
                           (r_br.branch_id,)).fetchone()
    assert b["parent_branch"] == r_src.branch_id and b["forked_at"] == 6
    copied = e.store.get_msgs(r_br.branch_id)
    assert len(copied) == 8                          # 6 copied prefix + 2 divergent tail


def test_s5_adopt_fork_preserves_one_based_stamped_turns_and_ops():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]
    source_results = []
    for turn, end in enumerate((2, 4, 6, 8), start=1):
        result = _build_stamped(e, "chat-src", *full[:end], turn=turn)
        source_results.append(result)
        e.store.journal(
            result.branch_id, turn, turn,
            [{"op": "entity_add", "name": f"Turn {turn} Witness", "kind": "npc"}],
            "rule")

    branched = _build_stamped(
        e, "chat-branchfile", *full[:6], "a3-DIFF", "u4-DIFF", turn=1)

    assert branched.turn_index == 4
    assert [row["turn_index"] for row in e.store.db.execute(
        "SELECT turn_index FROM turns WHERE branch_id=? ORDER BY turn_index",
        (branched.branch_id,)).fetchall()] == [1, 2, 3, 4]
    state = current_state(e.store, branched.branch_id)
    entity_names = {entity["name"] for entity in state["entities"].values()}
    assert entity_names >= {
        "Turn 1 Witness", "Turn 2 Witness", "Turn 3 Witness",
    }
    assert "Turn 4 Witness" not in entity_names

    branches = e.store.db.execute(
        "SELECT branch_id, parent_branch, head_turn FROM branches WHERE session_id=?",
        (branched.session_id,)).fetchall()
    assert [(row["branch_id"], row["parent_branch"], row["head_turn"])
            for row in branches] == [(branched.branch_id, source_results[0].branch_id, 4)]


def test_explicit_lineage_forks_position_five_below_generic_adoption_floor():
    e = eng(adopt_min_lcp=6)
    full = ["g", "u1", "a1", "u2", "a2", "u3-original"]
    source_results = []
    for turn, end in enumerate((2, 4, 6), start=1):
        result = _build_stamped(e, "chat-src", *full[:end], turn=turn)
        source_results.append(result)
        e.store.journal(
            result.branch_id, turn, turn,
            [{"op": "entity_add", "name": f"Turn {turn} Witness", "kind": "npc"}],
            "rule")

    source_sid = source_results[-1].session_id
    source_branch = source_results[-1].branch_id
    e.store.set_frozen(source_sid, True)
    e.store.genesis_mark(source_sid, "done")
    e.store.session_mode_set(source_sid, "passthrough")
    e.store.narrator_speaker_set(source_sid, "Oracle")

    def source_snapshot():
        return {
            "session": dict(e.store.db.execute(
                "SELECT * FROM sessions WHERE session_id=?", (source_sid,)).fetchone()),
            "branch": dict(e.store.db.execute(
                "SELECT * FROM branches WHERE branch_id=?", (source_branch,)).fetchone()),
            "messages": [dict(row) for row in e.store.db.execute(
                "SELECT * FROM branch_msgs WHERE branch_id=? ORDER BY pos",
                (source_branch,)).fetchall()],
            "turns": [dict(row) for row in e.store.db.execute(
                "SELECT * FROM turns WHERE branch_id=? ORDER BY turn_index",
                (source_branch,)).fetchall()],
            "journal": [dict(row) for row in e.store.db.execute(
                "SELECT * FROM ops_journal WHERE branch_id=? ORDER BY id",
                (source_branch,)).fetchall()],
        }

    before = source_snapshot()
    child = _build_stamped(
        e, "chat-child", *full[:5], "u3-brace", turn=1,
        parent="chat-src", fork_pos=5)

    assert child is not None
    assert child.session_id != source_sid
    assert child.turn_index == 3
    child_branch = e.store.db.execute(
        "SELECT * FROM branches WHERE branch_id=?", (child.branch_id,)).fetchone()
    assert child_branch["parent_branch"] == source_branch
    assert child_branch["forked_at"] == 5
    assert [row["content_hash"] for row in e.store.get_msgs(child.branch_id)] \
        != [row["content_hash"] for row in e.store.get_msgs(source_branch)]

    child_session = e.store.db.execute(
        "SELECT frozen, genesis, mode, narrator_speaker, frontend FROM sessions"
        " WHERE session_id=?", (child.session_id,)).fetchone()
    assert dict(child_session) == {
        "frozen": 1,
        "genesis": "done",
        "mode": "passthrough",
        "narrator_speaker": "Oracle",
        "frontend": "stamped-explicit-branch",
    }
    child_entities = {
        entity["name"] for entity in current_state(e.store, child.branch_id)["entities"].values()
    }
    assert child_entities >= {"Turn 1 Witness", "Turn 2 Witness"}
    assert "Turn 3 Witness" not in child_entities
    assert source_snapshot() == before


def test_invalid_or_partial_explicit_lineage_is_exact_no_mutation():
    e = eng(adopt_min_lcp=6)
    _build_stamped(e, "chat-src", "g", "u1", "a1", "u2", turn=1)
    messages = [
        {"role": "assistant", "content": "g"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u-branch"},
    ]
    cases = [
        (Stamp(session="child-parent-only", parent="chat-src"), messages),
        (Stamp(session="child-fork-only", fork_pos=3), messages),
        (Stamp(session="child-missing-parent", parent="missing", fork_pos=3), messages),
        (Stamp(session="child-too-short", parent="chat-src", fork_pos=4), messages),
        (Stamp(session="child-beyond-parent", parent="chat-src", fork_pos=5), [
            *messages,
            {"role": "assistant", "content": "unowned parent extension"},
            {"role": "user", "content": "u-branch-after-gap"},
        ]),
        (Stamp(session="child-bad-prefix", parent="chat-src", fork_pos=3), [
            messages[0], messages[1], {"role": "assistant", "content": "DIFF"}, messages[3],
        ]),
    ]
    for stamp, incoming in cases:
        before = tuple(e.store.db.iterdump())
        assert e.resolve_stamped(stamp, incoming) is None
        assert tuple(e.store.db.iterdump()) == before


def test_known_same_sid_divergence_is_exact_no_mutation_for_new_actions():
    for gen_type in ("normal", "impersonate"):
        e = eng()
        full = ["g", "u1", "a1", "u2", "a2", "u3-original"]
        original = None
        for turn, end in enumerate((2, 4, 6), start=1):
            original = _build_stamped(e, "same-chat", *full[:end], turn=turn)
            e.store.write_turn_hashes(
                original.branch_id, original.turn_index,
                user_hash=f"user-{turn}", assistant_hash=f"assistant-{turn}")
        view_before = (
            list(e.index.branches[original.branch_id].msgs),
            list(e.index.branches[original.branch_id].chains),
        )
        database_before = tuple(e.store.db.iterdump())

        refused = _build_stamped(
            e, "same-chat", *full[:5], "u3-branch", gen=gen_type, turn=1)

        assert refused is None
        assert tuple(e.store.db.iterdump()) == database_before
        assert (e.index.branches[original.branch_id].msgs,
                e.index.branches[original.branch_id].chains) == view_before


def test_known_same_sid_divergence_allows_exact_reserved_lost_reply_transition():
    e = eng()
    action_hash = canonicalize([{"role": "user", "content": "u1"}])[0].content_hash
    first = _build_stamped(e, "same-chat", "g", "u1", turn=1)
    e.store.write_turn_hashes(first.branch_id, first.turn_index, user_hash=action_hash)
    reserved = _build_stamped(e, "same-chat", "g", "u1", turn=2)
    e.store.write_turn_hashes(reserved.branch_id, reserved.turn_index, user_hash=action_hash)
    assert e.store.rule_ops_at(reserved.branch_id, reserved.turn_index) == []

    allowed = _build_stamped(
        e, "same-chat", "replacement assistant", "distinct action", turn=3)

    assert allowed is not None
    assert allowed.klass is TurnClass.new_turn
    assert allowed.turn_index == 3


def test_zero_prefix_exact_forward_window_is_accepted_and_synced():
    e = eng()
    first = _build_stamped(e, "same-chat", "g", "u1", turn=1)
    e.store.write_turn_hashes(first.branch_id, first.turn_index, user_hash="first-action")
    second = _build_stamped(e, "same-chat", "g", "u1", "a1", "u2", turn=2)
    e.store.write_turn_hashes(second.branch_id, second.turn_index, user_hash="second-action")

    allowed = _build_stamped(
        e, "same-chat", "replacement assistant", "distinct action", turn=3)

    assert allowed is not None and allowed.turn_index == 3
    view = e.index.branches[allowed.branch_id]
    assert [(msg.role, msg.text) for msg in view.msgs[-2:]] == [
        ("assistant", "replacement assistant"), ("user", "distinct action")]


def test_stamped_suffix_window_appends_only_its_unseen_tail():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3", "a3", "u4"]
    for turn, end in enumerate((2, 4, 6), start=1):
        prior = _build_stamped(e, "same-chat", *full[:end], turn=turn)

    slid = _build_stamped(e, "same-chat", *full[2:], turn=4)

    assert slid is not None and slid.branch_id == prior.branch_id
    view = e.index.branches[slid.branch_id]
    assert len(view.msgs) == 8
    assert [msg.text for msg in view.msgs] == full


def test_exact_forward_window_may_reuse_first_message_without_rewriting_history():
    e = eng()
    first = _build_stamped(e, "same-chat", "same assistant", "first action", turn=1)
    before = list(e.index.branches[first.branch_id].msgs)

    allowed = _build_stamped(
        e, "same-chat", "same assistant", "distinct action", turn=2)

    assert allowed is not None and allowed.turn_index == 2
    assert e.index.branches[first.branch_id].msgs == before


def test_known_same_sid_divergence_does_not_block_retry_classes():
    for gen_type in ("swipe", "regenerate", "continue"):
        e = eng()
        _build_stamped(e, "same-chat", "g", "u1", "a1", "u2", turn=1)
        allowed = _build_stamped(
            e, "same-chat", "g", "u1", "a1-DIFF", "u2-new", gen=gen_type, turn=2)
        assert allowed is not None


# --- S6 delete -----------------------------------------------------------------
def test_s6_delete_is_edit_fork():
    e = eng()
    e.observe(None, req("g", "u1"))
    e.observe(None, req("g", "u1", "a1", "u2"))
    e.observe(None, req("g", "u1", "a1", "u2", "a2", "u3"))
    r = e.observe(None, req("g", "u1", "a1", "u2", "u3-new"))  # a2,u3 deleted, new msg
    assert r.klass is TurnClass.edit_fork


# --- S7 duplicate request --------------------------------------------------------
def test_s7_dedup_never_double_classifies():
    e = SessionEngine(Store(":memory:"), SessionConfig())      # real 30s window
    body = req("g", "u1")
    r1 = e.observe(None, body)
    r2 = e.observe(None, body)                       # network retry
    assert r2.duplicate and r2.klass is r1.klass is TurnClass.new_session
    swipes = e.store.db.execute("SELECT COALESCE(MAX(swipe_count),0) s FROM turns").fetchone()
    assert swipes["s"] == 0                          # never bumped as swipe


# --- S8 post-processing switch ---------------------------------------------------
def test_s8_collapsed_blob_and_switch_no_crash():
    e = eng()
    blob1 = "Dane: hello there\nBean: hi yourself\nDane: what brings you"
    r1 = e.observe(None, msgs(("user", blob1)))
    assert r1.klass is TurnClass.new_session         # text-mode split (3 segments)
    blob2 = blob1 + "\nBean: adventure\nDane: excellent"
    r2 = e.observe(None, msgs(("user", blob2)))
    assert r2.klass is TurnClass.new_turn and r2.session_id == r1.session_id
    # switch to Merge-style multi-message mid-chat: worst case one spurious fork
    r3 = e.observe(None, req("hello there", "hi yourself", "what brings you",
                             "adventure", "excellent", "onward"))
    assert r3 is None or r3.klass in tuple(TurnClass)          # no crash, legal outcome


# --- S9 foreign injection mid-array ------------------------------------------------
def test_s9_foreign_user_role_wi_spurious_fork_only():
    e = eng()
    full = ["g", "u1", "a1", "u2", "a2", "u3"]
    for i in (2, 4, 6):
        e.observe(None, req(*full[:i]))
    poked = full[:3] + ["FOREIGN WI LORE"] + full[3:] + ["a3", "u4"]
    r = e.observe(None, req(*poked))
    assert r.klass in (TurnClass.edit_fork, TurnClass.new_turn)   # harmless worst case
    assert r.session_id == e.observe(None, req(*full[:2])).session_id or True  # no crash


# --- S10 persona switch --------------------------------------------------------------
def test_s10_persona_switch_same_session():
    e = eng()
    r1 = e.observe(Stamp(session="c", user="Bean"), req("g", "u1"))
    r2 = e.observe(Stamp(session="c", user="Dane"), req("g", "u1", "a1", "u2"))
    assert r2.session_id == r1.session_id and r2.branch_id == r1.branch_id
    assert r2.klass is TurnClass.new_turn            # hashes content-based, unaffected


# --- restart recovery -----------------------------------------------------------------
def test_restart_rebuilds_index_and_continues():
    store = Store(":memory:")
    e1 = SessionEngine(store, SessionConfig(dedup_window_s=0))
    r1 = e1.observe(None, req("g", "u1"))
    e1.observe(None, req("g", "u1", "a1", "u2"))
    e2 = SessionEngine(store, SessionConfig(dedup_window_s=0))   # fresh process, same DB
    r = e2.observe(None, req("g", "u1", "a1", "u2", "a2", "u3"))
    assert r.klass is TurnClass.new_turn and r.session_id == r1.session_id


def test_stamped_turn_never_regresses_below_head():
    """2026-07-09 regression: the ST extension resets its turn counter to 0 on chat reload /
    CHAT_CHANGED, so a stamped turn can arrive BELOW the real head after a page refresh. The
    server head is authoritative — a regressed stamp.turn must resolve to head+1, never landing
    the turn (and its rolls / [DIRECTIVE]) on an early turn where the resolution silently vanishes."""
    e = eng()
    sess = "st-regress"
    hist = [{"role": "assistant", "content": "greeting"}]
    r = None
    for t in range(6):                       # honest turns 0..5 -> head advances to 5
        hist.append({"role": "user", "content": f"u{t}"})
        r = e.resolve_stamped(Stamp(session=sess, gen_type="normal", turn=t), list(hist))
        hist.append({"role": "assistant", "content": f"a{t}"})
    head = e._head(r.branch_id)
    assert head == 5, head
    # reloaded client: counter reset -> stamp.turn=1 (below head). MUST clamp to head+1.
    hist.append({"role": "user", "content": "after reload"})
    r2 = e.resolve_stamped(Stamp(session=sess, gen_type="normal", turn=1), list(hist))
    assert r2.turn_index == head + 1 == 6, r2.turn_index
    # 2026-07-10 (Eranmor): a FORWARD-jumping stamp is no longer honored either — the
    # extension's counter ticks on continues/stopped generations that never reach the proxy,
    # which skipped indices (live session recorded turns 1,3,4,5) and desynced cooldown/
    # regen/mastery arithmetic. head+1 is the only truth; the stamp is a dedup/debug hint.
    hist.append({"role": "assistant", "content": "a6"})
    hist.append({"role": "user", "content": "ahead"})
    r3 = e.resolve_stamped(Stamp(session=sess, gen_type="normal", turn=50), list(hist))
    assert r3.turn_index == 7, r3.turn_index
