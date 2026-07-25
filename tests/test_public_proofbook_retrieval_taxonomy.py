from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "proofbook" / "engineering_learning.py"
LEDGER = ROOT / "proofbook" / "LEDGER.jsonl"


def test_proofbook_task_alias_retrieves_tooling_lessons() -> None:
    """Break: Proofbook task wording must not rank Semantic Cube advice."""
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--workspace-root",
            str(ROOT),
            "brief",
            "--task",
            "update ProofBook validation",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    records = {
        record["lesson_key"]: record
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line
        for record in (json.loads(line),)
    }
    returned_keys = []
    for position in range(1, 6):
        prefix = f"{position}. "
        line = next(
            output_line
            for output_line in result.stdout.splitlines()
            if output_line.startswith(prefix)
        )
        returned_keys.append(line[len(prefix):].rsplit(" r", 1)[0])

    assert {records[key]["domain"] for key in returned_keys} == {"tooling"}
    assert result.stdout.count("Matched: reviewed tag or alias") == 5
