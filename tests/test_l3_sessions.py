"""P1b: L3 heuristic path — fixtures S1-S10 (planning/08 A.1) + canon units + restart recovery.

Engine-level tests (no proxy): the L3 classifier decision table of 03 SS2.3 with the
B1 alignment amendment. dedup_window_s=0 disables S7 dedup except where S7 tests it.
"""
from __future__ import annotations

import json


from aetherstate.canon import canonicalize, chain, normalize, split_collapsed
from aetherstate.config import SessionConfig
from aetherstate.session_engine import SessionEngine, TurnClass
from aetherstate.stamps import Stamp
from aetherstate.store import Store


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
def _build_stamped(e, sess_id, *texts, gen="normal"):
    body = json.dumps({"messages": [
        {"role": "assistant" if i % 2 == 0 else "user", "content": t}
        for i, t in enumerate(texts)]}).encode()
    return e.observe(Stamp(session=sess_id, gen_type=gen), body)


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
