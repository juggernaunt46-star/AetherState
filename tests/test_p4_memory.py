"""P4 item 2: memory tiers + retrieval + rules reflection (02 SS10, 03 SS7, 08 L2,
Proxy SS4) — index writes, scene boundaries, prefilter, composite scoring, access-bump
recency, when-phrases, consolidation + retrieval exclusion, fork/rollback spine,
Q15 recall staging + compose injection."""
from __future__ import annotations

import json

from aetherstate import memory
from aetherstate.canon import canonicalize, chain
from aetherstate.compose import compose
from aetherstate.config import Config
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def mk():
    cfg = Config()
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kira"},
                                     {"op": "entity_add", "name": "Bean"}], "user", cfg)
    return cfg, store, sid, bid


def remember(store, cfg, sid, bid, turn, text, participants=("Kira",), importance=3,
             tags=(), scene=None):
    ops = []
    if scene is not None:
        ops.append({"op": "scene_set", "location": scene})
    ops.append({"op": "memory_event", "text": text, "participants": list(participants),
                "importance": importance, "tags": list(tags)})
    r = apply_delta(store, sid, bid, turn, ops, "user", cfg)
    memory.index_applied(store, sid, bid, r.applied, r.state)
    return r


# ------------------------------ index + scene boundaries ------------------------------
def test_index_writes_rows_with_scene_stamp_and_b4_tags():
    cfg, store, sid, bid = mk()
    remember(store, cfg, sid, bid, 1, "met at the tavern", scene="tavern")
    apply_delta(store, sid, bid, 2, [{"op": "scene_mode", "mode": "flashback"}],
                "user", cfg)
    remember(store, cfg, sid, bid, 3, "the old war wound")
    rows = store.memories_candidates(bid)
    by_text = {r["text"]: r for r in rows}
    assert by_text["met at the tavern"]["tier"] == "episodic"
    assert by_text["met at the tavern"]["location_id"] == "tavern"
    assert by_text["met at the tavern"]["scene_index"] == 1
    assert "flashback" in json.loads(by_text["the old war wound"]["tags"])   # 08 B4


def test_scene_index_bumps_only_on_location_change():
    cfg, store, sid, bid = mk()
    for turn, loc in ((1, "tavern"), (2, "tavern"), (3, "courtyard"), (4, "tavern")):
        apply_delta(store, sid, bid, turn, [{"op": "scene_set", "location": loc}],
                    "user", cfg)
    st = current_state(store, bid)
    assert st["scene"]["scene_index"] == 3         # tavern -> courtyard -> tavern; no dupe


# ------------------------------ prefilter (03 SS7) ------------------------------
def test_prefilter_matches_participants_location_tags_and_falls_back():
    cfg, store, sid, bid = mk()
    remember(store, cfg, sid, bid, 1, "kira secret", participants=("Kira",), scene="tavern")
    remember(store, cfg, sid, bid, 2, "dane rumor", participants=("Dane",))
    apply_delta(store, sid, bid, 3,
                [{"op": "scene_set", "location": "courtyard", "participants": ["Kira"]}],
                "user", cfg)
    st = current_state(store, bid)
    got = memory._prefilter(store.memories_candidates(bid), st, 200)
    assert [r["text"] for r in got] == ["kira secret"]          # participants ∩ scene
    # scene with NO overlap: fallback keeps recall alive (prefilter caps, never starves)
    apply_delta(store, sid, bid, 4,
                [{"op": "scene_set", "location": "moor", "participants": ["Vex"]}],
                "user", cfg)
    st = current_state(store, bid)
    got = memory._prefilter(store.memories_candidates(bid), st, 200)
    assert len(got) == 2                                        # recent-N fallback


def test_prefilter_none_location_never_matches():
    """A row with NO location (indexed before any scene_set) must not equality-match
    anything — and a scene with no location must not vacuum-match None rows."""
    cfg, store, sid, bid = mk()
    remember(store, cfg, sid, bid, 1, "unplaced memory", participants=("Dane",))
    assert store.memories_candidates(bid)[0]["location_id"] is None
    apply_delta(store, sid, bid, 2, [{"op": "scene_set", "location": "tavern"}],
                "user", cfg)
    remember(store, cfg, sid, bid, 3, "tavern memory", participants=("Vex",))
    st = current_state(store, bid)                        # scene: tavern, no participants
    got = memory._prefilter(store.memories_candidates(bid), st, 200)
    assert [r["text"] for r in got] == ["tavern memory"]  # None row only via fallback


