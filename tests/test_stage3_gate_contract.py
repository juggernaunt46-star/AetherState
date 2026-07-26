from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import stage_gate_contract as contract

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools/build_stage3_gate_report.py"
VALIDATOR = ROOT / "tools/validate_stage3_gate.py"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
STAGE = "stage-3-versioned-database-evolution"
MERGE_TARGET = "55205f73c58da18a681212545c34e90ac63532a7"
EXPECTED_GATES = (
    "architecture-characterization",
    "database-migrations",
    "historical-schema",
    "installer-linux",
    "installer-windows",
    "javascript",
    "linux-py310-full",
    "linux-py312-full",
    "manifest",
    "package-artifacts",
    "package-linux-py310",
    "package-linux-py312",
    "package-windows-py312",
    "privacy",
    "scoped-static",
    "semantic-cube-complete",
    "windows-py312-full",
)
SHARED_JOBS = {
    "quality",
    "python-tests",
    "javascript",
    "package-build",
    "package-smoke",
}


def _tool(script: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_evidence(
    directory: Path,
    gate_id: str,
    *,
    commit: str,
    status: str = "PASS",
    reason_code: str = "command_passed",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{gate_id}.json").write_text(
        json.dumps(
            {
                "schema": contract.GATE_EVIDENCE_SCHEMA,
                "id": gate_id,
                "status": status,
                "elapsed_seconds": 0.125,
                "reason_code": reason_code,
                "evidence_commit": commit,
            }
        ),
        encoding="utf-8",
    )


def _build(
    tmp_path: Path,
    *,
    origin: str = "github-actions:1:1",
    omitted: set[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    evidence = tmp_path / "evidence"
    for gate_id in EXPECTED_GATES:
        if omitted is None or gate_id not in omitted:
            _write_evidence(evidence, gate_id, commit=_head())
    report = tmp_path / "stage-3-report.json"
    result = _tool(
        BUILDER,
        [
            "--evidence-dir",
            str(evidence),
            "--evidence-origin",
            origin,
            "--report",
            str(report),
        ],
    )
    return result, report


def _validate(report: Path) -> subprocess.CompletedProcess[str]:
    return _tool(
        VALIDATOR,
        ["--report", str(report), "--candidate", "HEAD"],
    )


def test_stage3_contract_binds_exact_stage_rows_and_transition_owner() -> None:
    assert contract.STAGE_3 == STAGE
    assert contract.STAGE_3_MERGE_TARGET == MERGE_TARGET
    assert contract.REQUIRED_STAGE_3_GATES == EXPECTED_GATES
    assert contract.TERMINAL_OWNERSHIP_STATES == frozenset(
        {"stage-2-cumulative", "stage-3-database-evolution"}
    )
    assert contract.HANDOFF_GATE_IDS == frozenset({"stage-3-database-evolution"})


def test_current_workflow_has_one_dependency_closed_stage3_terminal() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("AETHERSTATE_TERMINAL_OWNER: stage-3-database-evolution") == 1
    assert "\n  stage-3-database-evolution:\n" in text
    assert "\n  stage-2-cumulative:\n" not in text
    block = text.split("\n  stage-3-database-evolution:\n", 1)[1]
    assert (
        "needs: [quality, python-tests, javascript, package-build, package-smoke]"
        in block
    )
    for job in SHARED_JOBS:
        assert f"\n  {job}:\n" in text
        assert f"${{{{ needs.{job}.result }}}}" in block
    assert "--gate-id database-migrations" in text
    assert "tests/test_historical_schema_inventory.py" in text
    assert "tests/test_historical_database_upgrades.py" in text
    assert "--terminal" in block
    assert "tools/build_stage3_gate_report.py" in block
    assert "tools/validate_stage3_gate.py" in block


def test_stage3_terminal_keeps_legacy_required_check_display_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    job_key = "\n  stage-3-database-evolution:\n"
    assert text.count("AETHERSTATE_TERMINAL_OWNER: stage-3-database-evolution") == 1
    assert text.count(job_key) == 1
    assert "\n  stage-2-cumulative:\n" not in text

    block = text.split(job_key, 1)[1]
    assert block.startswith("    name: stage-2-cumulative\n")
    assert "--gate-id semantic-cube-complete" in block
    assert "tools/build_stage3_gate_report.py" in block
    assert "--report build/hardening/database-evolution/stage-3-report.json" in block
    assert "tools/validate_stage3_gate.py" in block
    assert (
        "name: stage-3-database-evolution-${{ env.AETHERSTATE_CANDIDATE_SHA }}"
        in block
    )


@pytest.mark.parametrize("missing_job", sorted(SHARED_JOBS))
def test_stage3_terminal_rejects_a_missing_shared_dependency(
    missing_job: str,
) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    expected = "needs: [quality, python-tests, javascript, package-build, package-smoke]"
    assert expected in text
    mutated = text.replace(f", {missing_job}", "", 1).replace(
        f"{missing_job}, ", "", 1
    )
    block = mutated.split("\n  stage-3-database-evolution:\n", 1)[1]
    assert expected not in block


def test_stage3_builder_emits_complete_exact_candidate_report(tmp_path: Path) -> None:
    result, report_path = _build(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == contract.STAGE_REPORT_SCHEMA
    assert report["stage"] == STAGE
    assert report["public_base"] == contract.PUBLIC_BASE
    assert report["evidence_commit"] == _head()
    assert report["evidence_tree"] == contract.tree_for_commit(ROOT, _head())
    assert report["proof_input_sha256"] == contract.proof_input_sha256(ROOT, _head())
    assert [row["id"] for row in report["gates"]] == list(EXPECTED_GATES)
    assert report["contracts"] == {
        "umbrella_sha256": contract.UMBRELLA_SHA256,
        "semantic_cube_canonical_tip": contract.SEMANTIC_CUBE_CANONICAL_TIP,
        "semantic_cube_sha256": contract.SEMANTIC_CUBE_SHA256,
    }
    assert report["manifest"]["path"] == contract.MANIFEST_PATH
    assert report["budget"] == contract.BUDGET
    assert report["status"] == "PASS"
    assert report["reason_code"] == "all_required_gates_passed"
    validated = _validate(report_path)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert validated.stdout.strip() == f"PASS {STAGE} {_head()}"


def test_stage3_report_cannot_pass_without_migration_evidence(tmp_path: Path) -> None:
    result, report_path = _build(tmp_path, omitted={"database-migrations"})
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] != "PASS"
    assert next(
        row for row in report["gates"] if row["id"] == "database-migrations"
    )["status"] == "NOT_RUN"


def test_local_windows_report_holds_only_for_cross_platform_ci(tmp_path: Path) -> None:
    linux_only = {
        "installer-linux",
        "linux-py310-full",
        "linux-py312-full",
        "package-linux-py310",
        "package-linux-py312",
    }
    result, report_path = _build(
        tmp_path,
        origin="local-windows",
        omitted=linux_only,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "HOLD"
    assert report["reason_code"] == "cross_platform_ci_pending"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("evidence_commit", "0" * 40, "evidence_commit_mismatch"),
        ("evidence_tree", "0" * 40, "evidence_tree_mismatch"),
        ("proof_input_sha256", "0" * 64, "proof_input_changed"),
        ("budget", {"terminal_hold_seconds": True}, "invalid_report"),
        ("evidence_origin", "C:\\outside\\provider-row.txt", "invalid_report"),
        ("evidence_origin", "SELECT * FROM private_story", "invalid_report"),
        ("evidence_origin", "RuntimeError: provider failed", "invalid_report"),
    ],
)
def test_stage3_validator_rejects_identity_budget_and_forbidden_content(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    result, report_path = _build(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report[field] = value
    report_path.write_text(json.dumps(report), encoding="utf-8")
    validated = _validate(report_path)
    assert validated.returncode == 1
    assert validated.stdout.strip() == f"INVALID {STAGE} {reason}"
