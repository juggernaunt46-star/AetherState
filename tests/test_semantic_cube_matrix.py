"""Contract tests for the tracked Stage 2 Semantic Cube audit matrix."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs/hardening/semantic-cube/narrator-output-integrity-matrix.json"
DEFECTS_PATH = ROOT / "docs/hardening/semantic-cube/narrator-output-integrity-defects.json"
VALIDATOR_PATH = ROOT / "tools/validate_semantic_cube_matrix.py"


def _validator():
    spec = importlib.util.spec_from_file_location("semantic_cube_matrix", VALIDATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _documents():
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8")), json.loads(
        DEFECTS_PATH.read_text(encoding="utf-8")
    )


def _first_row(matrix):
    return matrix["rows"][0]


def test_matrix_uses_exact_targets_boundaries_modes_lifecycles_and_statuses() -> None:
    validator = _validator()
    matrix, defects = _documents()
    assert tuple(matrix["audit_targets"]) == validator.AUDIT_TARGETS
    assert tuple(matrix["diagnostic_boundaries"]) == validator.DIAGNOSTIC_BOUNDARIES
    assert tuple(matrix["modes"]) == validator.MODES
    assert tuple(matrix["lifecycles"]) == validator.LIFECYCLES
    assert set(matrix["statuses"]) == validator.STATUSES
    assert validator.validate_matrix(matrix, defects, repository_root=ROOT, terminal=False) == ()


def test_matrix_rows_require_nonempty_explicit_scope_and_stable_ids() -> None:
    validator = _validator()
    matrix, defects = _documents()
    changed = copy.deepcopy(matrix)
    _first_row(changed)["modes"] = []
    assert any("modes" in error for error in validator.validate_matrix(
        changed, defects, repository_root=ROOT, terminal=False
    ))
    changed = copy.deepcopy(matrix)
    _first_row(changed)["id"] = ""
    assert any("id" in error for error in validator.validate_matrix(
        changed, defects, repository_root=ROOT, terminal=False
    ))


def test_coverage_rules_partition_every_combination_without_135_rows() -> None:
    validator = _validator()
    matrix, defects = _documents()
    assert len(matrix["rows"]) < 135
    assert validator.validate_matrix(matrix, defects, repository_root=ROOT, terminal=False) == ()


def test_every_required_combination_maps_to_an_invariant_row() -> None:
    validator = _validator()
    matrix, defects = _documents()
    changed = copy.deepcopy(matrix)
    changed["rows"] = changed["rows"][:1]
    assert any("required coverage" in error for error in validator.validate_matrix(
        changed, defects, repository_root=ROOT, terminal=False
    ))


def test_provided_selectors_resolve_to_existing_repo_local_pytest_nodes() -> None:
    validator = _validator()
    matrix, defects = _documents()
    selectors = validator.selected_test_nodes(matrix)
    assert selectors
    assert validator.validate_matrix(matrix, defects, repository_root=ROOT, terminal=False) == ()


def test_terminal_pass_hold_and_not_applicable_require_their_exact_evidence() -> None:
    validator = _validator()
    matrix, defects = _documents()
    terminal = copy.deepcopy(matrix)
    for index, row in enumerate(terminal["rows"]):
        row["status"] = "PASS"
        row["pass_reason"] = "synthetic terminal contract"
        if index == 1:
            row["status"] = "HOLD"
            row.pop("pass_reason")
            row["hold_reason"] = "synthetic hold"
        elif index == 2:
            row["status"] = "NOT_APPLICABLE"
            row.pop("pass_reason")
            row["not_applicable_reason"] = "synthetic omission contract"
    assert validator.validate_matrix(terminal, defects, repository_root=ROOT, terminal=True) == ()
    for index, reason_field in (
        (0, "pass_reason"),
        (1, "hold_reason"),
        (2, "not_applicable_reason"),
    ):
        changed = copy.deepcopy(terminal)
        changed["rows"][index].pop(reason_field)
        assert validator.validate_matrix(
            changed, defects, repository_root=ROOT, terminal=True
        )
        changed = copy.deepcopy(terminal)
        changed["rows"][index]["evidence_ids"] = []
        assert validator.validate_matrix(
            changed, defects, repository_root=ROOT, terminal=True
        )


def test_terminal_rejects_null_invalid_and_unjustified_omissions() -> None:
    validator = _validator()
    matrix, defects = _documents()
    incomplete = copy.deepcopy(matrix)
    _first_row(incomplete)["status"] = None
    _first_row(incomplete).pop("pass_reason", None)
    assert validator.validate_matrix(incomplete, defects, repository_root=ROOT, terminal=True)
    invalid = copy.deepcopy(matrix)
    _first_row(invalid)["status"] = "INVALID"
    _first_row(invalid)["invalid_reason"] = "synthetic invalid row"
    assert validator.validate_matrix(invalid, defects, repository_root=ROOT, terminal=True)
    terminal = copy.deepcopy(matrix)
    for row in terminal["rows"]:
        row["status"] = "PASS"
        row["pass_reason"] = "synthetic terminal contract"
    _first_row(terminal)["selectors"] = []
    assert validator.validate_matrix(terminal, defects, repository_root=ROOT, terminal=True)


def test_defect_links_are_bidirectional_and_repair_commits_are_not_self_referential() -> None:
    validator = _validator()
    matrix, defects = _documents()
    for row in matrix["rows"]:
        row.pop("defect_ids", None)
    changed_defects = copy.deepcopy(defects)
    changed_defects["defects"] = [{
        "id": "scope-example",
        "first_boundary": "recognition",
        "affected_row_ids": [_first_row(matrix)["id"]],
        "repair_commit": "self",
    }]
    assert validator.validate_matrix(matrix, changed_defects, repository_root=ROOT, terminal=False)
    changed_defects["defects"][0]["repair_commit"] = "0123456789abcdef"
    assert any(
        "does not link back" in error
        for error in validator.validate_matrix(
            matrix, changed_defects, repository_root=ROOT, terminal=False
        )
    )
    linked = copy.deepcopy(matrix)
    _first_row(linked)["defect_ids"] = ["scope-example"]
    assert validator.validate_matrix(
        linked, changed_defects, repository_root=ROOT, terminal=False
    ) == ()


def test_matrix_and_defects_reject_authored_or_personal_content_fields() -> None:
    validator = _validator()
    matrix, defects = _documents()
    changed = copy.deepcopy(matrix)
    _first_row(changed)["reply"] = "forbidden"
    assert any("forbidden" in error for error in validator.validate_matrix(
        changed, defects, repository_root=ROOT, terminal=False
    ))
    changed_defects = copy.deepcopy(defects)
    changed_defects["credentials"] = "not allowed"
    assert any("forbidden" in error for error in validator.validate_matrix(
        matrix, changed_defects, repository_root=ROOT, terminal=False
    ))
    changed = copy.deepcopy(matrix)
    _first_row(changed)["owner_paths"] = ["C:\\Users\\example\\private.py"]
    assert any("absolute personal path" in error for error in validator.validate_matrix(
        changed, defects, repository_root=ROOT, terminal=False
    ))
