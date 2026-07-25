from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from pathlib import Path
from typing import Iterable, Mapping

GATE_EVIDENCE_SCHEMA = "aetherstate-hardening-gate-evidence/1"
STAGE_REPORT_SCHEMA = "aetherstate-hardening-stage-report/1"
STAGE = "stage-1-safety-baseline"
PUBLIC_BASE = "82b58277d7a1fb167434be0290d3dfd2bb3588e2"

REQUIRED_STAGE_1_GATES = {
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

SHARED_CI_JOB_IDS = frozenset(
    {"quality", "python-tests", "javascript", "package-build", "package-smoke"}
)
TERMINAL_OWNERSHIP_STATES = frozenset(
    {"stage-1-bootstrap", "stage-2-cumulative"}
)
HANDOFF_GATE_IDS = frozenset({"stage-2-cumulative"})
LOCAL_DIAGNOSTIC_GATES = {
    "local-public-scope",
    "local-terminal-budget",
    "local-windows-py310-full",
    "package-windows-py310-local",
}
LOCALLY_RUNNABLE_STAGE_1_GATES = frozenset(
    {
        "architecture-characterization",
        "historical-schema",
        "installer-windows",
        "javascript",
        "manifest",
        "no-runtime-diff",
        "privacy",
        "scoped-static",
    }
)
ALL_GATE_IDS = frozenset(
    REQUIRED_STAGE_1_GATES | LOCAL_DIAGNOSTIC_GATES | set(HANDOFF_GATE_IDS)
)

OVERALL_STATUSES = frozenset({"PASS", "HOLD", "TEST_BUDGET_HOLD", "INVALID"})
GATE_STATUSES = frozenset(
    {"PASS", "HOLD", "NOT_RUN", "TEST_BUDGET_HOLD", "INVALID"}
)
STATUS_PRECEDENCE = {
    "PASS": 0,
    "NOT_RUN": 1,
    "HOLD": 1,
    "TEST_BUDGET_HOLD": 2,
    "INVALID": 3,
}

BUDGET = {
    "change_loop_target_seconds": 600,
    "substage_target_seconds": 1200,
    "substage_hold_seconds": 2700,
    "terminal_target_seconds": 2700,
    "terminal_hold_seconds": 5400,
}

UMBRELLA_PATH = (
    "docs/superpowers/specs/2026-07-24-post-1.24-engineering-hardening-design.md"
)
UMBRELLA_SHA256 = (
    "c79b1f2eb3a87917ba91045113ff9b2e529742824280439627cdc215eb1e3d25"
)
SEMANTIC_CUBE_PATH = (
    "docs/superpowers/specs/"
    "2026-07-24-semantic-cube-narrator-output-integrity-design.md"
)
SEMANTIC_CUBE_CANONICAL_TIP = "bd4dee9c29ed0212dca64334c7acb2d49dfc58ae"
SEMANTIC_CUBE_SHA256 = (
    "d9a7c374f45f9c57353615ae00dd60b0ff1f672c8ac9e8b26b826d1ae1a00c97"
)
MANIFEST_PATH = "docs/hardening/post-1.24/behavior-player-surface-manifest.json"

PROOF_INPUT_EXCLUDES = frozenset(
    {
        "docs/superpowers/plans/"
        "2026-07-24-semantic-cube-narrator-output-integrity.md",
    }
)
STAGE_1_ALLOWED_PREFIXES = (
    "docs/hardening/post-1.24/",
    "tests/fixtures/hardening/",
)
STAGE_1_ALLOWED_EXACT_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "docs/superpowers/plans/2026-07-24-post-1.24-stage-1-safety-baseline.md",
        "docs/superpowers/plans/2026-07-24-semantic-cube-narrator-output-integrity.md",
        "docs/superpowers/specs/2026-07-24-post-1.24-engineering-hardening-design.md",
        "docs/superpowers/specs/2026-07-24-semantic-cube-narrator-output-integrity-design.md",
        "tests/test_architecture_baseline.py",
        "tests/test_behavior_surface_manifest.py",
        "tests/test_bounded_gate.py",
        "tests/test_clean_wheel_smoke.py",
        "tests/test_hardening_characterization.py",
        "tests/test_historical_schema_inventory.py",
        "tests/test_installer_contracts.py",
        "tests/test_stage_gate_contract.py",
        "tools/build_stage_gate_report.py",
        "tools/capture_architecture_baseline.py",
        "tools/capture_public_routes.py",
        "tools/capture_schema_contract.py",
        "tools/run_bounded_gate.py",
        "tools/smoke_clean_wheel.py",
        "tools/stage_gate_contract.py",
        "tools/validate_behavior_manifest.py",
        "tools/validate_stage_gate.py",
    }
)

