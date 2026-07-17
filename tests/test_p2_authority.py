"""P2 fixtures: mutation authority (02 SS12b, Q11), freeze semantics (02 SS6), raw mode (Q13/Q14)."""
from __future__ import annotations

from aetherstate.config import Config
from aetherstate.state import apply_delta
from aetherstate.store import Store


def mk(**cfg_over):
    cfg = Config()
    for k, v in cfg_over.items():
        sec, _, key = k.partition("__")
        setattr(getattr(cfg, sec), key, v)
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="x")
    apply_delta(store, sid, bid, 0, [{"op": "entity_add", "name": "Kira"},
                                     {"op": "entity_add", "name": "Dane"}], "user", cfg)
    return cfg, store, sid, bid


def test_user_organic_edit_gated_by_manual_override():
    cfg, store, sid, bid = mk()
    op = {"op": "arousal", "char": "Kira", "set": 80}
    r = apply_delta(store, sid, bid, 1, [op], "user", cfg)
    assert not r.applied and "manual_override" in r.quarantined[0]["reason"]
    cfg.manual_override.enabled = True                          # live toggle (12: hot-reload)
    r = apply_delta(store, sid, bid, 1, [op], "user", cfg)
    assert r.applied and r.state["chars"]["kira"]["arousal"]["arousal"] == 80


def test_user_consent_downgrade_always_upgrade_gated():
    cfg, store, sid, bid = mk()
    up = {"op": "consent_set", "subject": "Kira", "partner": "Dane",
          "category": "vaginal", "level": "granted"}
    r = apply_delta(store, sid, bid, 1, [up], "user", cfg)
    assert not r.applied and "consent upgrade is gated" in r.quarantined[0]["reason"]
    down = {**up, "level": "hard_limit"}
    r = apply_delta(store, sid, bid, 1, [down], "user", cfg)    # safety-direction: always
    assert r.applied
    cfg.manual_override.enabled = True
    r = apply_delta(store, sid, bid, 2, [up], "user", cfg)      # hard-limit relax = human-gated
    assert r.applied and r.state["consent"]["kira|dane|vaginal"]["level"] == "granted"


def test_extraction_cannot_touch_safety_or_consent_set():
    cfg, store, sid, bid = mk()
    for op in ({"op": "unfreeze"},
               {"op": "consent_set", "subject": "Kira", "partner": "Dane",
                "category": "anal", "level": "granted"}):
        r = apply_delta(store, sid, bid, 1, [op], "extraction", cfg)
        assert not r.applied


def test_freeze_suppresses_escalation_but_not_withdrawal():
    """02 SS6: frozen quarantines arousal/escalation/consent; withdraw + user stay live."""
    cfg, store, sid, bid = mk()
    apply_delta(store, sid, bid, 1, [
        {"op": "scene_set", "participants": ["kira", "dane"]},
        {"op": "consent_signal", "from_char": "Kira", "to_char": "Dane",
         "category": "vaginal", "signal": "enthusiastic"}], "extraction", cfg)
    r = apply_delta(store, sid, bid, 2, [{"op": "freeze", "reason": "safeword"}], "user", cfg)
    assert r.froze and r.state["frozen"]
    # 02 SS6: safeword sets in-scene consent to withdrawn
    assert r.state["consent"]["kira|dane|vaginal"]["level"] == "withdrawn"
    r = apply_delta(store, sid, bid, 3, [
        {"op": "arousal", "char": "Kira", "delta": 10},
        {"op": "contact", "action": "start", "from_char": "Dane", "from_part": "hands",
         "to_char": "Kira", "to_part": "hips", "type": "gripping"}], "extraction", cfg)
    assert not r.applied and len(r.quarantined) == 2
    r = apply_delta(store, sid, bid, 3, [
        {"op": "consent_signal", "from_char": "Kira", "to_char": "Dane",
         "category": "anal", "signal": "withdraw"}], "extraction", cfg)
    assert r.applied                                            # safety-direction flows while frozen
    r = apply_delta(store, sid, bid, 4, [{"op": "unfreeze"}], "rule", cfg)
    assert not r.applied                                        # unfreeze is user-only
    r = apply_delta(store, sid, bid, 4, [{"op": "unfreeze"}], "user", cfg)
    assert r.unfroze and not r.state["frozen"]


def test_extraction_safeword_freezes_except_in_raw():
    """Belt+suspenders (02 SS11) vs Q13/Q14 raw neutrality: logged as data, never freezes."""
    op = {"op": "consent_signal", "from_char": "Kira", "to_char": "Dane",
          "category": "other", "signal": "safeword"}
    cfg, store, sid, bid = mk()
    r = apply_delta(store, sid, bid, 1, [op], "extraction", cfg)
    assert r.froze
    cfg2, store2, sid2, bid2 = mk(consent__mode="unrestricted")
    r = apply_delta(store2, sid2, bid2, 1, [op], "extraction", cfg2)
    assert r.applied and not r.froze and not r.state["frozen"]


def test_unknown_entity_quarantined_for_nonuser_sources():
    """03 SS5.1/08 E3: alias-resolution failure quarantines; discovery is P3. User authoring creates."""
    cfg, store, sid, bid = mk()
    ghost = {"op": "arousal", "char": "Zed", "delta": 5}
    r = apply_delta(store, sid, bid, 1, [ghost], "extraction", cfg)
    assert not r.applied and "unknown entity" in r.quarantined[0]["reason"]
    cfg.manual_override.enabled = True
    r = apply_delta(store, sid, bid, 1, [ghost], "user", cfg)
    assert r.applied and "zed" in r.state["entities"]
