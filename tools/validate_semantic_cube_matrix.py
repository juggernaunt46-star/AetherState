#!/usr/bin/env python3
"""Validate the tracked Semantic Cube audit matrix without importing AetherState."""
from __future__ import annotations

import argparse
import itertools
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


AUDIT_TARGETS = (
    "recognition",
    "binding_world_alignment",
    "admission_complete_settlement",
    "narrator_transfer_model_compliance",
    "visibility_lifecycle",
)
DIAGNOSTIC_BOUNDARIES = (
    "recognition",
    "binding",
    "world_alignment",
    "admission",
    "complete_settlement",
    "narrator_transfer",
    "model_compliance",
    "visibility",
    "lifecycle",
)
MODES = ("exploration", "combat_opening", "combat_exchange")
LIFECYCLES = (
    "fresh_response",
    "duplicate_transport",
    "retry",
    "lost_reply",
    "swipe_regeneration",
    "continue",
    "reopen",
    "branch",
    "replay",
)
STATUSES = frozenset({"PASS", "HOLD", "NOT_APPLICABLE", "INVALID"})

MATRIX_SCHEMA = "semantic-cube-narrator-output-integrity-matrix/1"
DEFECTS_SCHEMA = "semantic-cube-narrator-output-integrity-defects/1"
ROW_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "invariant",
        "audit_target",
        "modes",
        "lifecycles",
        "status",
        "owner_paths",
        "selectors",
        "prerequisite_row_ids",
        "evidence_ids",
    }
)
FORBIDDEN_FIELDS = frozenset(
    {
        "prompt",
        "story",
        "reply",
        "request_text",
        "response_text",
        "credentials",
        "provider_payloads",
    }
)
_PERSONAL_ABSOLUTE_PATH = re.compile(
    r"^(?:[A-Za-z]:[\\/]|/(?:Users|home)/)", re.IGNORECASE
)
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _list(value: object) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _nonempty_strings(value: object) -> tuple[str, ...] | None:
    values = _list(value)
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        return None
    return tuple(values)