STABLE_REASON_CODES = frozenset(
    {
        "all_required_gates_passed",
        "architecture_characterization_failed",
        "command_passed",
        "covered_by_source_gate",
        "cross_platform_ci_pending",
        "dependency_or_runtime_gate_failed",
        "evidence_commit_mismatch",
        "evidence_not_ancestor",
        "evidence_tree_mismatch",
        "full_suite_failed",
        "gate_not_run",
        "gate_timeout",
        "historical_schema_hold",
        "invalid_gate_evidence",
        "invalid_report",
        "javascript_contract_failed",
        "manifest_changed",
        "manifest_failed",
        "package_build_failed",
        "package_smoke_failed",
        "privacy_contract_failed",
        "proof_input_changed",
        "public_history_invalid",
        "public_scope_invalid",
        "required_gate_not_run",
        "runtime_diff_detected",
        "scoped_static_failed",
        "source_gate_invalid",
        "stage_2_cumulative_failed",
        "terminal_serial_budget_exhausted",
        "unknown_gate_evidence",
    }
)

DIRECT_EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "id",
        "status",
        "elapsed_seconds",
        "reason_code",
        "evidence_commit",
    }
)
DERIVED_EVIDENCE_FIELDS = DIRECT_EVIDENCE_FIELDS | {"source_gate"}


class ContractError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _git(repo: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContractError("public_history_invalid") from exc
    if text:
        return result.stdout.strip()
    return result.stdout


def resolve_commit(repo: Path, candidate: str) -> str:
    value = _git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}")
    assert isinstance(value, str)
    if len(value) != 40:
        raise ContractError("public_history_invalid")
    return value


def tree_for_commit(repo: Path, commit: str) -> str:
    value = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    assert isinstance(value, str)
    return value


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode not in {0, 1}:
        raise ContractError("public_history_invalid")
    return result.returncode == 0


def _path_is_stage_1_owned(path: str) -> bool:
    return path in STAGE_1_ALLOWED_EXACT_PATHS or path.startswith(
        STAGE_1_ALLOWED_PREFIXES
    )


def _linear_commits(repo: Path, start: str, end: str) -> list[str]:
    if not is_ancestor(repo, start, end):
        raise ContractError("public_history_invalid")
    output = _git(repo, "rev-list", "--reverse", "--parents", f"{start}..{end}")
    assert isinstance(output, str)
    commits: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ContractError("public_history_invalid")
        commits.append(fields[0])
    return commits


def _changed_paths(repo: Path, commit: str) -> list[str]:
    output = _git(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        commit,
    )
    assert isinstance(output, str)
    return [line for line in output.splitlines() if line]


def check_public_safe_history(
    repo: Path,
    candidate: str,
    *,
    evidence_commit: str | None = None,
) -> str:
    candidate_commit = resolve_commit(repo, candidate)
    if not is_ancestor(repo, PUBLIC_BASE, candidate_commit):
        raise ContractError("public_history_invalid")
    pre_end = candidate_commit
    if evidence_commit is not None:
        evidence_commit = resolve_commit(repo, evidence_commit)
        if not is_ancestor(repo, evidence_commit, candidate_commit):
            raise ContractError("evidence_not_ancestor")
        pre_end = evidence_commit
    for commit in _linear_commits(repo, PUBLIC_BASE, pre_end):
        if any(not _path_is_stage_1_owned(path) for path in _changed_paths(repo, commit)):
            raise ContractError("public_history_invalid")
    if evidence_commit is not None:
        for commit in _linear_commits(repo, evidence_commit, candidate_commit):
            if any(path not in PROOF_INPUT_EXCLUDES for path in _changed_paths(repo, commit)):
                raise ContractError("public_history_invalid")
    return candidate_commit


