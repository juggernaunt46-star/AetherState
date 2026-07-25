from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence, cast

from stage_gate_contract import (
    ALL_GATE_IDS,
    BUDGET,
    GATE_STATUSES,
    LOCAL_DIAGNOSTIC_GATES,
    MANIFEST_PATH,
    OVERALL_STATUSES,
    PUBLIC_BASE,
    REQUIRED_STAGE_1_GATES,
    SEMANTIC_CUBE_CANONICAL_TIP,
    SEMANTIC_CUBE_PATH,
    SEMANTIC_CUBE_SHA256,
    STABLE_REASON_CODES,
    STAGE,
    STAGE_REPORT_SCHEMA,
    UMBRELLA_PATH,
    UMBRELLA_SHA256,
    ContractError,
    candidate_file_sha256,
    check_public_safe_history,
    is_ancestor,
    proof_input_sha256,
    report_reason,
    resolve_commit,
    tree_for_commit,
)

COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
REPORT_FIELDS = frozenset(
    {
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
)
ROW_FIELDS = frozenset(
    {"id", "status", "elapsed_seconds", "reason_code", "evidence_commit"}
)
DERIVED_ROW_FIELDS = ROW_FIELDS | {"source_gate"}
FORCED_INVALID_REASONS = frozenset(
    {"unknown_gate_evidence", "public_history_invalid", "invalid_report"}
)


class ReportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str = "invalid_report") -> None:
    if not condition:
        raise ReportError(code)


def _validate_row(
    value: object,
    *,
    allowed_ids: set[str],
    evidence_commit: str,
) -> Mapping[str, object]:
    _require(isinstance(value, dict))
    row = cast(dict[str, object], value)
    fields = frozenset(row)
    _require(fields in {ROW_FIELDS, DERIVED_ROW_FIELDS})
    _require(row.get("id") in allowed_ids)
    _require(row.get("status") in GATE_STATUSES)
    elapsed = row.get("elapsed_seconds")
    _require(
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and elapsed >= 0
        and round(float(elapsed), 3) == elapsed
    )
    _require(row.get("reason_code") in STABLE_REASON_CODES)
    _require(row.get("evidence_commit") == evidence_commit)
    if "source_gate" in row:
        _require(
            row.get("status") == "PASS"
            and row.get("reason_code") == "covered_by_source_gate"
            and row.get("source_gate") in ALL_GATE_IDS
        )
    else:
        _require(row.get("reason_code") != "covered_by_source_gate")
    return row


