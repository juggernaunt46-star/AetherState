from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "proofbook" / "engineering_learning.py"
CORE = ROOT / "tools" / "proofbook" / "engineering_learning_core.py"
LEDGER = ROOT / "proofbook" / "LEDGER.jsonl"
GENESIS = ROOT / "proofbook" / "GENESIS.json"
TAGS = ROOT / "proofbook" / "TAGS.json"

REQUIRED_PUBLIC_LESSONS = {
    "semantic_cube.recognition.narrator_tag_markdown_actuality",
    "semantic_cube.world_alignment.apply_turn_snapshot",
    "semantic_cube.world_alignment.unique_display_identity",
    "semantic_cube.delivery.packet_model_hud_separation",
    "tooling.stage_gate.portable_git_object_fingerprint",
    "tooling.stage_gate.direct_source_derivation",
    "tooling.process.owned_descendant_containment",
    "environment.port_release.listener_liveness",
    "tooling.stage_gate.public_tree_completeness",
    "tooling.stage_gate.bootstrap_freeze_retirement",
    "tooling.proofbook.publication_boundary_is_structural",
    "tooling.proofbook.genesis_prefix_integrity",
    "tooling.proofbook.exact_regression_scope",
}
FORBIDDEN_PUBLIC_MARKERS = (
    ".codex",
    ".worktrees",
    "aetherstate-personal",
    "local-only",
    "knowledge/",
    "tooling/",
    "refs/remotes/origin/main",
)
REQUIRED_REGRESSION_NODES = {
    "lifecycle.proof_backed_terminal_identity": {
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_swipe_commit_rejects_arbitrary_source_fingerprint_before_pointer_change",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_swipe_commit_rejects_an_inactive_attempt_as_its_source",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_delayed_older_promotion_cannot_rewind_the_active_swipe",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_fallback_artifact_returns_exact_proved_base_behind_accepted_terminal",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_promotion_cas_rejects_changed_lifecycle_anchor_and_rolls_back_attempt",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_accepted_proof_rejects_wire_story_that_differs_from_visible_truth",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_accepted_proof_requires_exact_wire_content_type_identity",
        ),
        (
            "tests/test_turn_lifecycle_promotion_hardening.py",
            "test_fork_binds_child_lifecycle_to_active_swipe_terminal_not_attempt_zero",
        ),
        (
            "tests/test_semantic_pipeline_selection_path.py",
            "test_valid_selection_promotes_once_and_terminal_retry_sends_no_model_request",
        ),
        (
            "tests/test_turn_lifecycle.py",
            "test_crash_boundaries_reopen_fallback_then_exact_accepted_bytes",
        ),
    },
    "semantic_cube.settlement.atomic_exact_replay": {
        (
            "tests/test_mechanic_settlement.py",
            "test_builder_is_deterministic_idempotent_and_store_ready",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_settlement_identity_is_stable_while_the_accepted_request_detects_change",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_validator_rejects_structural_and_fingerprint_tampering",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_cross_frame_meaning_and_target_members_fail_closed",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_incomplete_atomic_group_is_rejected",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_duplicate_member_and_check_hp_disagreement_are_rejected",
        ),
        (
            "tests/test_mechanic_settlement.py",
            "test_opening_requirements_close_target_admission_and_scene_transition",
        ),
        (
            "tests/test_skill_check_settlement_state.py",
            "test_exact_retry_is_duplicate_and_changed_same_ref_conflicts_without_mutation",
        ),
        (
            "tests/test_skill_check_settlement_state.py",
            "test_reopen_replays_once_and_later_same_prose_is_a_distinct_occurrence",
        ),
        (
            "tests/test_weapon_settlement_state.py",
            "test_exact_retry_is_duplicate_without_journal_and_changed_retry_conflicts",
        ),
        (
            "tests/test_weapon_settlement_state.py",
            "test_reopen_replays_wrapper_once_and_later_occurrence_of_same_prose_is_distinct",
        ),
    },
    "worldlex.authority.definition_assignment_receipt_separation": {
        (
            "tests/test_worldlex.py",
            "test_definition_reference_requires_exact_revision_fingerprint_and_shape",
        ),
        (
            "tests/test_worldlex.py",
            "test_pool_members_preserve_exact_definition_revision_and_reject_forgery",
        ),
        (
            "tests/test_worldlex.py",
            "test_pool_stage_transition_must_be_one_step_with_exact_parent_and_members",
        ),
        (
            "tests/test_worldlex.py",
            "test_adapter_identity_is_versioned_and_distinct_from_reducer_receipt_identity",
        ),
        (
            "tests/test_worldlex_assignment.py",
            "test_missing_wrong_revision_fingerprint_and_world_are_rejected",
        ),
        (
            "tests/test_worldlex_assignment.py",
            "test_subject_and_definition_owner_must_match_exact_scope",
        ),
        (
            "tests/test_worldlex_assignment.py",
            "test_adapter_contract_and_payload_forgery_are_rejected",
        ),
        (
            "tests/test_worldlex_assignment.py",
            "test_adapter_must_cover_the_exact_definition_shape_and_concepts",
        ),
        (
            "tests/test_worldlex_assignment.py",
            "test_reopen_is_idempotent_and_old_revision_is_retained",
        ),
    },
    "worldlex.capability.independent_scale_axes": {
        (
            "tests/test_capability_glossary.py",
            "test_freeze_is_deterministic_and_revisioned",
        ),
        (
            "tests/test_capability_glossary.py",
            "test_definition_uses_one_deep_snapshot_and_does_not_alias_caller_data",
        ),
    },
    "worldlex.world_event_overlay_separation": {
        (
            "tests/test_world_events.py",
            "test_claim_belief_and_fact_admission_are_typed_separately",
        ),
        (
            "tests/test_world_events.py",
            "test_fresh_v2_adapter_subject_selector_and_receipt_are_closed_and_fingerprinted",
        ),
        (
            "tests/test_world_events.py",
            "test_v2_rejects_malformed_top_level_authority_shapes",
        ),
        (
            "tests/test_world_events.py",
            "test_v2_rejects_forged_cause_adapter_value_and_receipt",
        ),
        (
            "tests/test_world_events.py",
            "test_projection_filters_world_session_and_branch_unless_origins_are_explicit",
        ),
        (
            "tests/test_world_events.py",
            "test_duration_uses_replayable_game_time_and_unsupported_effects_remain_lore_only",
        ),
        (
            "tests/test_world_events.py",
            "test_future_eligibility_supports_typed_predicates_and_true_override",
        ),
        (
            "tests/test_public_proofbook.py",
            "test_public_proofbook_layout_is_complete_and_developer_only",
        ),
    },
}


