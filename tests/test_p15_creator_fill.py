"""P15 (2026-07-09, Bean's Creator auto-fill bug): the boxes must COMPLETE.

Two live-reproduced root causes: (a) deterministic_world hard-clamped 'Name — description'
faction/location rows at 80 chars — every AI auto-fill returned rows amputated mid-word
('…hydrogel windo') and the stumps round-tripped into drafts, cards, and the ledger;
(b) _keep_seed_rows kept typed rows canon at ROW level, discarding the model's completed
version — so a blank field on an existing row (an npc's `home`, a bare custom skill's
mechanics, a bare faction's description) could never be filled by 'Auto-fill blanks', ever.
"""
from __future__ import annotations

from aetherstate import creator


LONG_ROW = ("The Spire Administration Bureau — corporate remnant controlling Floors 46 and "
            "above, rationing power, law, and desalinated water while quietly welding shut "
            "every downward passage it can reach")


# ------------------------------ (a) word-safe clamps ------------------------------
def test_long_composite_row_survives_intact():
    w = creator.deterministic_world({"name": "W", "genre": "cyberpunk",
                                     "factions": [LONG_ROW], "locations": [LONG_ROW]})
    assert w["factions"][0] == LONG_ROW          # under the new cap: byte-identical
    assert w["locations"][0] == LONG_ROW


def test_overlong_row_cuts_word_safe_never_mid_word():
    monster = "The Ledger Boys — " + "waterline barter cartel enforcing the truce " * 20
    w = creator.deterministic_world({"factions": [monster]})
    row = w["factions"][0]
    assert len(row) <= 500
    assert monster.startswith(row)               # a clean prefix…
    assert monster[len(row)] == " "              # …ending exactly at a word boundary
    assert not row.endswith((" ", ",", ";", ":", "—", "–", "-"))


def test_tone_and_date_no_longer_amputated():
    tone = "neon-drowned noir, saltwater dread, debt and silence measured in welded bulkheads"
    date = "15 years post-Drowning — Year of the Flooded Calendar, third season of the truce"
    w = creator.deterministic_world({"tone": tone, "date": date})
    assert w["tone"] == tone and w["date"] == date


def test_split_name_desc_tail_word_safe():
    src = "very long detail " * 40
    head, tail = creator._split_name_desc("Name — " + src)
    assert head == "Name"
    assert len(tail) <= 400
    assert src.startswith(tail)                   # a clean prefix…
    assert src[len(tail)] == " "                  # …ending exactly at a word boundary


def test_s_soft_hard_fallback_single_token():
    s = "x" * 900                                 # no boundary at all -> hard cut still holds
    assert creator._s_soft(s, 100) == "x" * 100


# ------------------------------ (b) field-level completion ------------------------------
def test_autofill_completes_blank_npc_home_keeps_typed_fields():
    seed = [{"name": "Maraen Stoltz", "role": "Director", "desc": "Signs orders.", "home": ""}]
    model = [{"name": "Maraen Stoltz", "role": "REWRITTEN", "desc": "REWRITTEN",
              "home": "The Aerie"},
             {"name": "New Guy", "role": "r", "desc": "d", "home": "Midreach"}]
    out = creator._keep_seed_rows(seed, model)
    assert out[0]["home"] == "The Aerie"          # the blank filled
    assert out[0]["role"] == "Director"           # typed fields stay verbatim
    assert out[0]["desc"] == "Signs orders."
    assert any(isinstance(r, dict) and r.get("name") == "New Guy" for r in out[1:])


def test_autofill_completes_bare_string_row_with_model_desc():
    out = creator._keep_seed_rows(["The Iron Pact"],
                                  ["The Iron Pact — rearms the northern marches"])
    assert out[0] == "The Iron Pact — rearms the northern marches"


def test_typed_description_row_stays_byte_identical():
    mine = "The Iron Pact — my canon words"
    out = creator._keep_seed_rows([mine], ["The Iron Pact — model words"])
    assert out[0] == mine


def test_autofill_completes_bare_custom_skill_mechanics():
    seed = [{"name": "Tide-Walking"}]
    model = [{"id": "tide_walking", "name": "Tide-Walking", "keyed_stat": "DEX",
              "governs": ["wade", "swim", "cross"], "desc": "Walk the flooded floors."}]
    out = creator._keep_seed_rows(seed, model)
    assert out[0]["keyed_stat"] == "DEX" and out[0]["governs"] == ["wade", "swim", "cross"]
    assert out[0]["name"] == "Tide-Walking"


def test_model_only_rows_still_append_and_cap_holds():
    model = [{"name": f"NPC {i}", "role": "", "desc": "", "home": ""} for i in range(20)]
    out = creator._keep_seed_rows([], model)
    assert len(out) == 12                          # the cap