def blob_bytes(repo: Path, candidate: str, path: str) -> bytes:
    value = _git(repo, "show", f"{candidate}:{path}", text=False)
    assert isinstance(value, bytes)
    return value


def tracked_paths(repo: Path, candidate: str) -> list[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", candidate)
    assert isinstance(output, str)
    return sorted(
        path
        for path in output.splitlines()
        if path and path not in PROOF_INPUT_EXCLUDES
    )


def proof_input_sha256(repo: Path, candidate: str) -> str:
    commit = resolve_commit(repo, candidate)
    archive = _git(repo, "archive", "--format=tar", commit, text=False)
    assert isinstance(archive, bytes)
    digest = hashlib.sha256()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            members = sorted(stream.getmembers(), key=lambda member: member.name)
            for member in members:
                path = member.name.rstrip("/")
                if not path or path in PROOF_INPUT_EXCLUDES or member.isdir():
                    continue
                extracted = stream.extractfile(member)
                if extracted is None:
                    raise ContractError("public_history_invalid")
                digest.update(path.encode("utf-8"))
                digest.update(b"\0")
                digest.update(extracted.read())
                digest.update(b"\0")
    except (tarfile.TarError, OSError) as exc:
        raise ContractError("public_history_invalid") from exc
    return digest.hexdigest()


def candidate_file_sha256(repo: Path, candidate: str, path: str) -> str:
    commit = resolve_commit(repo, candidate)
    return hashlib.sha256(blob_bytes(repo, commit, path)).hexdigest()


def overall_status(statuses: Iterable[str]) -> str:
    highest = 0
    for status in statuses:
        if status not in GATE_STATUSES:
            return "INVALID"
        highest = max(highest, STATUS_PRECEDENCE[status])
    return {0: "PASS", 1: "HOLD", 2: "TEST_BUDGET_HOLD", 3: "INVALID"}[highest]


def report_reason(
    gates: Iterable[Mapping[str, object]],
    diagnostics: Iterable[Mapping[str, object]],
    *,
    evidence_origin: str,
    forced_reason: str | None = None,
) -> tuple[str, str]:
    gate_rows = list(gates)
    diagnostic_rows = list(diagnostics)
    rows = gate_rows + diagnostic_rows
    status = overall_status(str(row.get("status", "")) for row in rows)
    if forced_reason is not None:
        return "INVALID", forced_reason
    if status == "INVALID":
        row = next(
            (
                item
                for item in rows
                if item.get("status") not in GATE_STATUSES
                or item.get("status") == "INVALID"
            ),
            None,
        )
        reason = str(row.get("reason_code")) if row else "invalid_gate_evidence"
        return status, reason if reason in STABLE_REASON_CODES else "invalid_gate_evidence"
    if status == "TEST_BUDGET_HOLD":
        row = next(item for item in rows if item.get("status") == status)
        reason = str(row.get("reason_code"))
        return status, reason if reason in STABLE_REASON_CODES else "gate_timeout"
    if status == "HOLD":
        actual_hold = next(
            (item for item in rows if item.get("status") == "HOLD"),
            None,
        )
        if actual_hold is not None:
            reason = str(actual_hold.get("reason_code"))
            return status, reason if reason in STABLE_REASON_CODES else "invalid_gate_evidence"
        passed = {
            str(item.get("id"))
            for item in gate_rows
            if item.get("status") == "PASS"
        }
        not_run = {
            str(item.get("id"))
            for item in gate_rows
            if item.get("status") == "NOT_RUN"
        }
        if (
            evidence_origin == "local-windows"
            and passed == set(LOCALLY_RUNNABLE_STAGE_1_GATES)
            and not_run
            == REQUIRED_STAGE_1_GATES - set(LOCALLY_RUNNABLE_STAGE_1_GATES)
        ):
            return status, "cross_platform_ci_pending"
        return status, "required_gate_not_run"
    return "PASS", "all_required_gates_passed"
