"""A1 (2026-07-10, Bean): auto-flip the DM rules-contract to its compact form on calm,
established turns. Gated by [specialization].auto_compact_contract (opt-in) + the warm-up
window [specialization].contract_full_turns. The FULL contract stays for the first N turns and
EVERY combat turn; a calm, established turn goes compact. Off (default) => an rpg session is
byte-identical to before (asserted here). Pure state read, no network — invariant-2 safe."""
from __future__ import annotations

import httpx

from aetherstate import compose
from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.stamps import Stamp
from aetherstate.state import apply_delta
from aetherstate.store import Store
from tests.mock_upstream import MockUpstream

FULL_MARK = "You are the Game Master of a mechanical RPG"   # unique to DM_RULES_CONTRACT
COMPACT_MARK = "A GAME with dice, not chat."                # unique to DM_RULES_CONTRACT_COMPACT


def _cfg(*, auto=False, full_turns=3, contract="full", max_tokens=8000) -> Config:
    cfg = Config()
    cfg.specialization.name = "rpg"
    cfg.specialization.auto_compact_contract = auto
    cfg.specialization.contract_full_turns = full_turns
    cfg.specialization.contract = contract
    cfg.injection.max_tokens = max_tokens      # generous: isolate the flip from the budget-degrade
    return cfg


def _state(turn=10, phase="lull", combat=False) -> dict:
    st = {
        "meta": {"turn": turn}, "scene": {"location_id": "the tavern", "phase": phase},
        "clock": {}, "chars": {}, "attributes": {}, "poses": {}, "clothing": {},
        "effects": {}, "quests": {}, "rolls": [],
        "player": {"bean": {"level": 2, "stats": {"STR": 10}}},
        "entities": {"bean": {"name": "Bean", "kind": "player", "present": True},
                     "greta": {"name": "Greta", "kind": "npc", "present": True}},
    }
    if combat:
        st["combat"] = {"active": True, "started_turn": turn,
                        "combatants": {"foe1": {"name": "Bandit", "side": "foe",
                                                "tier": "standard", "weapon": "club",
                                                "alive": True, "hp": {"cur": 6, "max": 6}}}}
    return st


# ------------------------------ the decision helper (pure) ----------------------------
def test_off_by_default_never_flips():
    # opt-in: with the knob off, a calm established turn is NEVER auto-compacted
    assert compose._auto_compact_contract(_state(turn=50), _cfg(auto=False)) is False


def test_calm_established_flips():
    assert compose._auto_compact_contract(_state(turn=10), _cfg(auto=True)) is True


def test_warmup_window_stays_full():
    cfg = _cfg(auto=True, full_turns=3)
    assert compose._auto_compact_contract(_state(turn=3), cfg) is False   # within warm-up
    assert compose._auto_compact_contract(_state(turn=4), cfg) is True    # first calm turn after
    # full_turns=0 -> compact-eligible from turn 1
    assert compose._auto_compact_contract(_state(turn=1), _cfg(auto=True, full_turns=0)) is True


def test_combat_active_stays_full():
    assert compose._auto_compact_contract(_state(turn=10, combat=True), _cfg(auto=True)) is False


def test_combat_phase_stays_full():
    for ph in ("climax", "combat", "battle", "fight", "ambush"):
        assert compose._auto_compact_contract(_state(turn=10, phase=ph), _cfg(auto=True)) is False


def test_explicit_compact_config_not_double_handled():
    # contract="compact" is already compact by config; the auto path stays out of it
    assert compose._auto_compact_contract(_state(turn=10), _cfg(auto=True, contract="compact")) \
        is False


def test_combat_turn_helper():
    assert compose._combat_turn(_state(combat=True)) is True
    assert compose._combat_turn(_state(phase="ambush")) is True
    assert compose._combat_turn(_state(phase="lull")) is False


# ------------------------------ end-to-end through compose ----------------------------
def _injected(cfg, state) -> str:
    doc = {"model": "m", "messages": [{"role": "user", "content": "I sip my ale."}]}
    out, _kept = compose.compose(doc, state, cfg, Stamp(session="s"), "new_turn")
    assert out is not None
    return "\n".join(str(m.get("content")) for m in out["messages"])


def test_e2e_off_default_injects_full_contract():
    body = _injected(_cfg(auto=False), _state(turn=10))
    assert FULL_MARK in body and COMPACT_MARK not in body     # byte-identical to prior behavior


def test_e2e_on_calm_established_injects_compact():
    body = _injected(_cfg(auto=True), _state(turn=10))
    assert COMPACT_MARK in body and FULL_MARK not in body     # the flip landed on the wire


def test_e2e_on_warmup_injects_full():
    body = _injected(_cfg(auto=True, full_turns=3), _state(turn=2))
    assert FULL_MARK in body and COMPACT_MARK not in body     # warm-up turn keeps full


def test_e2e_on_combat_injects_full():
    body = _injected(_cfg(auto=True), _state(turn=10, combat=True))
    assert FULL_MARK in body and COMPACT_MARK not in body     # combat keeps the war-room rules


def test_e2e_none_session_injects_no_contract():
    cfg = _cfg(auto=True)
    cfg.specialization.name = "none"
    body = _injected(cfg, _state(turn=10))
    assert FULL_MARK not in body and COMPACT_MARK not in body   # no rpg contract under `none`


# ------------- the /briefing inspector reports the REAL contract the next request carries -------
async def _briefing(cfg, head_turn, phase):
    store = Store(":memory:")
    sid, bid = store.create_session(external_id="st-p17")
    apply_delta(store, sid, bid, head_turn,
                [{"op": "scene_set", "location": "tavern", "phase": phase}], "user", cfg)
    app = create_app(cfg, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=MockUpstream())), store=store)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")
    j = (await client.get("/aether/session/st-p17/briefing")).json()
    await client.aclose()
    return j


async def test_briefing_reports_compact_on_calm_established():
    # head=3 -> the next request composes at turn 4 (>2), calm -> compact reported
    j = await _briefing(_cfg(auto=True, full_turns=2), head_turn=3, phase="lull")
    assert j["contract_variant"] == "compact" and j["upcoming_turn"] == 4


async def test_briefing_reports_full_in_combat():
    j = await _briefing(_cfg(auto=True, full_turns=2), head_turn=3, phase="climax")
    assert j["contract_variant"] == "full"


async def test_briefing_reports_full_during_warmup():
    # head=1 -> next request is turn 2 (<=2) -> still full
    j = await _briefing(_cfg(auto=True, full_turns=2), head_turn=1, phase="lull")
    assert j["contract_variant"] == "full"


async def test_briefing_full_when_feature_off():
    j = await _briefing(_cfg(auto=False), head_turn=3, phase="lull")
    assert j["contract_variant"] == "full"


# ------------- auto_compact_contract is a LIVE-settable knob (POST /aether/specialization) -------
async def test_specialization_route_toggles_auto_compact(tmp_path):
    cfg = _cfg(auto=False)
    cfg.server.data_dir = str(tmp_path)      # keep _persist_config off the real data dir
    app = create_app(cfg, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=MockUpstream())), store=Store(":memory:"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy")
    posted = (await client.post("/aether/specialization",
                                json={"auto_compact_contract": True})).json()
    got = (await client.get("/aether/specialization")).json()
    await client.aclose()
    assert posted["auto_compact_contract"] is True and got["auto_compact_contract"] is True
    assert cfg.specialization.auto_compact_contract is True    # flipped live on the running cfg
