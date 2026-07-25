from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools import stage_gate_contract as contract

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools/build_stage_gate_report.py"
VALIDATOR = ROOT / "tools/validate_stage_gate.py"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
STAGE_2_PLAN = (
    "docs/superpowers/plans/2026-07-24-semantic-cube-narrator-output-integrity.md"
)
EXPECTED_GATES = {
    "architecture-characterization",
    "historical-schema",
    "installer-linux",
    "installer-windows",
    "javascript",
    "linux-py310-full",
    "linux-py312-full",
    "manifest",
    "no-runtime-diff",
    "package-artifacts",
    "package-linux-py310",
    "package-linux-py312",
    "package-windows-py312",
    "privacy",
    "scoped-static",
    "windows-py312-full",
}
SHARED_JOBS = {"quality", "python-tests", "javascript", "package-build", "package-smoke"}
MATRIX_MARKERS = {
    "linux-py310-full",
    "linux-py312-full",
    "windows-py312-full",
    "installer-linux",
    "installer-windows",
    "package-linux-py310",
    "package-linux-py312",
    "package-windows-py312",
}
QUALITY_GATE_ORDER = (
    "scoped-static",
    "manifest",
    "architecture-characterization",
    "historical-schema",
    "privacy",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _head(cwd: Path = ROOT) -> str:
    return _git(cwd, "rev-parse", "HEAD")


def _tool(
    script: Path,
    args: list[str],
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_evidence(
    directory: Path,
    gate_id: str,
    *,
    commit: str,
    status: str = "PASS",
    reason_code: str = "command_passed",
    elapsed: float = 0.125,
    source_gate: str | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    value: dict[str, object] = {
        "schema": "aetherstate-hardening-gate-evidence/1",
        "id": gate_id,
        "status": status,
        "elapsed_seconds": elapsed,
        "reason_code": reason_code,
        "evidence_commit": commit,
    }
    if source_gate is not None:
        value["source_gate"] = source_gate
    (directory / f"{gate_id}.json").write_text(
        json.dumps(value),
        encoding="utf-8",
    )


def _write_all_pass(directory: Path, *, commit: str) -> None:
    for gate_id in sorted(EXPECTED_GATES):
        _write_evidence(directory, gate_id, commit=commit)


def _build(
    tmp_path: Path,
    *,
    cwd: Path = ROOT,
    evidence_origin: str = "github-actions:1:1",
    evidence_dir: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    directory = evidence_dir or tmp_path / "evidence"
    report = tmp_path / "stage-1-terminal-report.json"
    result = _tool(
        BUILDER,
        [
            "--evidence-dir",
            str(directory),
            "--evidence-origin",
            evidence_origin,
            "--report",
            str(report),
        ],
        cwd=cwd,
    )
    return result, report


def _validate(
    report: Path,
    *,
    cwd: Path = ROOT,
    candidate: str = "HEAD",
    require_pass: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = [
        "--report",
        str(report),
        "--stage",
        "stage-1-safety-baseline",
        "--candidate",
        candidate,
    ]
    if require_pass:
        args.append("--require-pass")
    return _tool(VALIDATOR, args, cwd=cwd)


def _pass_report(tmp_path: Path, *, cwd: Path = ROOT) -> Path:
    evidence = tmp_path / "evidence"
    _write_all_pass(evidence, commit=_head(cwd))
    result, report = _build(tmp_path, cwd=cwd, evidence_dir=evidence)
    assert result.returncode == 0, result.stdout + result.stderr
    return report


def _report_gate(report: dict[str, object], gate_id: str) -> dict[str, object]:
    gates = report["gates"]
    assert isinstance(gates, list)
    return next(row for row in gates if row["id"] == gate_id)


def _clone(tmp_path: Path) -> Path:
    clone = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "--shared", "--quiet", str(ROOT), str(clone)],
        check=True,
    )
    _git(clone, "config", "user.email", "task6@example.invalid")
    _git(clone, "config", "user.name", "Task Six")
    return clone


def _commit_file(repo: Path, relative: str, content: str, message: str = "fixture") -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", relative)
    _git(repo, "commit", "-m", message)
    return _head(repo)


def _assert_workflow_contract(text: str) -> None:
    owners = re.findall(
        r"(?m)^\s{2}AETHERSTATE_TERMINAL_OWNER:\s*([a-z0-9-]+)\s*$",
        text,
    )
    assert len(owners) == 1
    owner = owners[0]
    assert owner in {"stage-1-bootstrap", "stage-2-cumulative"}
    jobs = set(re.findall(r"(?m)^  ([a-z0-9-]+):\s*$", text))
    assert SHARED_JOBS <= jobs
    assert MATRIX_MARKERS <= set(re.findall(r"\b[a-z]+(?:-[a-z0-9]+)+\b", text))
    assert "AETHERSTATE_CANDIDATE_SHA:" in text
    assert text.count("ref: ${{ env.AETHERSTATE_CANDIDATE_SHA }}") == 6
    if owner == "stage-1-bootstrap":
        assert jobs == SHARED_JOBS | {"stage-1-report"}
        assert "--gate-id no-runtime-diff" in text
        assert "--gate-id stage-2-cumulative" not in text
    else:
        assert jobs == SHARED_JOBS | {"stage-2-cumulative"}
        assert "--gate-id no-runtime-diff" not in text
        assert "--gate-id stage-2-cumulative" in text
        assert "--failure-reason stage_2_cumulative_failed" in text
        stage_2_block = text.split("  stage-2-cumulative:", 1)[1]
        assert "build/hardening/gates/stage-2-cumulative.json" not in stage_2_block


def _quality_workflow(text: str) -> str:
    return text.split("  quality:", 1)[1].split("\n  python-tests:", 1)[0]


def test_shared_contract_is_the_single_exact_source_of_stage_values() -> None:
    assert contract.REQUIRED_STAGE_1_GATES == EXPECTED_GATES
    assert contract.SHARED_CI_JOB_IDS == frozenset(SHARED_JOBS)
    assert contract.TERMINAL_OWNERSHIP_STATES == frozenset(
        {"stage-1-bootstrap", "stage-2-cumulative"}
    )
    assert contract.HANDOFF_GATE_IDS == frozenset({"stage-2-cumulative"})
    assert "stage-2-cumulative" not in contract.REQUIRED_STAGE_1_GATES
    assert contract.LOCAL_DIAGNOSTIC_GATES == {
        "local-public-scope",
        "local-terminal-budget",
        "local-windows-py310-full",
        "package-windows-py310-local",
    }
    assert contract.OVERALL_STATUSES == frozenset(
        {"PASS", "HOLD", "TEST_BUDGET_HOLD", "INVALID"}
    )
    assert contract.GATE_STATUSES == frozenset(
        {"PASS", "HOLD", "NOT_RUN", "TEST_BUDGET_HOLD", "INVALID"}
    )
    assert contract.BUDGET == {
        "change_loop_target_seconds": 600,
        "substage_target_seconds": 1200,
        "substage_hold_seconds": 2700,
        "terminal_target_seconds": 2700,
        "terminal_hold_seconds": 5400,
    }
    assert contract.PUBLIC_BASE == "82b58277d7a1fb167434be0290d3dfd2bb3588e2"
    assert contract.PROOF_INPUT_EXCLUDES == frozenset({STAGE_2_PLAN})
    assert contract.UMBRELLA_SHA256 == (
        "c79b1f2eb3a87917ba91045113ff9b2e529742824280439627cdc215eb1e3d25"
    )
    assert contract.SEMANTIC_CUBE_CANONICAL_TIP == (
        "bd4dee9c29ed0212dca64334c7acb2d49dfc58ae"
    )
    assert contract.SEMANTIC_CUBE_SHA256 == (
        "d9a7c374f45f9c57353615ae00dd60b0ff1f672c8ac9e8b26b826d1ae1a00c97"
    )
    assert contract.GATE_STATUS_REASON_CODES == {
        "PASS": frozenset({"command_passed", "covered_by_source_gate"}),
        "HOLD": frozenset(
            {
                "architecture_characterization_failed",
                "dependency_or_runtime_gate_failed",
                "full_suite_failed",
                "historical_schema_hold",
                "javascript_contract_failed",
                "manifest_failed",
                "package_build_failed",
                "package_smoke_failed",
                "privacy_contract_failed",
                "public_scope_invalid",
                "runtime_diff_detected",
                "scoped_static_failed",
                "stage_2_cumulative_failed",
            }
        ),
        "NOT_RUN": frozenset({"gate_not_run"}),
        "TEST_BUDGET_HOLD": frozenset(
            {"gate_timeout", "terminal_serial_budget_exhausted"}
        ),
        "INVALID": frozenset(
            {
                "evidence_commit_mismatch",
                "invalid_gate_evidence",
                "source_gate_invalid",
            }
        ),
    }
    assert contract.DERIVATION_SOURCES == {
        "installer-linux": frozenset({"linux-py310-full"}),
        "installer-windows": frozenset(
            {"windows-py312-full", "local-windows-py310-full"}
        ),
    }
    assert contract.QUALITY_GATE_IDS == frozenset(QUALITY_GATE_ORDER)
    assert contract.QUALITY_JOB_TIMEOUT_MINUTES == 25
    assert contract.QUALITY_GATE_TIMEOUT_SECONDS == {
        gate_id: 240 for gate_id in QUALITY_GATE_ORDER
    }
    assert contract.QUALITY_UPLOAD_MARGIN_SECONDS == 300


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["PASS", "PASS"], "PASS"),
        (["PASS", "HOLD"], "HOLD"),
        (["PASS", "NOT_RUN"], "HOLD"),
        (["HOLD", "TEST_BUDGET_HOLD"], "TEST_BUDGET_HOLD"),
        (["INVALID", "TEST_BUDGET_HOLD", "HOLD"], "INVALID"),
        (["PASS", "unknown"], "INVALID"),
    ],
)
def test_exact_status_precedence(statuses: list[str], expected: str) -> None:
    assert contract.overall_status(statuses) == expected