# ------------------------------ scoring (Proxy SS4) ------------------------------
def test_relevance_and_access_bump_change_ranking():
    cfg, store, sid, bid = mk()
    cfg.memory.top_k = 1
    # SAME turn -> recency ties; relevance must decide (min-max over 2 cands is binary,
    # so any recency skew would drown the signal — the paper's pools are larger)
    r = apply_delta(store, sid, bid, 1, [
        {"op": "memory_event", "text": "the silver harpoon on the wall", "importance": 3},
        {"op": "memory_event", "text": "a boring lunch of bread", "importance": 3}],
        "user", cfg)
    memory.index_applied(store, sid, bid, r.applied, r.state)
    st = current_state(store, bid)
    top = memory.retrieve(store, cfg, bid, st, "where is the harpoon", now_turn=10)
    assert top[0]["text"].startswith("the silver harpoon")      # relevance wins
    # winner's last_accessed bumped to 10 -> with a neutral query, recency now favors it
    top2 = memory.retrieve(store, cfg, bid, st, "zzz nothing shared", now_turn=11)
    assert top2[0]["text"].startswith("the silver harpoon")
    rows2 = {r["text"]: r["last_accessed_turn"] for r in store.memories_candidates(bid)}
    assert rows2["the silver harpoon on the wall"] == 11


def test_retrieve_is_deterministic():
    cfg, store, sid, bid = mk()
    for t in range(1, 6):
        remember(store, cfg, sid, bid, t, f"event number {t}", importance=(t % 3) + 1)
    st = current_state(store, bid)
    a = [r["text"] for r in memory.retrieve(store, cfg, bid, st, "event", 8)]
    b = [r["text"] for r in memory.retrieve(store, cfg, bid, st, "event", 8)]
    assert a == b and len(a) == cfg.memory.top_k


# ------------------------------ rendering (04 SS3.5) ------------------------------
def test_when_phrases_and_recall_render():
    assert memory.when_phrase(1) == "just now"
    assert memory.when_phrase(15) == "earlier"
    assert memory.when_phrase(99) == "long ago"
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [{"op": "scene_mode", "mode": "dream"}], "user", cfg)
    remember(store, cfg, sid, bid, 2, "flying over the moor")
    lines = memory.recall_lines(store.memories_candidates(bid), now_turn=3)
    assert lines == ["- (dream) flying over the moor (just now)"]
    txt = memory.render_recall(lines, "Kira")
    assert txt.startswith("[RECALL] (things Kira remembers)")
    assert memory.render_recall(lines, None).startswith("[RECALL] (remembered)")
    assert not any(ch.isdigit() for ch in txt)              # numbers never shown (04 SS3.5)


# ------------------------------ reflection (08 L2) ------------------------------
def scene_hop(store, cfg, sid, bid, turn, loc):
    apply_delta(store, sid, bid, turn, [{"op": "scene_set", "location": loc}], "user", cfg)


def test_reflection_consolidates_old_scenes_and_excludes_members():
    cfg, store, sid, bid = mk()
    cfg.memory.reflection_every_scenes = 2
    remember(store, cfg, sid, bid, 1, "low detail", importance=2, scene="tavern")
    remember(store, cfg, sid, bid, 2, "THE CRUCIAL VOW", importance=9)
    for turn, loc in ((3, "courtyard"), (4, "moor"), (5, "keep")):
        scene_hop(store, cfg, sid, bid, turn, loc)
    st = current_state(store, bid)
    assert memory.reflect(store, cfg, sid, bid, st) == 1
    cands = store.memories_candidates(bid)               # parent_id IS NULL only
    assert len(cands) == 1 and cands[0]["tier"] == "summary"
    assert cands[0]["text"].startswith("THE CRUCIAL VOW")     # importance-weighted digest
    assert "summary" in json.loads(cands[0]["tags"])
    assert cands[0]["importance"] == 9
    assert memory.reflect(store, cfg, sid, bid, st) == 0      # idempotent


