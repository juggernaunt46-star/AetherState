from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "aetherstate-code-learning" / "SKILL.md"
LEDGER_CONTRACT = (
    ROOT
    / "skills"
    / "aetherstate-code-learning"
    / "references"
    / "ledger-contract.md"
)


def test_public_skill_requires_the_separate_publication_gate() -> None:
    combined = (
        SKILL.read_text(encoding="utf-8")
        + "\n"
        + LEDGER_CONTRACT.read_text(encoding="utf-8")
    )
    folded = combined.casefold()
    assert (
        "python tools/proofbook/publication_attestations.py validate"
        in combined
    )
    assert "complete ledger" in folded
    assert "public review artifact" in folded
