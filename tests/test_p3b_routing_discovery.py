"""P3b fixtures: assist routing (Q8, 06 C), entity discovery (08 B2/E1), config group sync."""
from __future__ import annotations


from aetherstate import discovery
from aetherstate.config import AssistEndpointConfig, Config
from aetherstate.jobs import Batch, JobRunner
from aetherstate.state import apply_delta, current_state
from aetherstate.store import Store


def mk(**over):
    cfg = Config()
    for path, v in over.items():
        section, _, key = path.partition("__")
        setattr(getattr(cfg, section), key, v)
    store = Store(":memory:")
    sid, bid = store.create_session()
    return cfg, store, sid, bid


def assist_cfg(tier="small", maxc=2) -> Config:
    cfg = Config()
    cfg.extraction.mode = "assist"
    cfg.assist.endpoints = [AssistEndpointConfig(
        name="sidecar", base_url="http://127.0.0.1:11434/v1", api_key="k",
        model="qwen2.5-7b-instruct", tier=tier, max_concurrent=maxc)]
    return cfg


# ------------------------------ Q8 routing ------------------------------
def test_endpoint_for_routes_to_assist_with_tier_flag():
    cfg = assist_cfg(tier="small")
    runner = JobRunner(Store(":memory:"), cfg, ladder=None)
    ep, key, maxc = runner.endpoint_for("s1")
    assert ep.base_url == "http://127.0.0.1:11434/v1" and ep.model == "qwen2.5-7b-instruct"
    assert ep.assist_tier is True                      # 04 SS5: OP CARD stays on rung 1-2
    assert key == "assist:sidecar" and maxc == 2

    cfg2 = assist_cfg(tier="medium")
    ep2, _, _ = JobRunner(Store(":memory:"), cfg2, ladder=None).endpoint_for("s1")
    assert ep2.assist_tier is False                    # medium holds full prompt budget


def test_assist_mode_without_endpoints_falls_back_to_main():
    cfg = Config()
    cfg.extraction.mode = "assist"
    cfg.upstream.base_url = "http://main-upstream/v1"
    runner = JobRunner(Store(":memory:"), cfg, ladder=None)
    runner.models["s1"] = "glm-4"
    ep, key, maxc = runner.endpoint_for("s1")
    assert ep.base_url == "http://main-upstream/v1" and ep.model == "glm-4"
    assert key == "main" and maxc == 1 and ep.assist_tier is False


def test_main_mode_ignores_assist_endpoints():
    cfg = assist_cfg()
    cfg.extraction.mode = "main"
    cfg.upstream.base_url = "http://main-upstream/v1"
    ep, key, _ = JobRunner(Store(":memory:"), cfg, ladder=None).endpoint_for("s1")
    assert ep.base_url == "http://main-upstream/v1" and key == "main"


async def test_batch_flows_to_assist_endpoint():
    """e2e through notify -> queue -> dispatcher -> ladder.extract sees the assist Endpoint."""
    cfg = assist_cfg()
    cfg.extraction.debounce_s = 0.01
    store = Store(":memory:")
    sid, bid = store.create_session()
    seen = []

    class FakeLadder:
        async def extract(self, ep, snapshot, characters, t0, t1, exchange, context=""):
            seen.append(ep)
            return None                                # failure path: state untouched

    runner = JobRunner(store, cfg, FakeLadder())
    store.record_turn(bid, 1, "new_turn", "normal")
    store.write_turn_text(bid, 1, user_text="User: hello")
    store.record_turn(bid, 2, "new_turn", "normal")    # settles turn 1
    runner.notify(sid, bid, 2)
    await runner.drain(timeout=2.0)
    await runner.stop()
    assert seen and seen[0].base_url == "http://127.0.0.1:11434/v1"
    assert seen[0].assist_tier is True


# ------------------------------ config group sync (12) ------------------------------
def test_assist_groups_extraction_is_canonical_when_set():
    cfg = Config.model_validate({"assist": {"groups": {"extraction": "assist"}}})
    assert cfg.extraction.mode == "assist"
    cfg2 = Config.model_validate({"extraction": {"mode": "rules"}})
    assert cfg2.assist.groups.extraction == "rules"    # shortcut mirrored into groups
    assert cfg2.extraction.mode == "rules"


# ------------------------------ discovery: the scanner (08 B2) ------------------------------
def test_scan_attribution_and_action_verbs():
    assert discovery.scan('"Stay," Marla said, blocking the door.') == {"Marla"}
    assert discovery.scan("Suddenly Vex entered the tavern.") == {"Vex"}
    assert "Kira" in discovery.scan("Kira smiled and reached for the bottle.")