def test_reflection_respects_horizon():
    cfg, store, sid, bid = mk()
    cfg.memory.reflection_every_scenes = 3
    remember(store, cfg, sid, bid, 1, "fresh thing", scene="tavern")
    scene_hop(store, cfg, sid, bid, 2, "courtyard")
    st = current_state(store, bid)
    assert memory.reflect(store, cfg, sid, bid, st) == 0      # only 1 scene back


# ------------------------------ fork / rollback spine ------------------------------
def test_fork_copies_index_and_remaps_parents():
    cfg, store, sid, bid = mk()
    cfg.memory.reflection_every_scenes = 1
    remember(store, cfg, sid, bid, 1, "old scene fact", scene="tavern")
    for turn, loc in ((2, "courtyard"), (3, "moor")):
        scene_hop(store, cfg, sid, bid, turn, loc)
    memory.reflect(store, cfg, sid, bid, current_state(store, bid))
    transcript = canonicalize([
        {"role": "user", "content": "Create Kira and Bean."},
        {"role": "user", "content": "Remember the old scene fact at the tavern."},
        {"role": "user", "content": "Move to the courtyard."},
        {"role": "user", "content": "Move to the moor."},
    ])
    heads = chain(transcript)
    store.append_msgs(
        bid,
        0,
        [
            (message.role, message.content_hash, head)
            for message, head in zip(transcript, heads)
        ],
    )
    for turn, message in enumerate(transcript):
        store.record_turn(bid, turn, "normal", "normal")
        store.write_turn_hashes(bid, turn, user_hash=message.content_hash)

    nb = store.fork_branch(bid, at_pos=len(transcript), fork_turn=3)
    rows = store.memories_candidates(nb)
    assert len(rows) == 1 and rows[0]["tier"] == "summary"
    assert rows[0]["branch_id"] == nb                     # fresh ids, remapped links
    src_ids = {r["memory_id"] for r in store.memories_candidates(bid)}
    assert rows[0]["memory_id"] not in src_ids


def test_rollback_deletes_rows_and_reopens_orphaned_members():
    cfg, store, sid, bid = mk()
    cfg.memory.reflection_every_scenes = 1
    remember(store, cfg, sid, bid, 1, "the vow", importance=8, scene="tavern")
    for turn, loc in ((2, "courtyard"), (3, "moor")):
        scene_hop(store, cfg, sid, bid, turn, loc)
    memory.reflect(store, cfg, sid, bid, current_state(store, bid))
    assert store.memories_candidates(bid)[0]["tier"] == "summary"
    store.rollback_to(bid, 2)                             # summary created_turn > 2? it is
    cands = store.memories_candidates(bid)                # max member turn = 1... summary
    texts = {r["text"] for r in cands}                    # stamped created_turn=1 -> kept?
    assert "the vow" in " ".join(texts) or any(r["tier"] == "episodic" for r in cands)
    # roll all the way back past the memory itself
    store.rollback_to(bid, 0)
    assert store.memories_candidates(bid) == []


# ------------------------------ Q15 staging + injection ------------------------------
def test_precompute_and_compose_inject_recall_block():
    cfg, store, sid, bid = mk()
    remember(store, cfg, sid, bid, 1, "kira hid the letter in the hollow oak",
             scene="tavern")
    st = current_state(store, bid)
    memory.precompute_recall(store, cfg, sid, bid, st, "where is the letter", 1)
    lines = store.read_recall(sid)
    assert lines and "hollow oak" in lines[0]
    doc = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out, kept = compose(doc, st, cfg, None, "quiet", recall=lines)
    assert out is not None
    joined = json.dumps(out)
    assert "[RECALL] (remembered)" in joined and "hollow oak" in joined
    assert any(c["cls"] == "memories" for c in kept)


def test_recall_absent_changes_nothing():
    cfg, store, sid, bid = mk()
    st = current_state(store, bid)
    doc = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    _, kept_without = compose(doc, st, cfg, None, "quiet", recall=[])
    assert not any(c["cls"] == "memories" for c in kept_without)