def _run_cli(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), "--workspace-root", str(root), *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _records(path: Path = LEDGER) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _load_core() -> Any:
    spec = importlib.util.spec_from_file_location("public_proofbook_core", CORE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_public_proofbook(source_root: Path, target_root: Path) -> None:
    shutil.copytree(source_root / "proofbook", target_root / "proofbook")
    shutil.copytree(
        source_root / "tools" / "proofbook",
        target_root / "tools" / "proofbook",
    )
    public_test = target_root / "tests" / "test_public_proofbook.py"
    public_test.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_root / "tests" / "test_public_proofbook.py", public_test)
    for record in _records(source_root / "proofbook" / "LEDGER.jsonl"):
        for group in ("owners", "regressions", "evidence"):
            for reference in record[group]:
                relative = Path(reference["path"])
                target = target_root / relative
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_root / relative, target)


def _candidate_record(core: Any, target_root: Path) -> dict[str, Any]:
    proof = target_root / "tests" / "test_public_proofbook.py"
    digest = "sha256:" + hashlib.sha256(proof.read_bytes()).hexdigest()
    record: dict[str, Any] = {
        "schema": "aetherstate/engineering-lesson/1",
        "lesson_key": "tooling.proofbook.public_candidate_example",
        "revision": 1,
        "record_id": "sha256:" + ("0" * 64),
        "lifecycle": "candidate",
        "domain": "tooling",
        "diagnosis": {"boundary": "publication_destination"},
        "symptom": "A proposed lesson can be routed to the wrong publication boundary.",
        "cause": "Destination was not classified before evidence was assembled.",
        "repair_rule": (
            "Classify every lesson as public, private, or abstain before admission."
        ),
        "scope": {
            "supported": "Repository-owned public engineering evidence.",
            "not_supported": "Unreviewed or unavailable evidence.",
        },
        "owners": [
            {
                "path": "tests/test_public_proofbook.py",
                "symbol": "test_add_accepts_public_candidate_and_rejects_private_evidence",
                "sha256": digest,
            }
        ],
        "regressions": [
            {
                "runner": "pytest",
                "path": "tests/test_public_proofbook.py",
                "node": "test_add_accepts_public_candidate_and_rejects_private_evidence",
                "sha256": digest,
            }
        ],
        "evidence": [
            {
                "kind": "test",
                "path": "tests/test_public_proofbook.py",
                "sha256": digest,
            }
        ],
        "tags": [
            "domain:tooling",
            "policy:abstain_on_ambiguity",
            "risk:false_pass",
        ],
        "verification": {"evidence_class": "test", "mode": "focused"},
        "provenance": "public_contract",
        "privacy_review": {
            "status": "pending",
            "reviewer": "public-maintainer",
            "evidence": digest,
        },
        "supersedes": None,
        "rationale": "Public candidate used to prove append and destination controls.",
    }
    record["record_id"] = core.compute_record_id(record)
    return record


