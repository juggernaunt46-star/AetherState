from aetherstate import compose, prompts
from aetherstate.config import Config
from aetherstate.pipeline import _apply_narrator_reasoning_default, _packet_manifest
from aetherstate.stamps import Stamp


def test_typed_narrator_wire_contract_migrates_old_frontend_prompt_stack():
    old_contract = (
        "WORLD: Ashfall Gate\n\n"
        "You are the Narrator — the voice of this world and everyone in it: every place.\n\n"
        "THE LEDGER. AetherState rides alongside this chat."
    )
    doc = {"messages": [
        {"role": "system", "content":
         "Write Ashfall Gate's next reply in a fictional chat between Ashfall Gate and Bean."},
        {"role": "system", "content": "World-info entry that must survive."},
        {"role": "system", "content": old_contract},
        {"role": "system", "content":
         "You are the Narrator: speak the world and its people, never the Player. The old tail."},
        {"role": "user", "content": "I open the gate."},
    ]}

    normalized, changed = compose.ensure_narrator_envelope(doc)
    wire = "\n".join(str(row.get("content", "")) for row in normalized["messages"])

    assert changed
    assert sum(row.get("content") == prompts.NARRATOR_ENVELOPE
               for row in normalized["messages"]) == 1
    assert "fictional chat between" not in wire
    assert "THE LEDGER" not in wire
    assert "World-info entry that must survive." in wire
    assert "WORLD: Ashfall Gate" in wire
    assert normalized["messages"][-1]["content"] == "I open the gate."


def test_current_narrator_wire_contract_is_idempotent():
    doc = {"messages": [
        {"role": "system", "content": prompts.NARRATOR_ENVELOPE},
        {"role": "user", "content": "I listen."},
    ]}

    normalized, changed = compose.ensure_narrator_envelope(doc)

    assert normalized is doc
    assert changed is False


def test_turn_packet_declares_aetherstate_authority_over_frontend_context():
    assert "AetherState governs this request" in compose.TURN_PACKET_AUTHORITY
    assert "SillyTavern/card/world-info/persona/example/history" \
        in compose.TURN_PACKET_AUTHORITY
    ladder = compose.TURN_PACKET_PRIORITY_LADDER
    assert ladder.index("P0 =") < ladder.index("P1 =") < ladder.index("P2 =") \
        < ladder.index("P3 =")
    assert "exact settled mechanics and outcomes" in ladder
    assert "[ENEMY INTENT]: exact code-owned future pending-move facts only" in ladder
    assert "never a settled impact" in ladder
    assert "exact future visible tell only" not in ladder
    assert "newest Player action" in ladder
    assert "ignore completely on a P0/P1 conflict" in ladder
    assert "never reconcile" in ladder


def test_packet_manifest_maps_shape_without_logging_prompt_text():
    doc = {
        "model": "example-model",
        "stream": True,
        "temperature": 0.8,
        "messages": [
            {"role": "system", "content": prompts.NARRATOR_ENVELOPE},
            {"role": "system", "content": compose.TURN_PACKET_START + "\n[SCENE] yard"},
            {"role": "user", "content":
             "[AETHER P0]\n[DIRECTIVE] settled\n[AETHER P1]\nsecret player prose"},
            {"role": "assistant", "content": "private story history"},
        ],
    }

    manifest = _packet_manifest(doc)

    assert [row["kind"] for row in manifest["messages"]] == [
        "narrator_contract", "aether_turn_packet", "player_current",
        "history_assistant",
    ]
    assert manifest["request_fields"] == ["model", "stream", "temperature"]
    assert manifest["sentinel_present"] is False
    assert "private" not in str(manifest)
    assert all(len(row["sha256"]) == 64 for row in manifest["messages"])


def test_packet_manifest_detects_transport_sentinel_leak():
    manifest = _packet_manifest({
        "messages": [{"role": "system", "content": "<<AETHER:session=leak>>"}],
    })

    assert manifest["sentinel_present"] is True


def test_venice_narrator_reasoning_is_hard_off_by_default():
    cfg = Config()
    cfg.upstream.base_url = "https://api.venice.ai/api/v1"
    doc = {
        "model": "zai-org-glm-5-2",
        "reasoning": {"enabled": True, "effort": "medium"},
        "reasoning_effort": "medium",
        "venice_parameters": {"disable_thinking": False, "character_slug": "kept"},
    }

    changed = _apply_narrator_reasoning_default(
        doc, cfg, Stamp(session="s", card_role="narrator"))

    assert changed is True
    assert doc["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in doc
    assert doc["venice_parameters"]["disable_thinking"] is True
    assert doc["venice_parameters"]["strip_thinking_response"] is True
    assert doc["venice_parameters"]["character_slug"] == "kept"

    receipt = _packet_manifest(doc)["reasoning_controls"]
    assert receipt == {
        "schema": "reasoning-controls/1",
        "reasoning_enabled": False,
        "reasoning_effort_present": False,
        "venice_disable_thinking": True,
        "venice_strip_thinking_response": True,
        "hard_off": True,
        "fingerprint": receipt["fingerprint"],
    }
    assert len(receipt["fingerprint"]) == 16
    assert "character_slug" not in str(receipt)


def test_reasoning_receipt_reports_incomplete_controls_without_copying_provider_payload():
    doc = {
        "reasoning": {"enabled": False, "effort": "low", "private": "do-not-copy"},
        "reasoning_effort": "low",
        "venice_parameters": {
            "disable_thinking": True,
            "strip_thinking_response": False,
            "api_key": "do-not-copy",
        },
        "messages": [{"role": "user", "content": "private prompt"}],
    }

    receipt = _packet_manifest(doc)["reasoning_controls"]

    assert receipt["reasoning_enabled"] is False
    assert receipt["reasoning_effort_present"] is True
    assert receipt["venice_disable_thinking"] is True
    assert receipt["venice_strip_thinking_response"] is False
    assert receipt["hard_off"] is False
    assert "private" not in str(receipt)
    assert "api_key" not in str(receipt)
    assert "low" not in str(receipt)


def test_narrator_reasoning_default_is_scoped_and_has_an_escape_hatch():
    stamp = Stamp(session="s", card_role="narrator")
    for base_url, enabled in [
        ("https://api.openai.com/v1", True),
        ("http://127.0.0.1:1234/v1", True),
        ("https://api.venice.ai/api/v1", False),
    ]:
        cfg = Config()
        cfg.upstream.base_url = base_url
        cfg.upstream.disable_narrator_reasoning = enabled
        doc = {"reasoning": {"enabled": True}}
        before = dict(doc["reasoning"])
        assert _apply_narrator_reasoning_default(doc, cfg, stamp) is False
        assert doc["reasoning"] == before

    cfg = Config()
    cfg.upstream.base_url = "https://api.venice.ai/api/v1"
    doc = {"reasoning": {"enabled": True}}
    assert _apply_narrator_reasoning_default(
        doc, cfg, Stamp(session="s", card_role="character")) is False
    assert doc["reasoning"] == {"enabled": True}
