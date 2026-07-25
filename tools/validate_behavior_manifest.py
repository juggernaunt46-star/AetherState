from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, NoReturn

from stage_gate_contract import REQUIRED_STAGE_1_GATES


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aetherstate-behavior-player-surface/1"
BASELINE_VERSION = "1.24.0"
BASELINE_COMMIT = "82b58277d7a1fb167434be0290d3dfd2bb3588e2"
REACHABILITY = frozenset({"direct", "configured", "background"})
PROOF_KINDS = frozenset({"pytest", "node", "workflow"})


class ManifestError(ValueError):
    def __init__(self, code: str, reference: str) -> None:
        self.code = code
        self.reference = reference
        super().__init__(f"ERROR {code} {reference}")


def _fail(code: str, reference: str) -> NoReturn:
    raise ManifestError(code, reference)


def _read_json(path: Path, reference: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail("manifest_unreadable", reference)
    if not isinstance(value, dict):
        _fail("manifest_not_object", reference)
    return value


def _relative_path(value: object, reference: str, suffix: str) -> Path:
    if not isinstance(value, str) or not value:
        _fail("path_malformed", reference)
    normalized = value.replace("\\", "/")
    windows_path = PureWindowsPath(value)
    if (
        normalized.startswith("/")
        or windows_path.is_absolute()
        or ":" in normalized
        or any(part == ".." for part in normalized.split("/"))
        or not normalized.endswith(suffix)
    ):
        _fail("path_not_relative", reference)
    return Path(normalized)


def _baseline_ids(path: Path) -> list[str]:
    try:
        ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        _fail("baseline_unreadable", "baseline_ids")
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        _fail("baseline_ids_not_canonical", "baseline_ids")
    return ids


def _validate_pytest(proof: dict[str, Any], reference: str) -> str:
    selector = proof.get("selector")
    if not isinstance(selector, str) or selector.count("::") != 1:
        _fail("pytest_selector_malformed", reference)
    path_text, node = selector.split("::", 1)
    path = _relative_path(path_text, reference, ".py")
    if not node.startswith("test_") or not node.replace("_", "").isalnum():
        _fail("pytest_selector_malformed", reference)
    if not (ROOT / path).is_file():
        _fail("pytest_path_missing", reference)
    return selector


def _validate_node(proof: dict[str, Any], reference: str) -> None:
    command = proof.get("command")
    if not isinstance(command, list) or len(command) != 2 or command[0] != "node":
        _fail("node_command_malformed", reference)
    path = _relative_path(command[1], reference, ".mjs")
    if not (ROOT / path).is_file():
        _fail("node_path_missing", reference)


def _validate_workflow(proof: dict[str, Any], reference: str) -> None:
    gate = proof.get("gate")
    if not isinstance(gate, str) or gate not in REQUIRED_STAGE_1_GATES:
        _fail("workflow_gate_unknown", reference)


def validate(manifest_path: Path, baseline_path: Path) -> list[str]:
    value = _read_json(manifest_path, "manifest")
    if value.get("schema") != SCHEMA:
        _fail("schema_unknown", "manifest")
    baseline = value.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("version") != BASELINE_VERSION or baseline.get("commit") != BASELINE_COMMIT:
        _fail("baseline_unknown", "manifest")
    if value.get("merge_target") != BASELINE_COMMIT:
        _fail("merge_target_invalid", "manifest")
    if value.get("cumulative") is not True:
        _fail("cumulative_required", "manifest")
    if not isinstance(value.get("known_defects"), list):
        _fail("known_defects_malformed", "manifest")
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, list):
        _fail("surfaces_malformed", "manifest")

    ids: list[str] = []
    selectors: list[str] = []
    for entry in surfaces:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            _fail("surface_id_malformed", "surface")
        entry_id = entry["id"]
        ids.append(entry_id)
        if entry.get("preservation") != "required":
            _fail("preservation_missing", entry_id)
        if entry.get("default_reachability") not in REACHABILITY:
            _fail("reachability_missing", entry_id)
        proofs = entry.get("proofs")
        if not isinstance(proofs, list) or not proofs:
            _fail("proofs_missing", entry_id)
        for index, proof in enumerate(proofs):
            reference = f"{entry_id}/proof-{index}"
            if not isinstance(proof, dict) or proof.get("kind") not in PROOF_KINDS:
                _fail("proof_kind_unknown", reference)
            if proof["kind"] == "pytest":
                selectors.append(_validate_pytest(proof, reference))
            elif proof["kind"] == "node":
                _validate_node(proof, reference)
            else:
                _validate_workflow(proof, reference)
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        _fail("surface_ids_not_canonical", "surfaces")
    if not set(_baseline_ids(baseline_path)) <= set(ids):
        _fail("baseline_surface_removed", "surfaces")
    return list(dict.fromkeys(selectors))


def _check_pytest_collection(selectors: list[str]) -> None:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", *selectors]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        _fail("pytest_collection_failed", "pytest-selectors")


def _check_merge_target(manifest_path: Path, requested_ref: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", requested_ref],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        _fail("merge_target_unresolved", "merge_target")
    manifest = _read_json(manifest_path, "manifest")
    if result.stdout.strip() != manifest["merge_target"]:
        raise ManifestError("HOLD", "merge_target_advanced")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/hardening/post-1.24/behavior-player-surface-manifest.json"),
    )
    parser.add_argument(
        "--baseline-ids",
        type=Path,
        default=Path("tests/fixtures/hardening/behavior-surface-ids-1.24.0.txt"),
    )
    parser.add_argument("--check-pytest-collection", action="store_true")
    parser.add_argument("--merge-target", choices=["origin/main"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest_path = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
        baseline_path = args.baseline_ids if args.baseline_ids.is_absolute() else ROOT / args.baseline_ids
        selectors = validate(manifest_path, baseline_path)
        if args.check_pytest_collection:
            _check_pytest_collection(selectors)
        if args.merge_target:
            _check_merge_target(manifest_path, args.merge_target)
    except ManifestError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"PASS behavior-player-surface {len(_read_json(manifest_path, 'manifest')['surfaces'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