def _validate_rows(
    report: Mapping[str, object],
    evidence_commit: str,
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    gate_values = report.get("gates")
    diagnostic_values = report.get("diagnostics")
    _require(isinstance(gate_values, list))
    _require(isinstance(diagnostic_values, list))
    gate_list = cast(list[object], gate_values)
    diagnostic_list = cast(list[object], diagnostic_values)
    gates = [
        _validate_row(
            row,
            allowed_ids=REQUIRED_STAGE_1_GATES,
            evidence_commit=evidence_commit,
        )
        for row in gate_list
    ]
    diagnostics = [
        _validate_row(
            row,
            allowed_ids=LOCAL_DIAGNOSTIC_GATES,
            evidence_commit=evidence_commit,
        )
        for row in diagnostic_list
    ]
    gate_ids = [str(row["id"]) for row in gates]
    diagnostic_ids = [str(row["id"]) for row in diagnostics]
    _require(gate_ids == sorted(REQUIRED_STAGE_1_GATES))
    _require(
        diagnostic_ids == sorted(diagnostic_ids)
        and len(diagnostic_ids) == len(set(diagnostic_ids))
    )
    return gates, diagnostics


def _load_report(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError("invalid_report") from exc
    _require(isinstance(value, dict))
    _require(frozenset(value) == REPORT_FIELDS)
    return value


def _validate_report(
    repo: Path,
    report: Mapping[str, object],
    *,
    requested_stage: str,
    candidate: str,
) -> tuple[str, str, str]:
    _require(requested_stage == STAGE)
    _require(report.get("schema") == STAGE_REPORT_SCHEMA)
    _require(report.get("stage") == STAGE)
    _require(report.get("status") in OVERALL_STATUSES)
    _require(report.get("public_base") == PUBLIC_BASE)
    evidence_commit = report.get("evidence_commit")
    evidence_tree = report.get("evidence_tree")
    fingerprint = report.get("proof_input_sha256")
    _require(
        isinstance(evidence_commit, str)
        and COMMIT_RE.fullmatch(evidence_commit) is not None
    )
    _require(
        isinstance(evidence_tree, str)
        and COMMIT_RE.fullmatch(evidence_tree) is not None
    )
    _require(
        isinstance(fingerprint, str)
        and DIGEST_RE.fullmatch(fingerprint) is not None
    )
    evidence_commit = cast(str, evidence_commit)
    evidence_tree = cast(str, evidence_tree)
    fingerprint = cast(str, fingerprint)

    candidate_commit = resolve_commit(repo, candidate)
    try:
        resolved_evidence = resolve_commit(repo, evidence_commit)
    except ContractError as exc:
        raise ReportError("evidence_not_ancestor") from exc
    if not is_ancestor(repo, resolved_evidence, candidate_commit):
        raise ReportError("evidence_not_ancestor")
    _require(
        tree_for_commit(repo, resolved_evidence) == evidence_tree,
        "evidence_tree_mismatch",
    )
    history_error: str | None = None
    try:
        check_public_safe_history(
            repo,
            candidate_commit,
            evidence_commit=resolved_evidence,
        )
    except ContractError as exc:
        history_error = exc.code
    if history_error is not None:
        if not (
            report.get("status") == "INVALID"
            and report.get("reason_code") == history_error
        ):
            raise ReportError(history_error)

    _require(
        proof_input_sha256(repo, candidate_commit) == fingerprint,
        "proof_input_changed",
    )
    contracts = report.get("contracts")
    _require(
        contracts
        == {
            "umbrella_sha256": UMBRELLA_SHA256,
            "semantic_cube_canonical_tip": SEMANTIC_CUBE_CANONICAL_TIP,
            "semantic_cube_sha256": SEMANTIC_CUBE_SHA256,
        }
    )
    _require(
        candidate_file_sha256(repo, candidate_commit, UMBRELLA_PATH)
        == UMBRELLA_SHA256
    )
    _require(
        candidate_file_sha256(repo, candidate_commit, SEMANTIC_CUBE_PATH)
        == SEMANTIC_CUBE_SHA256
    )
    manifest = report.get("manifest")
    _require(
        isinstance(manifest, dict)
        and frozenset(manifest) == {"path", "sha256"}
        and manifest.get("path") == MANIFEST_PATH
        and isinstance(manifest.get("sha256"), str)
        and DIGEST_RE.fullmatch(str(manifest.get("sha256"))) is not None
    )
    manifest = cast(dict[str, object], manifest)
    _require(
        candidate_file_sha256(repo, candidate_commit, MANIFEST_PATH)
        == manifest["sha256"],
        "manifest_changed",
    )
    _require(report.get("budget") == BUDGET)
    evidence_origin = report.get("evidence_origin")
    _require(isinstance(evidence_origin, str))
    evidence_origin = cast(str, evidence_origin)
    _require(
        evidence_origin == "local-windows"
        or re.fullmatch(r"github-actions:[0-9]+:[0-9]+", evidence_origin)
        is not None
    )
    _require(report.get("reason_code") in STABLE_REASON_CODES)
    gates, diagnostics = _validate_rows(report, resolved_evidence)
    status = str(report["status"])
    reason = str(report["reason_code"])
    if status == "INVALID" and reason in FORCED_INVALID_REASONS:
        pass
    else:
        expected_status, expected_reason = report_reason(
            gates,
            diagnostics,
            evidence_origin=evidence_origin,
        )
        _require(status == expected_status)
        _require(reason == expected_reason)
    return status, reason, resolved_evidence


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--stage")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--check-public-safe-history", action="store_true")
    return parser.parse_args(argv)


def _invalid(scope: str, code: str) -> int:
    print(f"INVALID {scope} {code}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    repo = Path.cwd()
    if args.check_public_safe_history:
        if args.report is not None or args.stage is not None or args.require_pass:
            return _invalid("public-safe-history", "invalid_arguments")
        try:
            candidate = check_public_safe_history(repo, args.candidate)
        except ContractError as exc:
            return _invalid("public-safe-history", exc.code)
        print(f"PASS public-safe-history {candidate}")
        return 0
    if args.report is None or args.stage is None:
        return _invalid(STAGE, "invalid_arguments")
    try:
        report = _load_report(args.report)
        status, reason, evidence_commit = _validate_report(
            repo,
            report,
            requested_stage=args.stage,
            candidate=args.candidate,
        )
    except (ReportError, ContractError, OSError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, (ReportError, ContractError)) else "invalid_report"
        return _invalid(STAGE, code)
    if status == "PASS":
        print(f"PASS {STAGE} {evidence_commit}")
        return 0
    print(f"{status} {STAGE} {reason}")
    return 1 if args.require_pass else 0


if __name__ == "__main__":
    raise SystemExit(main())