def test_public_proofbook_layout_is_complete_and_developer_only() -> None:
    required = {
        CLI,
        CORE,
        LEDGER,
        TAGS,
        GENESIS,
        ROOT / "proofbook" / "CONTRACT.md",
        ROOT / "proofbook" / "PUBLICATION_POLICY.md",
        ROOT / "proofbook" / "README.md",
    }
    assert all(path.is_file() for path in required)

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/aetherstate"]' in pyproject
    assert "proofbook" not in pyproject.casefold()
    for source in (ROOT / "src" / "aetherstate").rglob("*.py"):
        assert "proofbook" not in source.read_text(encoding="utf-8").casefold()


def test_public_ledger_validates_with_useful_current_lessons() -> None:
    validate = _run_cli("validate")
    assert validate.returncode == 0, validate.stderr
    assert validate.stdout.startswith("valid records=")

    status = _run_cli("status")
    assert status.returncode == 0, status.stderr
    fields = dict(
        field.split("=", 1) for field in status.stdout.strip().split()
    )
    assert int(fields["records"]) >= 25
    assert fields["records"] == fields["active"]
    assert fields["candidates"] == "0"
    assert fields["superseded"] == "0"
    assert fields["invalidated"] == "0"
    assert fields["stale"] == "0"


def test_public_ledger_is_a_new_safe_repository_relative_history() -> None:
    records = _records()
    assert REQUIRED_PUBLIC_LESSONS <= {
        str(record["lesson_key"]) for record in records
    }
    assert len({record["record_id"] for record in records}) == len(records)

    for record in records:
        assert record["revision"] == 1
        assert record["lifecycle"] == "verified"
        assert record["supersedes"] is None
        assert record["provenance"] == "public_contract"
        assert record["privacy_review"]["status"] == "approved"
        serialized = json.dumps(record, ensure_ascii=False).casefold()
        assert all(marker not in serialized for marker in FORBIDDEN_PUBLIC_MARKERS)
        for group in ("owners", "regressions", "evidence"):
            assert record[group]
            for reference in record[group]:
                assert "git_ref" not in reference
                relative = Path(reference["path"])
                assert not relative.is_absolute()
                assert ".." not in relative.parts
                assert (ROOT / relative).is_file()


def test_broad_public_lessons_name_every_direct_regression() -> None:
    by_key = {str(record["lesson_key"]): record for record in _records()}
    for lesson_key, required in REQUIRED_REGRESSION_NODES.items():
        actual = {
            (str(row["path"]), str(row["node"]))
            for row in by_key[lesson_key]["regressions"]
        }
        assert required <= actual, lesson_key


def test_each_required_regression_retrieves_its_owning_lesson() -> None:
    for lesson_key, required in REQUIRED_REGRESSION_NODES.items():
        for path, node in sorted(required):
            result = _run_cli(
                "brief",
                "--task",
                "zzzxxyy exact regression lookup",
                "--regression-node",
                f"{path}::{node}",
            )
            assert result.returncode == 0, result.stderr
            assert lesson_key in result.stdout
            assert "Matched: exact regression node" in result.stdout


