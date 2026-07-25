from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "aetherstate-code-learning"
SKILL = SKILL_ROOT / "SKILL.md"
REFERENCE = SKILL_ROOT / "references" / "ledger-contract.md"
AGENT = SKILL_ROOT / "agents" / "openai.yaml"


def test_public_code_learning_skill_is_complete_and_self_contained() -> None:
    assert SKILL.is_file()
    assert REFERENCE.is_file()
    assert AGENT.is_file()

    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter, body = text[4:].split("\n---\n", 1)
    assert set(
        line.split(":", 1)[0] for line in frontmatter.splitlines()
    ) == {"name", "description"}
    assert "name: aetherstate-code-learning" in frontmatter
    assert "Proofbook" in frontmatter
    assert len(body.splitlines()) < 220

    agent = AGENT.read_text(encoding="utf-8")
    assert "$aetherstate-code-learning" in agent
    assert "display_name:" in agent
    assert "short_description:" in agent
    assert "default_prompt:" in agent


def test_public_skill_routes_every_lesson_to_public_private_or_abstain() -> None:
    combined = (
        SKILL.read_text(encoding="utf-8")
        + "\n"
        + REFERENCE.read_text(encoding="utf-8")
    )
    folded = combined.casefold()
    assert "public" in folded
    assert "private" in folded
    assert "abstain" in folded
    assert "classify the lesson destination before" in folded
    assert "all evidence is present in the public repository" in folded
    assert "any evidence is private or unavailable" in folded
    assert "when destination or evidence safety is ambiguous" in folded
    assert "never copy a private lesson into the public ledger" in folded


def test_public_skill_uses_only_public_commands_and_paths() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (SKILL, REFERENCE, AGENT)
    )
    folded = combined.casefold()
    assert "python tools/proofbook/engineering_learning.py validate" in combined
    assert "python tools/proofbook/engineering_learning.py status" in combined
    assert "python tools/proofbook/engineering_learning.py brief" in combined
    forbidden = (
        ".codex/",
        ".codex\\",
        ".worktrees",
        "aetherstate-personal",
        "local-only",
        "knowledge/",
        "tooling/",
        "c:\\",
        "refs/remotes/origin/main",
    )
    assert all(marker not in folded for marker in forbidden)


def test_public_skill_keeps_proofbook_out_of_runtime_authority() -> None:
    combined = (
        SKILL.read_text(encoding="utf-8")
        + "\n"
        + REFERENCE.read_text(encoding="utf-8")
    ).casefold()
    assert "developer-only" in combined
    assert "never gameplay authority" in combined
    assert "never player data" in combined
    assert "never model-training data" in combined
    assert "source and tests outrank lessons" in combined