def test_bootstrap_workflow_has_exact_parallel_ownership_contract() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    _assert_workflow_contract(text)
    assert "AETHERSTATE_TERMINAL_OWNER: stage-1-bootstrap" in text
    assert "needs: package-build" in text
    assert "needs: [quality, python-tests, javascript, package-build, package-smoke]" in text
    assert text.count("python -m build --sdist --wheel") == 1
    assert text.count("node tests/creator_resource_contract.mjs") == 1
    assert "fail-fast: false" in text
    assert "if: always()" in text


def test_quality_lane_watchdogs_have_bounded_aggregate_upload_margin() -> None:
    section = _quality_workflow(WORKFLOW.read_text(encoding="utf-8"))
    timeout_match = re.search(r"runs-on: ubuntu-latest\s+timeout-minutes: (\d+)", section)
    assert timeout_match is not None
    job_seconds = int(timeout_match.group(1)) * 60
    gate_seconds: dict[str, int] = {}
    for gate_id in QUALITY_GATE_ORDER:
        match = re.search(
            rf"--gate-id {re.escape(gate_id)}\s+--timeout-seconds (\d+)",
            section,
        )
        assert match is not None
        gate_seconds[gate_id] = int(match.group(1))
    assert gate_seconds == contract.QUALITY_GATE_TIMEOUT_SECONDS
    assert (
        job_seconds - sum(gate_seconds.values())
        >= contract.QUALITY_UPLOAD_MARGIN_SECONDS
    )


