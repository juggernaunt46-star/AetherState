"""P2 fixtures: header rendering (04 SS3.1-3.2), budget governor (03 SS4), placement (06 B.1)."""
from __future__ import annotations

from aetherstate.compose import Component, compose, govern, render_guard, render_header, splice
from aetherstate.config import Config
from aetherstate.stamps import Stamp
from aetherstate.state import empty_state


def rich_state():
    st = empty_state()
    st["entities"] = {"kira": {"name": "Kira", "present": True},
                      "dane": {"name": "Dane", "present": True}}
    st["scene"] = {"location_id": "Dane's study", "phase": "rising",
                   "participants": ["kira", "dane"]}
    st["poses"] = {"kira": {"base": "bent_over", "anchor": "desk"}}
    st["clothing"] = {"kira": {"skirt": {"state": "displaced", "covers": ["hips", "thighs"]}}}
    st["contacts"] = {"k": {"from_char": "dane", "from_part": "hands", "to_char": "kira",
                            "to_part": "hips", "type": "gripping", "intensity": 2}}
    st["consent"] = {"kira|dane|vaginal": {"level": "enthusiastic", "max_intensity": None}}
    st["chars"] = {"kira": {"obsessions": {"entity:dane": {"target_kind": "entity",
                   "target": "Dane", "intensity": 55, "flavor": "romantic"}},
                   "cravings": {"wine": {"level": 39, "dependency": 0, "_seed": {}}}}}
    return st


def test_header_renders_04_blocks_and_drive_threshold():
    cfg = Config()
    h = render_header(rich_state(), cfg)
    for tag in ("[SCENE]", "[PHYSICAL]", "[CONTACT]", "[CONSENT]", "[DRIVES]"):
        assert tag in h, tag
    assert "Dane's study" in h and "bent_over@desk" in h
    assert "exposed: hips, thighs" in h                     # displaced skirt -> derived exposure
    assert "obsession Dane 55/100" in h
    assert "wine" not in h                                   # 39 < inject_threshold 40 (02 SS4.1)


def test_header_empty_state_renders_nothing():
    assert render_header(empty_state(), Config()) == ""      # 03 SS10: no state -> inject nothing


def test_raw_mode_omits_consent_block_entirely():
    cfg = Config()
    cfg.consent.mode = "unrestricted"
    h = render_header(rich_state(), cfg)
    assert "[CONSENT]" not in h and "[SCENE]" in h            # Q13: inert for generation


def test_frozen_flag_renders():
    st = rich_state()
    st["frozen"], st["frozen_reason"] = True, "safeword"
    assert "FROZEN — safeword active" in render_header(st, Config())


def test_guard_note_resolution_and_impersonate_suppression():
    cfg = Config()
    assert render_guard(cfg, None, "new_turn") == ""          # no name -> no guard (P5 heuristic)
    stamp = Stamp(session="s", user="Bean")
    g = render_guard(cfg, stamp, "new_turn")
    assert "Bean is played by the user ONLY" in g             # 04 SS3.2
    assert render_guard(cfg, stamp, "impersonate") == ""      # Q12: suppressed on impersonate
    cfg.user_guard.name = "Nyx"
    assert "Nyx" in render_guard(cfg, stamp, "new_turn")      # config outranks stamp
    cfg.user_guard.enabled = False
    assert render_guard(cfg, stamp, "new_turn") == ""


def test_governor_cap_floor_and_priorities():
    cfg = Config()
    header = Component("state_header", "H" * 330, 100)        # ~100 tokens
    memories = Component("memories", "M" * 3300, 60)          # ~1000 tokens
    lore = Component("lore", "L" * 3300, 20)
    kept = govern([lore, header, memories], cfg)              # cap 1200: header+memories fit, lore not
    assert [c.cls for c in kept] == ["state_header", "memories"]
    cfg.injection.max_tokens = 120                            # below header floor 150
    kept = govern([lore, header, memories], cfg)
    assert [c.cls for c in kept] == ["state_header"]          # 03 SS4: header-only below floor
    cfg.injection.max_tokens = 0
    assert govern([header], cfg) == []                        # cap<=0: inject nothing
    cfg.injection.max_tokens = 1200
    cfg.injection.assumed_ctx_tokens = 4000                   # min(1200, 0.15*4000=600)
    big_dir = Component("director_note", "D" * 2310, 80)      # ~700 tokens: over remaining
    kept = govern([header, big_dir, memories], cfg)
    assert [c.cls for c in kept] == ["state_header"]          # drop class AND everything below


def test_placement_modes_never_eat_history():
    cfg = Config()
    msgs = [{"role": "system", "content": "card"}] + \
           [{"role": "user", "content": f"u{i}"} if i % 2 == 0 else
            {"role": "assistant", "content": f"a{i}"} for i in range(8)]
    d = {"model": "m", "min_p": 0.07, "messages": msgs}
    for mode, check in (
            ("depth", lambda out: out["messages"][len(out["messages"]) - 1 - 3]["content"] == "STATE"),
            ("system_merge", lambda out: "SCENE STATE" in out["messages"][0]["content"]
             and "STATE" in out["messages"][0]["content"]),
            ("suffix", lambda out: out["messages"][-1]["content"] == "STATE")):
        cfg.injection.placement = mode
        out = splice(d, "STATE", cfg)
        assert check(out), mode
        for m in msgs[1:]:
            assert m in out["messages"], (mode, m)            # governor never eats history (07 P2 exit)
        assert out["min_p"] == 0.07                           # unknown fields survive splice
    cfg.injection.placement = "system_merge"
    out = splice({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, "STATE", cfg)
    assert out["messages"][0]["role"] == "system"             # no system msg -> insert at head


def test_compose_end_to_end_returns_none_when_empty():
    cfg = Config()
    d = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out, kept = compose(d, empty_state(), cfg, None, "new_turn")
    assert out is None and kept == []
    out, kept = compose(d, rich_state(), cfg, Stamp(session="s", user="Bean"), "new_turn")
    assert out is not None
    injected = [m for m in out["messages"] if "[SCENE]" in str(m.get("content"))]
    assert injected and "[CONTROL]" in injected[0]["content"]  # guard rides the header class
    assert kept and kept[0]["cls"] == "state_header"