def test_public_genesis_seals_the_ordered_initial_prefix(tmp_path: Path) -> None:
    genesis = json.loads(GENESIS.read_text(encoding="utf-8"))
    assert set(genesis) == {"schema", "record_count", "prefix_sha256"}
    assert genesis["schema"] == "aetherstate/proofbook-genesis/1"
    assert isinstance(genesis["record_count"], int)
    assert not isinstance(genesis["record_count"], bool)
    assert genesis["record_count"] == 37

    payload = LEDGER.read_bytes()
    prefix_lines = payload.splitlines(keepends=True)[: genesis["record_count"]]
    prefix = b"".join(prefix_lines)
    assert len(prefix_lines) == genesis["record_count"]
    assert (
        "sha256:" + hashlib.sha256(prefix).hexdigest()
        == genesis["prefix_sha256"]
    )

    for mutation in (
        "delete",
        "reorder",
        "truncate_and_reseal",
        "rewrite_and_reseal",
    ):
        clone = tmp_path / mutation
        clone.mkdir()
        _copy_public_proofbook(ROOT, clone)
        ledger = clone / "proofbook" / "LEDGER.jsonl"
        lines = ledger.read_bytes().splitlines(keepends=True)
        if mutation == "delete":
            del lines[4]
        elif mutation == "reorder":
            lines[4], lines[5] = lines[5], lines[4]
        elif mutation == "truncate_and_reseal":
            lines = lines[:25]
            shortened = b"".join(lines)
            genesis_path = clone / "proofbook" / "GENESIS.json"
            genesis_path.write_text(
                json.dumps(
                    {
                        "prefix_sha256": (
                            "sha256:" + hashlib.sha256(shortened).hexdigest()
                        ),
                        "record_count": 25,
                        "schema": "aetherstate/proofbook-genesis/1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        else:
            core = _load_core()
            rewritten = json.loads(lines[4])
            rewritten["rationale"] = (
                "Rewritten but internally canonical public-history row."
            )
            rewritten["record_id"] = core.compute_record_id(rewritten)
            lines[4] = (
                core.canonical_json(rewritten).encode("utf-8") + b"\n"
            )
            resealed = b"".join(lines[:37])
            genesis_path = clone / "proofbook" / "GENESIS.json"
            genesis_path.write_text(
                json.dumps(
                    {
                        "prefix_sha256": (
                            "sha256:" + hashlib.sha256(resealed).hexdigest()
                        ),
                        "record_count": 37,
                        "schema": "aetherstate/proofbook-genesis/1",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
        ledger.write_bytes(b"".join(lines))
        result = subprocess.run(
            [
                sys.executable,
                str(clone / "tools" / "proofbook" / "engineering_learning.py"),
                "--workspace-root",
                str(clone),
                "validate",
            ],
            cwd=clone,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == 1
        assert "genesis prefix" in result.stderr


def test_public_brief_is_deterministic_and_explains_a_match() -> None:
    args = (
        "brief",
        "--task",
        "Make bounded process cleanup contain resistant descendants",
        "--path",
        "tools/run_bounded_gate.py",
    )
    first = _run_cli(*args)
    second = _run_cli(*args)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert "ENGINEERING LEARNING BRIEF" in first.stdout
    assert "tooling.process.owned_descendant_containment" in first.stdout
    assert "Cause:" in first.stdout
    assert "Rule:" in first.stdout
    assert "Not supported:" in first.stdout
    assert "Currentness: verified" in first.stdout

    no_match = _run_cli("brief", "--task", "zzzxxyyqqq")
    assert no_match.returncode == 0
    assert "No verified current lesson matched this task." in no_match.stdout


def test_public_proofbook_validates_without_git_or_private_workspace_state(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clean-public-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)
    result = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "validate",
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_add_accepts_public_candidate_and_rejects_private_evidence(
    tmp_path: Path,
) -> None:
    core = _load_core()
    clone = tmp_path / "candidate-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)

    record = _candidate_record(core, clone)
    public_input = clone / "public-candidate.json"
    public_input.write_text(
        json.dumps(record, ensure_ascii=False),
        encoding="utf-8",
    )
    added = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "add",
            "--input",
            str(public_input),
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert added.returncode == 0, added.stderr
    assert added.stdout.strip() == record["record_id"]

    private_record = _candidate_record(core, clone)
    private_record["lesson_key"] = "tooling.proofbook.private_evidence_rejection"
    private_record["owners"][0]["path"] = ".codex/private-evidence.json"
    private_record["record_id"] = core.compute_record_id(private_record)
    private_input = clone / "private-candidate.json"
    private_input.write_text(
        json.dumps(private_record, ensure_ascii=False),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "add",
            "--input",
            str(private_input),
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert rejected.returncode == 1
    assert "public repository path" in rejected.stderr
    assert ".codex/private-evidence.json" not in rejected.stderr


def test_private_prose_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    core = _load_core()
    clone = tmp_path / "privacy-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)
    record = _candidate_record(core, clone)
    secret_text = "api_key=do-not-publish-this-value"
    record["symptom"] = secret_text
    record["record_id"] = core.compute_record_id(record)
    input_path = clone / "secret-candidate.json"
    input_path.write_text(json.dumps(record), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "add",
            "--input",
            str(input_path),
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    assert "private content" in result.stderr
    assert secret_text not in result.stderr


def test_private_provenance_and_git_metadata_paths_are_rejected(
    tmp_path: Path,
) -> None:
    core = _load_core()
    clone = tmp_path / "public-boundary-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)

    private_provenance = _candidate_record(core, clone)
    private_provenance["provenance"] = "redacted_authorized_audit"
    private_provenance["record_id"] = core.compute_record_id(private_provenance)
    provenance_input = clone / "private-provenance.json"
    provenance_input.write_text(
        json.dumps(private_provenance),
        encoding="utf-8",
    )
    provenance_result = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "add",
            "--input",
            str(provenance_input),
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert provenance_result.returncode == 1
    assert "public provenance" in provenance_result.stderr
    assert "redacted_authorized_audit" not in provenance_result.stderr

    git_metadata = _candidate_record(core, clone)
    git_metadata["lesson_key"] = "tooling.proofbook.git_metadata_rejection"
    git_metadata["owners"][0]["path"] = ".git/config"
    git_metadata["record_id"] = core.compute_record_id(git_metadata)
    git_input = clone / "git-metadata.json"
    git_input.write_text(json.dumps(git_metadata), encoding="utf-8")
    git_result = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "add",
            "--input",
            str(git_input),
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert git_result.returncode == 1
    assert "public repository path" in git_result.stderr
    assert ".git/config" not in git_result.stderr


def test_blank_ledger_lines_are_rejected_as_noncanonical(tmp_path: Path) -> None:
    clone = tmp_path / "blank-line-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)
    ledger = clone / "proofbook" / "LEDGER.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(
        "\n".join((lines[0], "", *lines[1:])) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(clone / "tools" / "proofbook" / "engineering_learning.py"),
            "--workspace-root",
            str(clone),
            "validate",
        ],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    assert "blank ledger line" in result.stderr


def test_add_rejects_duplicate_keys_quoted_credentials_and_missing_references(
    tmp_path: Path,
) -> None:
    core = _load_core()
    clone = tmp_path / "strict-add-tree"
    clone.mkdir()
    _copy_public_proofbook(ROOT, clone)
    command = [
        sys.executable,
        str(clone / "tools" / "proofbook" / "engineering_learning.py"),
        "--workspace-root",
        str(clone),
        "add",
    ]

    duplicate = _candidate_record(core, clone)
    duplicate_text = json.dumps(duplicate)
    duplicate_text = duplicate_text.replace(
        '"symptom":',
        '"symptom":"first value","symptom":',
        1,
    )
    duplicate_input = clone / "duplicate-key.json"
    duplicate_input.write_text(duplicate_text, encoding="utf-8")
    duplicate_result = subprocess.run(
        [*command, "--input", str(duplicate_input)],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert duplicate_result.returncode == 1
    assert "duplicate JSON object key" in duplicate_result.stderr
    assert "first value" not in duplicate_result.stderr

    credential_forms = (
        '{"api_key": "do-not-publish-this-value"}',
        '{"Authorization": "Bearer do-not-publish-this-value"}',
        '{"Authorization": "Basic do-not-publish-this-value"}',
        '{"Authorization": "Token do-not-publish-this-value"}',
        '{"Authorization": "Digest do-not-publish-this-value"}',
        '{"client_secret": "do-not-publish-this-value"}',
        '{"secret_key": "do-not-publish-this-value"}',
        '{"private_key": "do-not-publish-this-value"}',
        '{"auth_token": "do-not-publish-this-value"}',
        '{"token": "do-not-publish-this-value"}',
        "'Authorization': 'Bearer do-not-publish-this-value'",
        'authorization: "Bearer do-not-publish-this-value"',
        "Authorization = Bearer do-not-publish-this-value",
    )
    for index, credential_text in enumerate(credential_forms):
        credential = _candidate_record(core, clone)
        credential["lesson_key"] = (
            f"tooling.proofbook.quoted_credential_rejection_{index}"
        )
        credential["symptom"] = credential_text
        credential["record_id"] = core.compute_record_id(credential)
        credential_input = clone / f"quoted-credential-{index}.json"
        credential_input.write_text(json.dumps(credential), encoding="utf-8")
        credential_result = subprocess.run(
            [*command, "--input", str(credential_input)],
            cwd=clone,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert credential_result.returncode == 1
        assert "private content" in credential_result.stderr
        assert credential_text not in credential_result.stderr

    missing = _candidate_record(core, clone)
    missing["lesson_key"] = "tooling.proofbook.missing_public_reference"
    missing["owners"][0]["path"] = "tests/missing_public_owner.py"
    missing["record_id"] = core.compute_record_id(missing)
    missing_input = clone / "missing-reference.json"
    missing_input.write_text(json.dumps(missing), encoding="utf-8")
    missing_result = subprocess.run(
        [*command, "--input", str(missing_input)],
        cwd=clone,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert missing_result.returncode == 1
    assert "unavailable public references" in missing_result.stderr
    assert "tests/missing_public_owner.py" not in missing_result.stderr