def test_quality_lane_uploads_each_gate_immediately_under_unique_name() -> None:
    section = _quality_workflow(WORKFLOW.read_text(encoding="utf-8"))
    command_positions = [
        section.index(f"--gate-id {gate_id}") for gate_id in QUALITY_GATE_ORDER
    ]
    assert command_positions == sorted(command_positions)
    for index, gate_id in enumerate(QUALITY_GATE_ORDER):
        start = command_positions[index]
        end = (
            command_positions[index + 1]
            if index + 1 < len(command_positions)
            else len(section)
        )
        window = section[start:end]
        artifact_name = f"name: gate-quality-{gate_id}"
        evidence_path = f"path: build/hardening/gates/{gate_id}.json"
        assert f"--timeout-seconds {contract.QUALITY_GATE_TIMEOUT_SECONDS[gate_id]}" in window
        assert "if: always()" in window
        assert artifact_name in window
        assert evidence_path in window
        assert window.index(artifact_name) < window.index(evidence_path)
    assert section.count("uses: actions/upload-artifact@v4") == len(
        QUALITY_GATE_ORDER
    )
    assert "pattern: gate-*" in WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("stage-1-bootstrap", "unknown-owner", 1),
        lambda text: text.replace(
            "  AETHERSTATE_TERMINAL_OWNER: stage-1-bootstrap",
            "  AETHERSTATE_TERMINAL_OWNER: stage-1-bootstrap\n"
            "  AETHERSTATE_TERMINAL_OWNER: stage-1-bootstrap",
        ),
        lambda text: text.replace("  quality:", "  quality-removed:", 1),
        lambda text: text.replace("  stage-1-report:", "  stage-2-cumulative:", 1),
        lambda text: text.replace("  stage-1-report:", "  terminal-removed:", 1),
    ],
)
def test_workflow_contract_rejects_invalid_ownership_states(mutation) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_workflow_contract(mutation(text))


