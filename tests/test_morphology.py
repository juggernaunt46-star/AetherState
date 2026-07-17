from __future__ import annotations

import pytest

from aetherstate.morphology import productive_compound_head


GENERIC_WEAPON_HEADS = (
    "blade", "sword", "hammer", "knife", "axe", "rifle", "carbine", "pistol", "cannon",
)


@pytest.mark.parametrize(("token", "expected"), [
    ("vibroblade", "blade"),
    ("chainsword", "sword"),
    ("powerhammer", "hammer"),
    ("vibroknife", "knife"),
    ("chainaxe", "axe"),
    ("lasrifle", "rifle"),
    ("pulsecarbine", "carbine"),
    ("laspistol", "pistol"),
    ("ioncannon", "cannon"),
])
def test_productive_compound_head_returns_only_caller_licensed_heads(
    token: str, expected: str
) -> None:
    assert productive_compound_head(token, GENERIC_WEAPON_HEADS) == expected


@pytest.mark.parametrize("token", GENERIC_WEAPON_HEADS)
def test_productive_compound_head_does_not_reclassify_bare_heads(token: str) -> None:
    assert productive_compound_head(token, GENERIC_WEAPON_HEADS) is None


def test_productive_compound_head_enforces_modifier_length() -> None:
    assert productive_compound_head("xxblade", ("blade",)) is None
    assert productive_compound_head("xxxblade", ("blade",)) == "blade"


def test_productive_compound_head_respects_caller_precedence() -> None:
    assert productive_compound_head("redmoonblade", ("blade", "moonblade")) == "blade"
    assert productive_compound_head("redmoonblade", ("moonblade", "blade")) == "moonblade"


def test_productive_compound_head_rejects_negative_modifier_lengths() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        productive_compound_head("vibroblade", ("blade",), -1)
