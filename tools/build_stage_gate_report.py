from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Sequence

from stage_gate_contract import (
    BUDGET,
    DERIVED_EVIDENCE_FIELDS,
    DIRECT_EVIDENCE_FIELDS,
    GATE_EVIDENCE_SCHEMA,
    GATE_STATUSES,
    LOCAL_DIAGNOSTIC_GATES,
    MANIFEST_PATH,
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
    derivation_source_row_is_valid,
    elapsed_seconds_is_valid,
    gate_status_reason_is_valid,
    proof_input_sha256,
    report_reason,
    resolve_commit,
    tree_for_commit,
)

COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
ORIGIN_RE = re.compile(r"github-actions:[0-9]+:[0-9]+\Z")


def _missing_row(gate_id: str, evidence_commit: str) -> dict[str, object]:
    return {
        "id": gate_id,
        "status": "NOT_RUN",
        "elapsed_seconds": 0.0,
        "reason_code": "gate_not_run",
        "evidence_commit": evidence_commit,
    }


def _invalid_row(
    gate_id: str,
    evidence_commit: str,
    reason: str = "invalid_gate_evidence",
) -> dict[str, object]:
    return {
        "id": gate_id,
        "status": "INVALID",
        "elapsed_seconds": 0.0,
        "reason_code": reason,
        "evidence_commit": evidence_commit,
    }


def _load_evidence(
    path: Path,
    expected_id: str,
    evidence_commit: str,
) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _invalid_row(expected_id, evidence_commit)
    if not isinstance(value, dict):
        return _invalid_row(expected_id, evidence_commit)
    commit = value.get("evidence_commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        return _invalid_row(expected_id, evidence_commit)
    if commit != evidence_commit:
        return _invalid_row(
            expected_id,
            evidence_commit,
            "evidence_commit_mismatch",
        )
    fields = frozenset(value)
    if fields not in {DIRECT_EVIDENCE_FIELDS, DERIVED_EVIDENCE_FIELDS}:
        return _invalid_row(expected_id, evidence_commit)
    elapsed = value.get("elapsed_seconds")
    if (
        value.get("schema") != GATE_EVIDENCE_SCHEMA
        or value.get("id") != expected_id
        or value.get("status") not in GATE_STATUSES
        or not elapsed_seconds_is_valid(elapsed)
        or value.get("reason_code") not in STABLE_REASON_CODES
    ):
        return _invalid_row(expected_id, evidence_commit)
    status_reason_valid = (
        gate_status_reason_is_valid(
            expected_id,
            value.get("status"),
            value.get("reason_code"),
            value.get("source_gate"),
        )
        if "source_gate" in value
        else gate_status_reason_is_valid(
            expected_id,
            value.get("status"),
            value.get("reason_code"),
        )
    )
    if not status_reason_valid:
        return _invalid_row(expected_id, evidence_commit)
    row = {key: value[key] for key in DIRECT_EVIDENCE_FIELDS if key != "schema"}
    if "source_gate" in value:
        row["source_gate"] = value["source_gate"]
    return row


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--evidence-origin", required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def _origin_is_safe(value: str) -> bool:
    return value == "local-windows" or ORIGIN_RE.fullmatch(value) is not None


def build_report(
    repo: Path,
    evidence_dir: Path,
    evidence_origin: str,
) -> dict[str, object]:
    evidence_commit = resolve_commit(repo, "HEAD")
    evidence_tree = tree_for_commit(repo, evidence_commit)
    forced_reason: str | None = None
    try:
        check_public_safe_history(repo, evidence_commit)
    except ContractError as exc:
        forced_reason = exc.code

    known_ids = REQUIRED_STAGE_1_GATES | LOCAL_DIAGNOSTIC_GATES
    seen_files: dict[str, Path] = {}
    if evidence_dir.is_dir():
        for path in sorted(evidence_dir.glob("*.json")):
            if path.stem not in known_ids or path.stem in seen_files:
                forced_reason = forced_reason or "unknown_gate_evidence"
                continue
            seen_files[path.stem] = path
    elif evidence_dir.exists():
        raise OSError("evidence_dir_invalid")

    gates = [
        _load_evidence(seen_files[gate_id], gate_id, evidence_commit)
        if gate_id in seen_files
        else _missing_row(gate_id, evidence_commit)
        for gate_id in sorted(REQUIRED_STAGE_1_GATES)
    ]
    diagnostics = [
        _load_evidence(seen_files[gate_id], gate_id, evidence_commit)
        for gate_id in sorted(LOCAL_DIAGNOSTIC_GATES)
        if gate_id in seen_files
    ]
    rows_by_id = {
        str(row["id"]): row
        for row in gates + diagnostics
    }
    gates = [
        (
            row
            if "source_gate" not in row
            or derivation_source_row_is_valid(
                str(row["id"]),
                str(row["source_gate"]),
                rows_by_id,
                evidence_commit,
            )
            else _invalid_row(
                str(row["id"]),
                evidence_commit,
                "source_gate_invalid",
            )
        )
        for row in gates
    ]

    umbrella_hash = candidate_file_sha256(repo, evidence_commit, UMBRELLA_PATH)
    cube_hash = candidate_file_sha256(repo, evidence_commit, SEMANTIC_CUBE_PATH)
    if umbrella_hash != UMBRELLA_SHA256 or cube_hash != SEMANTIC_CUBE_SHA256:
        forced_reason = forced_reason or "invalid_report"
    status, reason = report_reason(
        gates,
        diagnostics,
        evidence_origin=evidence_origin,
        forced_reason=forced_reason,
    )
    return {
        "schema": STAGE_REPORT_SCHEMA,
        "stage": STAGE,
        "status": status,
        "public_base": PUBLIC_BASE,
        "evidence_commit": evidence_commit,
        "evidence_tree": evidence_tree,
        "proof_input_sha256": proof_input_sha256(repo, evidence_commit),
        "contracts": {
            "umbrella_sha256": umbrella_hash,
            "semantic_cube_canonical_tip": SEMANTIC_CUBE_CANONICAL_TIP,
            "semantic_cube_sha256": cube_hash,
        },
        "manifest": {
            "path": MANIFEST_PATH,
            "sha256": candidate_file_sha256(repo, evidence_commit, MANIFEST_PATH),
        },
        "gates": gates,
        "diagnostics": diagnostics,
        "budget": BUDGET,
        "evidence_origin": evidence_origin,
        "reason_code": reason,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not _origin_is_safe(args.evidence_origin):
        print("INVALID evidence_origin")
        return 2
    try:
        report = build_report(Path.cwd(), args.evidence_dir, args.evidence_origin)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(f".{args.report.name}.tmp")
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(args.report)
    except (OSError, ContractError, subprocess.SubprocessError):
        print("INVALID report_build_failed")
        return 2
    print(f"{report['status']} {STAGE} {report['reason_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