def test_scan_rejects_sentence_furniture_and_bare_caps():
    assert discovery.scan("The tavern was dark. Suddenly it rained.") == set()
    assert discovery.scan("She smiled. He nodded.") == set()
    assert discovery.scan("Marla is nice.") == set()   # no verb evidence, no attribution
    assert discovery.scan("") == set()


def test_scan_speaker_prefix_counts_but_narrator_never():
    assert "Vex" in discovery.scan("Vex: get down, now!")
    assert discovery.scan("Narrator: the rain kept falling.") == set()


# ------------------------------ discovery: threshold + creation ------------------------------
def test_two_turns_of_text_evidence_create_entity():
    cfg, store, sid, bid = mk()
    known = discovery.known_names(current_state(store, bid), ("Bean",))
    created = discovery.observe_text(store, cfg, sid, bid, 1, "Marla said hi.", known)
    assert created == []                               # 1 turn: below threshold
    created = discovery.observe_text(store, cfg, sid, bid, 2, "Marla walked closer.", known)
    assert created == ["Marla"]
    st = current_state(store, bid)
    assert any(e.get("name") == "Marla" for e in st["entities"].values())
    row = store.discovery_rows(bid)[0]
    assert row["status"] == "created"


def test_same_turn_evidence_counts_once():
    cfg, store, sid, bid = mk()
    known: set = set()
    discovery.observe_text(store, cfg, sid, bid, 3, "Marla said hi. Marla smiled.", known)
    assert store.discovery_bump(bid, "Marla", 3) == 1  # still one distinct turn


def test_auto_create_off_proposes_instead():
    cfg, store, sid, bid = mk(extraction__auto_entity_create=False)
    known: set = set()
    discovery.observe_text(store, cfg, sid, bid, 1, "Marla said hi.", known)
    discovery.observe_text(store, cfg, sid, bid, 2, "Marla laughed.", known)
    st = current_state(store, bid)
    assert not any(e.get("name") == "Marla" for e in st["entities"].values())
    assert store.discovery_rows(bid, "proposed")[0]["name"] == "Marla"


def test_extraction_can_never_create_entities():
    """03 SS5.1 / authority matrix: entity_add from extraction is quarantined."""
    cfg, store, sid, bid = mk()
    r = apply_delta(store, sid, bid, 1, [{"op": "entity_add", "name": "Zed"}],
                    "extraction", cfg)
    assert not r.applied and "privileged" in r.quarantined[0]["reason"]


def test_quarantine_feed_retro_unquarantines_current_batch():
    """08 B2: unknown-name ops feed the counter; creation retro-applies THIS batch's ops."""
    cfg, store, sid, bid = mk()
    runner = JobRunner(store, cfg, ladder=None)
    store.record_turn(bid, 1, "new_turn", "normal")
    q1 = [{"op": {"op": "mood", "char": "Zed", "valence": 5},
           "reason": "unknown entity 'Zed' (08 B2 discovery counts evidence)"}]
    n = runner._discover_from_quarantine(Batch(sid, bid, 1, 1, 1), q1)
    assert n == 0                                      # first evidence: counted, not created
    st = current_state(store, bid)
    assert not any(e.get("name") == "Zed" for e in st["entities"].values())

    store.record_turn(bid, 2, "new_turn", "normal")
    q2 = [{"op": {"op": "mood", "char": "Zed", "valence": 7},
           "reason": "unknown entity 'Zed' (08 B2 discovery counts evidence)"}]
    n = runner._discover_from_quarantine(Batch(sid, bid, 2, 2, 2), q2)
    assert n == 1                                      # created + retro-applied
    st = current_state(store, bid)
    zed = next(eid for eid, e in st["entities"].items() if e.get("name") == "Zed")
    assert st["chars"][zed]["affect"]["valence"] == 7  # the CURRENT batch's op landed


# ------------------------------ /aether/status caps view (10 SS5) ------------------------------
async def test_status_surfaces_caps_demotions_and_breakers(proxy_app, client):
    store, jobs = proxy_app.state.store, proxy_app.state.jobs
    store.caps_set("http://127.0.0.1:8080/v1", "local-q8", 1, native="llamacpp")
    store.caps_set("https://api.venice.ai/api/v1", "glm-4", 3)
    store.caps_fail("https://api.venice.ai/api/v1", "glm-4")
    jobs._disabled_until["sess-1"] = 42                # 09 C2 breaker open

    r = await client.get("/aether/status")
    ex = r.json()["extraction"]
    assert ex["mode"] == "main" and ex["groups"]["extraction"] == "main"
    caps = {c["model"]: c for c in ex["caps"]}
    assert caps["local-q8"]["rung"] == 1 and caps["local-q8"]["native"] == "llamacpp"
    assert caps["glm-4"]["rung"] == 3 and caps["glm-4"]["failures"] == 1
    assert ex["breakers"] == [{"session": "sess-1", "disabled_until_turn": 42}]
