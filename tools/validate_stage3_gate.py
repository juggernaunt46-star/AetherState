from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Mapping, Sequence, cast

from stage_gate_contract import (
    BUDGET,
    GATE_STATUSES,
    LOCALLY_RUNNABLE_STAGE_3_GATES,
    MANIFEST_PATH,
    OVERALL_STATUSES,
    PUBLIC_BASE,
    REQUIRED_STAGE_3_GATES,
    SEMANTIC_CUBE_CANONICAL_TIP,
    SEMANTIC_CUBE_PATH,
    SEMANTIC_CUBE_SHA256,
    STABLE_REASON_CODES,
    STAGE_3,
    STAGE_3_MERGE_TARGET,
    STAGE_REPORT_SCHEMA,
    UMBRELLA_PATH,
    UMBRELLA_SHA256,
    ContractError,
    candidate_file_sha256,
    derivation_source_row_is_valid,
    elapsed_seconds_is_valid,
    gate_status_reason_is_valid,
    is_ancestor,
    proof_input_sha256,
    report_reason,
    resolve_commit,
    tree_for_commit,
)

COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
SQL_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|PRAGMA|ALTER\s+TABLE|"
    r"CREATE\s+TABLE|DROP\s+TABLE)\b",
    re.IGNORECASE,
)
EXCEPTION_RE = re.compile(
    r"(?:Traceback\s+\(most recent call last\)|"
    r"\b[A-Za-z][A-Za-z0-9_]*(?:Error|Exception):)",
)
CONTENT_RE = re.compile(
    r"\b(?:provider|story|row_content|credential|prompt|response_body)\b",
    re.IGNORECASE,
)
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
    {"unknown_gate_evidence", "evidence_not_ancestor", "invalid_report"}
)


class ReportError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str = "invalid_report") -> None:
    if not condition:
        raise ReportError(code)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _content_is_safe(report: Mapping[str, object]) -> bool:
    for value in _strings(report):
        if (
            PureWindowsPath(value).is_absolute()
            or PurePosixPath(value).is_absolute()
            or SQL_RE.search(value)
            or EXCEPTION_RE.search(value)
            or CONTENT_RE.search(value)
        ):
            return False
    return True


def _validate_row(
    value: object,
    *,
    evidence_commit: str,
) -> Mapping[str, object]:
    _require(isinstance(value, dict))
    row = cast(dict[str, object], value)
    fields = frozenset(row)
    _require(fields in {ROW_FIELDS, DERIVED_ROW_FIELDS})
    gate_id = row.get("id")
    status = row.get("status")
    reason_code = row.get("reason_code")
    _require(isinstance(gate_id, str) and gate_id in REQUIRED_STAGE_3_GATES)
    _require(isinstance(status, str) and status in GATE_STATUSES)
    _require(elapsed_seconds_is_valid(row.get("elapsed_seconds")))
    _require(isinstance(reason_code, str) and reason_code in STABLE_REASON_CODES)
    _require(row.get("evidence_commit") == evidence_commit)
    status_reason_valid = (
        gate_status_reason_is_valid(
            str(row["id"]),
            row.get("status"),
            row.get("reason_code"),
            row.get("source_gate"),
        )
        if "source_gate" in row
        else gate_status_reason_is_valid(
            str(row["id"]),
            row.get("status"),
            row.get("reason_code"),
        )
    )
    _require(status_reason_valid)
    return row


def _validate_rows(
    report: Mapping[str, object],
    evidence_commit: str,
) -> list[Mapping[str, object]]:
    gate_values = report.get("gates")
    _require(isinstance(gate_values, list))
    _require(report.get("diagnostics") == [])
    gates = [
        _validate_row(row, evidence_commit=evidence_commit)
        for row in cast(list[object], gate_values)
    ]
    _require([str(row["id"]) for row in gates] == list(REQUIRED_STAGE_3_GATES))
    rows_by_id = {str(row["id"]): row for row in gates}
    for row in gates:
        if "source_gate" in row:
            _require(
                derivation_source_row_is_valid(
                    str(row["id"]),
                    str(row["source_gate"]),
                    rows_by_id,
                    evidence_commit,
                )
            )
    return gates


def _load_report(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError("invalid_report") from exc
    _require(isinstance(value, dict))
    report = cast(dict[str, object], value)
    _require(frozenset(report) == REPORT_FIELDS)
    _require(_content_is_safe(report))
    return report


def _validate_report(
    repo: Path,
    report: Mapping[str, object],
    *,
    candidate: str,
) -> tuple[str, str, str]:
    _require(report.get("schema") == STAGE_REPORT_SCHEMA)
    _require(report.get("stage") == STAGE_3)
    report_status = report.get("status")
    _require(
        isinstance(report_status, str) and report_status in OVERALL_STATUSES
    )
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
    _require(evidence_commit == candidate_commit, "evidence_commit_mismatch")
    _require(
        is_ancestor(repo, STAGE_3_MERGE_TARGET, candidate_commit),
        "evidence_not_ancestor",
    )
    _require(
        tree_for_commit(repo, candidate_commit) == evidence_tree,
        "evidence_tree_mismatch",
    )
    _require(
        proof_input_sha256(repo, candidate_commit) == fingerprint,
        "proof_input_changed",
    )

    _require(
        report.get("contracts")
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
    _require(
        evidence_origin == "local-windows"
        or (
            isinstance(evidence_origin, str)
            and re.fullmatch(
                r"github-actions:[0-9]+:[0-9]+",
                evidence_origin,
            )
            is not None
        )
    )
    evidence_origin = cast(str, evidence_origin)
    report_reason_code = report.get("reason_code")
    _require(
        isinstance(report_reason_code, str)
        and report_reason_code in STABLE_REASON_CODES
    )
    gates = _validate_rows(report, evidence_commit)
    status = str(report["status"])
    reason = str(report["reason_code"])
    if status == "INVALID" and reason in FORCED_INVALID_REASONS:
        pass
    else:
        expected_status, expected_reason = report_reason(
            gates,
            [],
            evidence_origin=evidence_origin,
            required_gates=REQUIRED_STAGE_3_GATES,
            locally_runnable_gates=LOCALLY_RUNNABLE_STAGE_3_GATES,
        )
        _require(status == expected_status)
        _require(reason == expected_reason)
    return status, reason, evidence_commit


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--require-pass", action="store_true")
    return parser.parse_args(argv)


def _invalid(code: str) -> int:
    print(f"INVALID {STAGE_3} {code}")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = _load_report(args.report)
        status, reason, evidence_commit = _validate_report(
            Path.cwd(),
            report,
            candidate=args.candidate,
        )
    except (ReportError, ContractError, OSError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, (ReportError, ContractError)) else "invalid_report"
        return _invalid(code)
    if status == "PASS":
        print(f"PASS {STAGE_3} {evidence_commit}")
        return 0
    print(f"{status} {STAGE_3} {reason}")
    return 1 if args.require_pass else 0


if __name__ == "__main__":
    raise SystemExit(main())