def _scan_rejected_content(value: object, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower().replace("-", "_")
            child_path = f"{path}.{key_text}"
            if normalized in FORBIDDEN_FIELDS:
                errors.append(f"{child_path}: forbidden authored or personal content field")
            _scan_rejected_content(child, child_path, errors)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_rejected_content(child, f"{path}[{index}]", errors)
        return
    if isinstance(value, str) and _PERSONAL_ABSOLUTE_PATH.match(value.strip()):
        errors.append(f"{path}: absolute personal path is forbidden")


def _selector_error(selector: str, repository_root: Path) -> str | None:
    if "\\" in selector or selector.startswith(("/", ".")):
        return "selector must be a repository-local POSIX pytest node id"
    pieces = selector.split("::")
    if len(pieces) < 2 or not pieces[0].startswith("tests/"):
        return "selector must name an exact test node under tests/"
    relative = Path(pieces[0])
    if relative.is_absolute() or ".." in relative.parts:
        return "selector escapes the repository"
    test_file = repository_root / relative
    if not test_file.is_file():
        return f"selector file does not exist: {pieces[0]}"
    node = pieces[-1].split("[", 1)[0]
    if not node:
        return "selector has an empty node"
    source = test_file.read_text(encoding="utf-8")
    node_pattern = re.compile(
        rf"(?m)^\s*(?:async\s+)?def\s+{re.escape(node)}\s*\("
    )
    if node_pattern.search(source) is None:
        return f"selector node does not exist: {node}"
    return None


def _validate_metadata(matrix: Mapping[str, Any], errors: list[str]) -> None:
    if matrix.get("schema") != MATRIX_SCHEMA:
        errors.append(f"matrix.schema: expected {MATRIX_SCHEMA}")
    exact_sequences = (
        ("audit_targets", AUDIT_TARGETS),
        ("diagnostic_boundaries", DIAGNOSTIC_BOUNDARIES),
        ("modes", MODES),
        ("lifecycles", LIFECYCLES),
    )
    for field, expected in exact_sequences:
        value = matrix.get(field)
        if not isinstance(value, list) or tuple(value) != expected:
            errors.append(f"matrix.{field}: must equal the exact contract sequence")
    statuses = matrix.get("statuses")
    if not isinstance(statuses, list) or set(statuses) != STATUSES or len(statuses) != len(STATUSES):
        errors.append("matrix.statuses: must contain each exact status once")


def _validate_rows(
    matrix: Mapping[str, Any],
    defects: Mapping[str, Any],
    *,
    repository_root: Path,
    terminal: bool,
    errors: list[str],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    rows_value = _list(matrix.get("rows"))
    if not rows_value:
        errors.append("matrix.rows: must be a nonempty list")
        return {}, {}

    rows: dict[str, Mapping[str, Any]] = {}
    for index, raw_row in enumerate(rows_value):
        path = f"matrix.rows[{index}]"
        row = _mapping(raw_row)
        if row is None:
            errors.append(f"{path}: must be an object")
            continue
        missing = sorted(ROW_REQUIRED_FIELDS - set(row))
        if missing:
            errors.append(f"{path}: missing required fields {missing}")
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.strip():
            errors.append(f"{path}.id: must be nonempty")
            continue
        if row_id in rows:
            errors.append(f"{path}.id: duplicate stable row id {row_id}")
            continue
        rows[row_id] = row

        invariant = row.get("invariant")
        if not isinstance(invariant, str) or not invariant.strip():
            errors.append(f"{path}.invariant: must be nonempty")
        target = row.get("audit_target")
        if target not in AUDIT_TARGETS:
            errors.append(f"{path}.audit_target: outside exact contract")
        modes = _nonempty_strings(row.get("modes"))
        if modes is None or not set(modes) <= set(MODES):
            errors.append(f"{path}.modes: must be a nonempty exact-mode subset")
        lifecycles = _nonempty_strings(row.get("lifecycles"))
        if lifecycles is None or not set(lifecycles) <= set(LIFECYCLES):
            errors.append(f"{path}.lifecycles: must be a nonempty exact-lifecycle subset")

        for field in ("owner_paths", "selectors", "evidence_ids"):
            if _nonempty_strings(row.get(field)) is None:
                errors.append(f"{path}.{field}: must contain nonempty strings")
        prerequisites = row.get("prerequisite_row_ids")
        if not isinstance(prerequisites, list) or any(
            not isinstance(item, str) or not item for item in prerequisites
        ):
            errors.append(f"{path}.prerequisite_row_ids: must be a string list")

        owner_paths = _nonempty_strings(row.get("owner_paths")) or ()
        for owner_path in owner_paths:
            relative = Path(owner_path)
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(f"{path}.owner_paths: nonlocal path {owner_path}")
            elif not (repository_root / relative).exists():
                errors.append(f"{path}.owner_paths: missing path {owner_path}")

        selectors = _nonempty_strings(row.get("selectors")) or ()
        for selector in selectors:
            selector_error = _selector_error(selector, repository_root)
            if selector_error:
                errors.append(f"{path}.selectors: {selector_error}")

        boundary = row.get("diagnostic_boundary")
        if boundary is not None and boundary not in DIAGNOSTIC_BOUNDARIES:
            errors.append(f"{path}.diagnostic_boundary: outside exact contract")

        status = row.get("status")
        if status is None:
            if terminal:
                errors.append(f"{path}.status: terminal matrix cannot contain null")
        elif status not in STATUSES:
            errors.append(f"{path}.status: outside exact contract")
        elif status == "INVALID":
            if terminal:
                errors.append(f"{path}.status: terminal matrix cannot contain INVALID")
            if not isinstance(row.get("invalid_reason"), str) or not row["invalid_reason"].strip():
                errors.append(f"{path}.invalid_reason: required for INVALID")
        elif status == "PASS":
            if not isinstance(row.get("pass_reason"), str) or not row["pass_reason"].strip():
                errors.append(f"{path}.pass_reason: required for PASS")
        elif status == "HOLD":
            if not isinstance(row.get("hold_reason"), str) or not row["hold_reason"].strip():
                errors.append(f"{path}.hold_reason: required for HOLD")
        elif status == "NOT_APPLICABLE":
            reason = row.get("not_applicable_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(
                    f"{path}.not_applicable_reason: required for NOT_APPLICABLE"
                )

    for row_id, row in rows.items():
        for prerequisite in row.get("prerequisite_row_ids", []):
            if prerequisite not in rows:
                errors.append(
                    f"matrix row {row_id}: unknown prerequisite row {prerequisite}"
                )

    defects_value = _list(defects.get("defects"))
    if defects.get("schema") != DEFECTS_SCHEMA:
        errors.append(f"defects.schema: expected {DEFECTS_SCHEMA}")
    if defects_value is None:
        errors.append("defects.defects: must be a list")
        return rows, {}

    defects_by_id: dict[str, Mapping[str, Any]] = {}
    for index, raw_defect in enumerate(defects_value):
        path = f"defects.defects[{index}]"
        defect = _mapping(raw_defect)
        if defect is None:
            errors.append(f"{path}: must be an object")
            continue
        defect_id = defect.get("id")
        if not isinstance(defect_id, str) or not defect_id.strip():
            errors.append(f"{path}.id: must be nonempty")
            continue
        if defect_id in defects_by_id:
            errors.append(f"{path}.id: duplicate defect id {defect_id}")
            continue
        defects_by_id[defect_id] = defect
        if defect.get("first_boundary") not in DIAGNOSTIC_BOUNDARIES:
            errors.append(f"{path}.first_boundary: outside exact contract")
        affected = _nonempty_strings(defect.get("affected_row_ids"))
        if affected is None:
            errors.append(f"{path}.affected_row_ids: must contain row ids")
            affected = ()
        repair_commit = defect.get("repair_commit")
        if not isinstance(repair_commit, str) or _COMMIT.fullmatch(repair_commit) is None:
            errors.append(f"{path}.repair_commit: must be an already-known commit id")
        for row_id in affected:
            if row_id not in rows:
                errors.append(f"{path}.affected_row_ids: unknown row {row_id}")
            elif defect_id not in (rows[row_id].get("defect_ids") or []):
                errors.append(
                    f"{path}: row {row_id} does not link back to defect {defect_id}"
                )

    for row_id, row in rows.items():
        defect_ids = row.get("defect_ids", [])
        if not isinstance(defect_ids, list) or any(
            not isinstance(item, str) or not item for item in defect_ids
        ):
            errors.append(f"matrix row {row_id}.defect_ids: must be a string list")
            continue
        for defect_id in defect_ids:
            defect = defects_by_id.get(defect_id)
            if defect is None:
                errors.append(f"matrix row {row_id}: unknown defect {defect_id}")
            elif row_id not in (defect.get("affected_row_ids") or []):
                errors.append(
                    f"matrix row {row_id}: defect {defect_id} does not link back"
                )
    return rows, defects_by_id


def _validate_coverage(
    matrix: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    errors: list[str],
) -> None:
    rules = _list(matrix.get("coverage_rules"))
    if not rules:
        errors.append("matrix.coverage_rules: must be a nonempty compact partition")
        return
    seen: dict[tuple[str, str, str], list[tuple[int, str]]] = {}
    for index, raw_rule in enumerate(rules):
        path = f"matrix.coverage_rules[{index}]"
        rule = _mapping(raw_rule)
        if rule is None:
            errors.append(f"{path}: must be an object")
            continue
        targets = _nonempty_strings(rule.get("audit_targets"))
        modes = _nonempty_strings(rule.get("modes"))
        lifecycles = _nonempty_strings(rule.get("lifecycles"))
        disposition = rule.get("disposition")
        if targets is None or not set(targets) <= set(AUDIT_TARGETS):
            errors.append(f"{path}.audit_targets: invalid coverage subset")
            continue
        if modes is None or not set(modes) <= set(MODES):
            errors.append(f"{path}.modes: invalid coverage subset")
            continue
        if lifecycles is None or not set(lifecycles) <= set(LIFECYCLES):
            errors.append(f"{path}.lifecycles: invalid coverage subset")
            continue
        if disposition not in {"required", "not_applicable"}:
            errors.append(f"{path}.disposition: must be required or not_applicable")
            continue
        reason = rule.get("contract_reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{path}.contract_reason: must be nonempty")
        for combination in itertools.product(targets, modes, lifecycles):
            seen.setdefault(combination, []).append((index, str(disposition)))

    universe = set(itertools.product(AUDIT_TARGETS, MODES, LIFECYCLES))
    missing = universe - set(seen)
    overlaps = {combination for combination, owners in seen.items() if len(owners) != 1}
    extra = set(seen) - universe
    if missing:
        errors.append(f"matrix.coverage_rules: missing required coverage for {len(missing)} combinations")
    if overlaps:
        errors.append(
            f"matrix.coverage_rules: overlapping coverage for {len(overlaps)} combinations"
        )
    if extra:
        errors.append(f"matrix.coverage_rules: includes {len(extra)} invalid combinations")

    for (target, mode, lifecycle), owners in seen.items():
        if len(owners) != 1 or owners[0][1] != "required":
            continue
        if not any(
            row.get("audit_target") == target
            and mode in (row.get("modes") or [])
            and lifecycle in (row.get("lifecycles") or [])
            for row in rows.values()
        ):
            errors.append(
                "matrix.coverage_rules: required coverage does not map to an invariant row "
                f"for {target}/{mode}/{lifecycle}"
            )


def validate_matrix(
    matrix: Mapping[str, object],
    defects: Mapping[str, object],
    *,
    repository_root: Path,
    terminal: bool,
) -> tuple[str, ...]:
    """Return every deterministic contract error without running tests."""
    errors: list[str] = []
    matrix_any: Mapping[str, Any] = matrix
    defects_any: Mapping[str, Any] = defects
    _scan_rejected_content(matrix_any, "matrix", errors)
    _scan_rejected_content(defects_any, "defects", errors)
    _validate_metadata(matrix_any, errors)
    rows, _ = _validate_rows(
        matrix_any,
        defects_any,
        repository_root=repository_root,
        terminal=terminal,
        errors=errors,
    )
    _validate_coverage(matrix_any, rows, errors)
    return tuple(errors)


def selected_test_nodes(
    matrix: Mapping[str, object],
    *,
    row_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return sorted unique exact selectors for all or selected matrix rows."""
    rows_value = matrix.get("rows")
    if not isinstance(rows_value, list):
        return ()
    requested = set(row_ids)
    known: set[str] = set()
    selectors: set[str] = set()
    for raw_row in rows_value:
        if not isinstance(raw_row, Mapping):
            continue
        row_id = raw_row.get("id")
        if not isinstance(row_id, str):
            continue
        known.add(row_id)
        if requested and row_id not in requested:
            continue
        row_selectors = raw_row.get("selectors")
        if isinstance(row_selectors, list):
            selectors.update(
                selector for selector in row_selectors if isinstance(selector, str) and selector
            )
    unknown = requested - known
    if unknown:
        raise ValueError(f"unknown row ids: {', '.join(sorted(unknown))}")
    return tuple(sorted(selectors))


def _load_mapping(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: top level must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--defects", required=True, type=Path)
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("--row", action="append", default=[])
    parser.add_argument("--print-selectors", action="store_true")
    args = parser.parse_args(argv)
    try:
        matrix = _load_mapping(args.matrix)
        defects = _load_mapping(args.defects)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"INVALID: {exc}")
        return 1
    repository_root = Path(__file__).resolve().parents[1]
    errors = validate_matrix(
        matrix,
        defects,
        repository_root=repository_root,
        terminal=args.terminal,
    )
    if errors:
        for error in errors:
            print(f"INVALID: {error}")
        return 1
    if args.print_selectors:
        try:
            selectors = selected_test_nodes(matrix, row_ids=args.row)
        except ValueError as exc:
            print(f"INVALID: {exc}")
            return 1
        print("\n".join(selectors))
    else:
        print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