def test_transition_contract_accepts_only_the_canonical_stage_2_swap() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    stage_2 = text.replace("stage-1-bootstrap", "stage-2-cumulative", 1)
    start = stage_2.index("  # Bootstrap authority only.")
    replacement = """  # Canonical Stage 2 terminal authority.
  stage-2-cumulative:
    needs: [quality, python-tests, javascript, package-build, package-smoke]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ env.AETHERSTATE_CANDIDATE_SHA }}
      - run: >-
          python tools/run_bounded_gate.py
          --gate-id stage-2-cumulative
          --timeout-seconds 300
          --failure-reason stage_2_cumulative_failed
          --evidence build/hardening/stage-2/gates/stage-2-cumulative.json
          --
          python -c 'print("stage 2")'
"""
    stage_2 = stage_2[:start] + replacement

    _assert_workflow_contract(stage_2)

    with pytest.raises(AssertionError):
        _assert_workflow_contract(stage_2 + "\n# --gate-id no-runtime-diff\n")
    with pytest.raises(AssertionError):
        _assert_workflow_contract(
            stage_2.replace(
                "build/hardening/stage-2/gates/stage-2-cumulative.json",
                "build/hardening/gates/stage-2-cumulative.json",
            )
        )


def test_builder_emits_exact_sorted_pass_report_and_validator_accepts_it(
    tmp_path: Path,
) -> None:
    report_path = _pass_report(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert set(report) == {
        "schema",
        "stage",
        "status",
        "public_base",
        "evidence_commit",
        "evidence_tree",
        "proof_input_sha256",
        "contracts",
        "manifest",
        "gates",
        "diagnostics",
        "budget",
        "evidence_origin",
        "reason_code",
    }
    assert report["schema"] == "aetherstate-hardening-stage-report/1"
    assert report["stage"] == "stage-1-safety-baseline"
    assert report["status"] == "PASS"
    assert report["reason_code"] == "all_required_gates_passed"
    assert report["public_base"] == contract.PUBLIC_BASE
    assert report["evidence_commit"] == _head()
    assert report["evidence_tree"] == _git(ROOT, "rev-parse", "HEAD^{tree}")
    assert re.fullmatch(r"[0-9a-f]{64}", report["proof_input_sha256"])
    assert report["contracts"] == {
        "umbrella_sha256": contract.UMBRELLA_SHA256,
        "semantic_cube_canonical_tip": contract.SEMANTIC_CUBE_CANONICAL_TIP,
        "semantic_cube_sha256": contract.SEMANTIC_CUBE_SHA256,
    }
    assert report["manifest"]["path"] == contract.MANIFEST_PATH
    assert report["manifest"]["sha256"] == hashlib.sha256(
        (ROOT / contract.MANIFEST_PATH).read_bytes()
    ).hexdigest()
    assert [gate["id"] for gate in report["gates"]] == sorted(EXPECTED_GATES)
    assert len(report["gates"]) == 16
    assert report["diagnostics"] == []
    assert report["budget"] == contract.BUDGET

    result = _validate(report_path, require_pass=True)
    assert result.returncode == 0
    assert result.stdout.strip() == f"PASS stage-1-safety-baseline {_head()}"


def test_report_is_content_free_and_contains_no_absolute_or_payload_fields(
    tmp_path: Path,
) -> None:
    report_path = _pass_report(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    serialized = json.dumps(report).lower()

    assert str(ROOT).lower() not in serialized
    for forbidden in (
        "exception",
        "traceback",
        "credential",
        "password",
        "request_body",
        "response_body",
        "payload",
        "command_line",
    ):
        assert forbidden not in serialized


def test_missing_required_evidence_becomes_not_run_and_require_pass_blocks(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    for gate_id in sorted(EXPECTED_GATES - {"privacy"}):
        _write_evidence(evidence, gate_id, commit=commit)

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "HOLD"
    privacy = next(gate for gate in report["gates"] if gate["id"] == "privacy")
    assert privacy == {
        "id": "privacy",
        "status": "NOT_RUN",
        "elapsed_seconds": 0.0,
        "reason_code": "gate_not_run",
        "evidence_commit": commit,
    }
    structural = _validate(report_path)
    assert structural.returncode == 0
    assert structural.stdout.strip() == (
        "HOLD stage-1-safety-baseline required_gate_not_run"
    )
    required = _validate(report_path, require_pass=True)
    assert required.returncode != 0
    assert required.stdout.strip() == structural.stdout.strip()


@pytest.mark.parametrize(
    ("gate_status", "gate_reason", "overall"),
    [
        ("HOLD", "privacy_contract_failed", "HOLD"),
        ("TEST_BUDGET_HOLD", "gate_timeout", "TEST_BUDGET_HOLD"),
        ("INVALID", "source_gate_invalid", "INVALID"),
        ("unknown", "command_passed", "INVALID"),
    ],
)
def test_builder_derives_all_non_pass_precedence_and_never_accepts_a_claim(
    tmp_path: Path,
    gate_status: str,
    gate_reason: str,
    overall: str,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    _write_evidence(
        evidence,
        "privacy",
        commit=commit,
        status=gate_status,
        reason_code=gate_reason,
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == overall
    assert report["status"] != "PASS"


def test_builder_rejects_direct_pass_with_timeout_reason(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    _write_evidence(
        evidence,
        "privacy",
        commit=commit,
        status="PASS",
        reason_code="gate_timeout",
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert _report_gate(report, "privacy")["status"] == "INVALID"


def test_builder_rejects_non_finite_elapsed_seconds(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    _write_evidence(
        evidence,
        "privacy",
        commit=commit,
        elapsed=float("inf"),
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert _report_gate(report, "privacy")["status"] == "INVALID"
    assert _validate(report_path).returncode == 0


@pytest.mark.parametrize(
    ("target_gate", "source_gate"),
    [
        ("installer-linux", "linux-py310-full"),
        ("installer-windows", "windows-py312-full"),
        ("installer-windows", "local-windows-py310-full"),
    ],
)
def test_builder_and_validator_accept_only_exact_direct_derivation_sources(
    tmp_path: Path,
    target_gate: str,
    source_gate: str,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    if source_gate in contract.LOCAL_DIAGNOSTIC_GATES:
        _write_evidence(evidence, source_gate, commit=commit)
    _write_evidence(
        evidence,
        target_gate,
        commit=commit,
        status="PASS",
        reason_code="covered_by_source_gate",
        source_gate=source_gate,
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert _report_gate(report, target_gate)["source_gate"] == source_gate
    assert _validate(report_path, require_pass=True).returncode == 0


@pytest.mark.parametrize(
    ("source_gate", "remove_source"),
    [
        ("javascript", False),
        ("local-public-scope", False),
        ("linux-py310-full", True),
    ],
)
def test_builder_requires_present_exact_source_for_derived_pass(
    tmp_path: Path,
    source_gate: str,
    remove_source: bool,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    if source_gate == "local-public-scope":
        _write_evidence(evidence, source_gate, commit=commit)
    if remove_source:
        (evidence / f"{source_gate}.json").unlink()
    _write_evidence(
        evidence,
        "installer-linux",
        commit=commit,
        status="PASS",
        reason_code="covered_by_source_gate",
        source_gate=source_gate,
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert _report_gate(report, "installer-linux")["status"] == "INVALID"


@pytest.mark.parametrize(
    ("gate_id", "reason_code", "source_gate"),
    [
        ("privacy", "command_passed", None),
        ("installer-linux", "covered_by_source_gate", "linux-py310-full"),
    ],
)
def test_builder_normalizes_mismatched_commit_to_valid_invalid_row(
    tmp_path: Path,
    gate_id: str,
    reason_code: str,
    source_gate: str | None,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    previous_commit = _git(ROOT, "rev-parse", "HEAD^")
    _write_all_pass(evidence, commit=commit)
    _write_evidence(
        evidence,
        gate_id,
        commit=previous_commit,
        status="PASS",
        reason_code=reason_code,
        source_gate=source_gate,
    )

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert _report_gate(report, gate_id) == {
        "id": gate_id,
        "status": "INVALID",
        "elapsed_seconds": 0.0,
        "reason_code": "evidence_commit_mismatch",
        "evidence_commit": commit,
    }
    structural = _validate(report_path)
    assert structural.returncode == 0
    assert structural.stdout.strip() == (
        "INVALID stage-1-safety-baseline evidence_commit_mismatch"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: _report_gate(report, "privacy").update(
            {"reason_code": "gate_timeout"}
        ),
        lambda report: _report_gate(report, "privacy").update(
            {"elapsed_seconds": float("inf")}
        ),
        lambda report: _report_gate(report, "installer-linux").update(
            {"source_gate": "javascript"}
        ),
        lambda report: _report_gate(report, "installer-linux").update(
            {"source_gate": "stage-2-cumulative"}
        ),
        lambda report: _report_gate(report, "installer-linux").update(
            {"source_gate": "local-public-scope"}
        ),
        lambda report: _report_gate(report, "linux-py310-full").update(
            {
                "reason_code": "covered_by_source_gate",
                "source_gate": "javascript",
            }
        ),
    ],
)
def test_validator_rejects_impossible_pass_reason_or_forged_derivation(
    tmp_path: Path,
    mutation,
) -> None:
    original_path = _pass_report(tmp_path)
    report = json.loads(original_path.read_text(encoding="utf-8"))
    mutation(report)
    forged = tmp_path / "forged-report.json"
    forged.write_text(json.dumps(report), encoding="utf-8")

    result = _validate(forged, require_pass=True)

    assert result.returncode != 0
    assert result.stdout.strip() == "INVALID stage-1-safety-baseline invalid_report"


@pytest.mark.parametrize(
    ("diagnostic_status", "reason", "overall"),
    [
        ("PASS", "command_passed", "PASS"),
        ("HOLD", "public_scope_invalid", "HOLD"),
        ("TEST_BUDGET_HOLD", "terminal_serial_budget_exhausted", "TEST_BUDGET_HOLD"),
        ("INVALID", "source_gate_invalid", "INVALID"),
    ],
)
def test_local_diagnostics_participate_in_precedence_but_never_fill_a_gate(
    tmp_path: Path,
    diagnostic_status: str,
    reason: str,
    overall: str,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    _write_evidence(
        evidence,
        "local-public-scope",
        commit=commit,
        status=diagnostic_status,
        reason_code=reason,
    )
    built, report_path = _build(tmp_path, evidence_dir=evidence)
    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == overall
    assert [row["id"] for row in report["diagnostics"]] == ["local-public-scope"]

    if diagnostic_status == "PASS":
        (evidence / "privacy.json").unlink()
        built, report_path = _build(tmp_path, evidence_dir=evidence)
        assert built.returncode == 0
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "HOLD"
        assert next(row for row in report["gates"] if row["id"] == "privacy")[
            "status"
        ] == "NOT_RUN"


def test_local_complete_subset_reports_cross_platform_pending(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    for gate_id in sorted(contract.LOCALLY_RUNNABLE_STAGE_1_GATES):
        _write_evidence(evidence, gate_id, commit=commit)

    built, report_path = _build(
        tmp_path,
        evidence_origin="local-windows",
        evidence_dir=evidence,
    )

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "HOLD"
    assert report["reason_code"] == "cross_platform_ci_pending"


def test_unknown_or_handoff_evidence_in_stage_1_directory_yields_invalid_report(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence"
    commit = _head()
    _write_all_pass(evidence, commit=commit)
    _write_evidence(evidence, "stage-2-cumulative", commit=commit)

    built, report_path = _build(tmp_path, evidence_dir=evidence)

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert report["reason_code"] == "unknown_gate_evidence"
    assert len(report["gates"]) == 16
    assert "stage-2-cumulative" not in {row["id"] for row in report["gates"]}


def test_validator_rejects_changed_manifest_fingerprint_design_or_gate_shape(
    tmp_path: Path,
) -> None:
    original_path = _pass_report(tmp_path)
    original = json.loads(original_path.read_text(encoding="utf-8"))
    mutations = []
    changed_manifest = copy.deepcopy(original)
    changed_manifest["manifest"]["sha256"] = "0" * 64
    mutations.append(changed_manifest)
    changed_fingerprint = copy.deepcopy(original)
    changed_fingerprint["proof_input_sha256"] = "0" * 64
    mutations.append(changed_fingerprint)
    changed_umbrella = copy.deepcopy(original)
    changed_umbrella["contracts"]["umbrella_sha256"] = "0" * 64
    mutations.append(changed_umbrella)
    changed_cube = copy.deepcopy(original)
    changed_cube["contracts"]["semantic_cube_sha256"] = "0" * 64
    mutations.append(changed_cube)
    wrong_tree = copy.deepcopy(original)
    wrong_tree["evidence_tree"] = "0" * 40
    mutations.append(wrong_tree)
    missing_gate = copy.deepcopy(original)
    missing_gate["gates"].pop()
    mutations.append(missing_gate)
    duplicate_gate = copy.deepcopy(original)
    duplicate_gate["gates"].append(copy.deepcopy(duplicate_gate["gates"][0]))
    mutations.append(duplicate_gate)
    unknown_gate = copy.deepcopy(original)
    unknown_gate["gates"][0]["id"] = "unknown"
    mutations.append(unknown_gate)

    for index, report in enumerate(mutations):
        path = tmp_path / f"mutated-{index}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        result = _validate(path)
        assert result.returncode != 0
        assert result.stdout.strip().startswith("INVALID stage-1-safety-baseline ")
        assert str(ROOT) not in result.stdout + result.stderr


def test_proof_fingerprint_changes_for_every_tracked_input_except_stage_2_plan(
    tmp_path: Path,
) -> None:
    repo = _clone(tmp_path)
    baseline = contract.proof_input_sha256(repo, "HEAD")
    _commit_file(repo, STAGE_2_PLAN, "stage 2 plan\n", "allowed excluded plan")
    assert contract.proof_input_sha256(repo, "HEAD") == baseline

    for relative in (
        "docs/superpowers/plans/2026-07-24-post-1.24-stage-1-safety-baseline.md",
        "docs/superpowers/specs/2026-07-24-post-1.24-engineering-hardening-design.md",
        "docs/superpowers/specs/2026-07-24-semantic-cube-narrator-output-integrity-design.md",
        "tests/fixtures/hardening/behavior-surface-ids-1.24.0.txt",
    ):
        before = contract.proof_input_sha256(repo, "HEAD")
        path = repo / relative
        _commit_file(repo, relative, path.read_text(encoding="utf-8") + "\nproof change\n")
        assert contract.proof_input_sha256(repo, "HEAD") != before


def test_proof_fingerprint_is_independent_of_checkout_eol_config(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    windows_checkout = contract.proof_input_sha256(repo, "HEAD")
    _git(repo, "config", "core.autocrlf", "input")
    linux_checkout = contract.proof_input_sha256(repo, "HEAD")

    assert windows_checkout == linux_checkout


def test_only_exact_stage_2_plan_descendant_can_reuse_a_pass_report(
    tmp_path: Path,
) -> None:
    repo = _clone(tmp_path)
    report = _pass_report(tmp_path, cwd=repo)
    evidence_commit = json.loads(report.read_text(encoding="utf-8"))["evidence_commit"]
    _commit_file(repo, STAGE_2_PLAN, "canonical stage 2 plan\n", "stage 2 plan")

    accepted = _validate(report, cwd=repo, require_pass=True)

    assert accepted.returncode == 0
    assert accepted.stdout.strip() == (
        f"PASS stage-1-safety-baseline {evidence_commit}"
    )


def test_post_evidence_change_and_revert_cannot_hide_behind_same_tree(
    tmp_path: Path,
) -> None:
    repo = _clone(tmp_path)
    report = _pass_report(tmp_path, cwd=repo)
    relative = "tools/stage_gate_contract.py"
    original = (repo / relative).read_text(encoding="utf-8")
    _commit_file(repo, relative, original + "\n# temporary change\n", "change proof input")
    _commit_file(repo, relative, original, "revert proof input")

    rejected = _validate(report, cwd=repo, require_pass=True)

    assert rejected.returncode != 0
    assert rejected.stdout.strip() == (
        "INVALID stage-1-safety-baseline public_history_invalid"
    )


def test_merge_commit_after_evidence_is_rejected_even_with_allowed_final_tree(
    tmp_path: Path,
) -> None:
    repo = _clone(tmp_path)
    report = _pass_report(tmp_path, cwd=repo)
    base_commit = _head(repo)
    _git(repo, "checkout", "-b", "side")
    _commit_file(repo, STAGE_2_PLAN, "canonical stage 2 plan\n", "side plan")
    _git(repo, "checkout", "--detach", base_commit)
    _git(repo, "commit", "--allow-empty", "-m", "main empty")
    _git(repo, "merge", "--no-ff", "side", "-m", "import side")

    rejected = _validate(report, cwd=repo)

    assert rejected.returncode != 0
    assert rejected.stdout.strip() == (
        "INVALID stage-1-safety-baseline public_history_invalid"
    )


def test_builder_marks_commit_outside_stage_1_allowlist_invalid(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    _commit_file(
        repo,
        "README.md",
        (repo / "README.md").read_text(encoding="utf-8") + "\noutside stage 1\n",
        "outside path",
    )
    evidence = tmp_path / "bad-evidence"
    _write_all_pass(evidence, commit=_head(repo))

    built, report_path = _build(
        tmp_path,
        cwd=repo,
        evidence_dir=evidence,
    )

    assert built.returncode == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "INVALID"
    assert report["reason_code"] == "public_history_invalid"


def test_report_evidence_commit_must_be_ancestral_to_candidate(tmp_path: Path) -> None:
    repo = _clone(tmp_path)
    base_commit = _head(repo)
    _git(repo, "checkout", "-b", "evidence-side")
    _commit_file(repo, STAGE_2_PLAN, "side evidence\n", "side evidence")
    report = _pass_report(tmp_path, cwd=repo)
    _git(repo, "checkout", "--detach", base_commit)
    _git(repo, "commit", "--allow-empty", "-m", "candidate side")

    rejected = _validate(report, cwd=repo)

    assert rejected.returncode != 0
    assert rejected.stdout.strip() == (
        "INVALID stage-1-safety-baseline evidence_not_ancestor"
    )


def test_public_safe_history_mode_is_content_free_and_detects_bad_history(
    tmp_path: Path,
) -> None:
    repo = _clone(tmp_path)
    passed = _tool(
        VALIDATOR,
        ["--check-public-safe-history", "--candidate", "HEAD"],
        cwd=repo,
    )
    assert passed.returncode == 0
    assert passed.stdout.strip() == f"PASS public-safe-history {_head(repo)}"

    _commit_file(
        repo,
        "README.md",
        (repo / "README.md").read_text(encoding="utf-8") + "\nunsafe\n",
        "unsafe",
    )
    failed = _tool(
        VALIDATOR,
        ["--check-public-safe-history", "--candidate", "HEAD"],
        cwd=repo,
    )
    assert failed.returncode != 0
    assert failed.stdout.strip() == "INVALID public-safe-history public_history_invalid"
