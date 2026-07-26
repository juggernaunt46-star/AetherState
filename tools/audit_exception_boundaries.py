#!/usr/bin/env python3
"""Create or verify the repository-safe broad-exception boundary audit."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Iterable


SCHEMA = "aetherstate-exception-boundary-audit/1"
POLICY_VERSION = "aetherstate-exception-boundary-policy/1"
BOUNDARIES = frozenset(
    {
        "streaming",
        "rollback",
        "shutdown",
        "optional_cognition",
        "availability_boundary",
    }
)


@dataclass(frozen=True)
class Policy:
    boundary: str
    reason_code: str


# This is deliberately a closed table. A new handler-bearing module has no policy
# until its owner explicitly assigns a boundary class and reason code here.
MODULE_POLICY = {
    "src/aetherstate/__init__.py": Policy("shutdown", "PACKAGE_BOOTSTRAP_SHUTDOWN"),
    "src/aetherstate/__main__.py": Policy("shutdown", "CLI_SHUTDOWN_AND_OPTIONAL_SETUP"),
    "src/aetherstate/app.py": Policy("shutdown", "APPLICATION_LIFESPAN_SHUTDOWN"),
    "src/aetherstate/assist.py": Policy("optional_cognition", "ASSIST_OPTIONAL_COGNITION"),
    "src/aetherstate/chat_continuity.py": Policy("rollback", "CHAT_CONTINUITY_ADMISSION_ROLLBACK"),
    "src/aetherstate/compose.py": Policy("availability_boundary", "COMPOSITION_AVAILABILITY_FALLBACK"),
    "src/aetherstate/config.py": Policy("availability_boundary", "CONFIGURATION_AVAILABILITY_FALLBACK"),
    "src/aetherstate/control.py": Policy("availability_boundary", "CONTROL_SURFACE_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/creator.py": Policy("availability_boundary", "CREATOR_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/director.py": Policy("optional_cognition", "DIRECTOR_OPTIONAL_COGNITION"),
    "src/aetherstate/enemy_kits.py": Policy("optional_cognition", "ENEMY_KIT_OPTIONAL_COGNITION"),
    "src/aetherstate/extraction.py": Policy("optional_cognition", "EXTRACTION_OPTIONAL_COGNITION"),
    "src/aetherstate/genesis.py": Policy("availability_boundary", "GENESIS_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/hud.py": Policy("availability_boundary", "HUD_AVAILABILITY_FALLBACK"),
    "src/aetherstate/jobs.py": Policy("optional_cognition", "BACKGROUND_JOB_OPTIONAL_COGNITION"),
    "src/aetherstate/knowledge.py": Policy("optional_cognition", "KNOWLEDGE_OPTIONAL_COGNITION"),
    "src/aetherstate/linter.py": Policy("optional_cognition", "LINTER_OPTIONAL_COGNITION"),
    "src/aetherstate/memory.py": Policy("optional_cognition", "MEMORY_OPTIONAL_COGNITION"),
    "src/aetherstate/narration_pre_display_guard.py": Policy("optional_cognition", "NARRATION_ADVISORY_OPTIONAL_COGNITION"),
    "src/aetherstate/narrator.py": Policy("optional_cognition", "NARRATOR_OPTIONAL_COGNITION"),
    "src/aetherstate/pipeline.py": Policy("optional_cognition", "PIPELINE_OPTIONAL_COGNITION"),
    "src/aetherstate/player_lessons.py": Policy("rollback", "PLAYER_LESSONS_ROLLBACK"),
    "src/aetherstate/playerlex.py": Policy("rollback", "PLAYERLEX_ROLLBACK"),
    "src/aetherstate/playstack.py": Policy("availability_boundary", "PLAYSTACK_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/promptcache.py": Policy("optional_cognition", "PROMPT_CACHE_OPTIONAL_COGNITION"),
    "src/aetherstate/prompts.py": Policy("optional_cognition", "PROMPT_CONTRACT_OPTIONAL_COGNITION"),
    "src/aetherstate/proxy.py": Policy("streaming", "PROXY_STREAMING_BOUNDARY"),
    "src/aetherstate/registry/__init__.py": Policy("availability_boundary", "REGISTRY_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/schema_migrations.py": Policy("rollback", "SCHEMA_MIGRATION_ROLLBACK"),
    "src/aetherstate/secret_store.py": Policy("availability_boundary", "SECRET_STORE_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/semantic_transition_truth.py": Policy("availability_boundary", "SEMANTIC_PROJECTION_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/state.py": Policy("rollback", "STATE_REDUCTION_ROLLBACK"),
    "src/aetherstate/status.py": Policy("availability_boundary", "STATUS_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/store.py": Policy("rollback", "STORE_TRANSACTION_ROLLBACK"),
    "src/aetherstate/system_health.py": Policy("availability_boundary", "SYSTEM_HEALTH_AVAILABILITY_BOUNDARY"),
    "src/aetherstate/tier0.py": Policy("optional_cognition", "TIER0_OPTIONAL_COGNITION"),
    "src/aetherstate/worldlex_store.py": Policy("rollback", "WORLDLEX_STORE_ROLLBACK"),
}

# Pipeline performs several different jobs. These named, source-owned function
# families override its module family; anything else remains explicitly optional.
FUNCTION_POLICY = {
    ("src/aetherstate/pipeline.py", "Pipeline._semantic_promote_or_replay"): Policy("rollback", "PIPELINE_SEMANTIC_ROLLBACK"),
    ("src/aetherstate/pipeline.py", "Pipeline._semantic_route_replay"): Policy("rollback", "PIPELINE_SEMANTIC_ROLLBACK"),
    ("src/aetherstate/pipeline.py", "Pipeline.complete_semantic_selection"): Policy("rollback", "PIPELINE_SEMANTIC_ROLLBACK"),
    ("src/aetherstate/pipeline.py", "Pipeline._process_truth_gated"): Policy("rollback", "PIPELINE_SEMANTIC_ROLLBACK"),
    ("src/aetherstate/pipeline.py", "Pipeline._process_with_player_lessons_guard"): Policy("rollback", "PIPELINE_SEMANTIC_ROLLBACK"),
    ("src/aetherstate/pipeline.py", "Pipeline.on_response"): Policy("streaming", "PIPELINE_RESPONSE_STREAMING"),
    ("src/aetherstate/pipeline.py", "Pipeline.guard_response"): Policy("streaming", "PIPELINE_RESPONSE_STREAMING"),
    ("src/aetherstate/pipeline.py", "Pipeline.record_response_trace"): Policy("streaming", "PIPELINE_RESPONSE_STREAMING"),
    ("src/aetherstate/pipeline.py", "Pipeline.on_upstream_error"): Policy("streaming", "PIPELINE_RESPONSE_STREAMING"),
    ("src/aetherstate/pipeline.py", "Pipeline.transport_error"): Policy("streaming", "PIPELINE_RESPONSE_STREAMING"),
    ("src/aetherstate/player_lessons.py", "PlayerLessons._playerlex_entries"): Policy("optional_cognition", "PLAYER_LESSONS_OPTIONAL_PLAYERLEX"),
    ("src/aetherstate/player_lessons.py", "PlayerLessons._stored_lesson"): Policy("availability_boundary", "PLAYER_LESSONS_STORAGE_AVAILABILITY"),
    ("src/aetherstate/player_lessons.py", "PlayerLessons._item_values"): Policy("availability_boundary", "PLAYER_LESSONS_STORAGE_AVAILABILITY"),
    ("src/aetherstate/player_lessons.py", "PlayerLessons._intent_item_values"): Policy("availability_boundary", "PLAYER_LESSONS_STORAGE_AVAILABILITY"),
    ("src/aetherstate/player_lessons.py", "PlayerLessons._compile_sample.safe_overlay"): Policy("optional_cognition", "PLAYER_LESSONS_OPTIONAL_PREVIEW"),
    ("src/aetherstate/playerlex.py", "PlayerLex._v1_record_from_values"): Policy("availability_boundary", "PLAYERLEX_STORAGE_AVAILABILITY"),
    ("src/aetherstate/playerlex.py", "PlayerLex._record_from_row"): Policy("availability_boundary", "PLAYERLEX_STORAGE_AVAILABILITY"),
    ("src/aetherstate/state.py", "_world_overlay_operation_violation"): Policy("availability_boundary", "WORLD_OVERLAY_AVAILABILITY_BOUNDARY"),
    ("src/aetherstate/state.py", "reduce_state"): Policy("availability_boundary", "REDUCER_AVAILABILITY_BOUNDARY"),
    ("src/aetherstate/state.py", "state_summary"): Policy("availability_boundary", "STATE_SUMMARY_AVAILABILITY_BOUNDARY"),
    ("src/aetherstate/state.py", "_attach_world_event_branch_view"): Policy("availability_boundary", "WORLD_EVENT_VIEW_AVAILABILITY_BOUNDARY"),
    ("src/aetherstate/state.py", "_enrich"): Policy("availability_boundary", "STATE_ENRICHMENT_AVAILABILITY_BOUNDARY"),
    ("src/aetherstate/system_health.py", "SystemHealth._persist"): Policy("rollback", "SYSTEM_HEALTH_DURABLE_ROLLBACK"),
}


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _tracked_source_paths(root: Path) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--", "src/aetherstate"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    paths = sorted(
        item for item in completed.stdout.splitlines()
        if item.startswith("src/aetherstate/") and item.endswith(".py")
    )
    return tuple(root / item for item in paths)


def _qualified_handlers(path: Path, root: Path) -> Iterable[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.rows: list[tuple[int, str, str]] = []

        def _visit_scope(self, node: ast.AST, name: str) -> None:
            self.scope.append(name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node, node.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scope(node, node.name)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is None:
                kind = "bare"
            elif isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                kind = node.type.id
            else:
                self.generic_visit(node)
                return
            self.rows.append((node.lineno, kind, ".".join(self.scope) or "<module>"))
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    yield from sorted(visitor.rows)


def _source_fingerprint(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _policy_document() -> dict[str, object]:
    module_families = [
        {
            "boundary": policy.boundary,
            "reason_code": policy.reason_code,
            "paths": [path],
        }
        for path, policy in sorted(MODULE_POLICY.items())
    ]
    function_families = [
        {
            "boundary": policy.boundary,
            "reason_code": policy.reason_code,
            "path": path,
            "qualified_function": function,
        }
        for (path, function), policy in sorted(FUNCTION_POLICY.items())
    ]
    return {
        "version": POLICY_VERSION,
        "module_families": module_families,
        "function_families": function_families,
        "unclassified_rule": "A handler without an exact function or module policy is unclassified.",
    }


def _entry(path: Path, root: Path, line: int, kind: str, function: str) -> dict[str, object]:
    relative = path.relative_to(root).as_posix()
    policy = FUNCTION_POLICY.get((relative, function)) or MODULE_POLICY.get(relative)
    if policy is None:
        return {
            "path": relative,
            "line": line,
            "handler_kind": kind,
            "qualified_function": function,
            "boundary": None,
            "reason_code": "UNCLASSIFIED_POLICY_REQUIRED",
        }
    return {
        "path": relative,
        "line": line,
        "handler_kind": kind,
        "qualified_function": function,
        "boundary": policy.boundary,
        "reason_code": policy.reason_code,
    }


def build_artifact(root: Path) -> dict[str, object]:
    paths = _tracked_source_paths(root)
    entries = [
        _entry(path, root, line, kind, function)
        for path in paths
        for line, kind, function in _qualified_handlers(path, root)
    ]
    entries.sort(
        key=lambda row: (
            str(row["path"]), int(row["line"]), str(row["handler_kind"]), str(row["qualified_function"])
        )
    )
    counts = {boundary: sum(row["boundary"] == boundary for row in entries) for boundary in sorted(BOUNDARIES)}
    return {
        "schema": SCHEMA,
        "policy": _policy_document(),
        "source_fingerprint": _source_fingerprint(paths, root),
        "summary": {
            "total": len(entries),
            "unclassified": sum(row["boundary"] is None for row in entries),
            "by_boundary": counts,
            "by_handler_kind": {
                kind: sum(row["handler_kind"] == kind for row in entries)
                for kind in ("bare", "Exception", "BaseException")
            },
        },
        "entries": entries,
    }


def _canonical(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _assert_classified(value: dict[str, object]) -> None:
    summary = value["summary"]
    if not isinstance(summary, dict) or summary.get("unclassified") != 0:
        raise ValueError("unclassified exception handler policy")


def _write(path: Path, value: dict[str, object]) -> None:
    _assert_classified(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical(value), encoding="utf-8")


def _check(path: Path, value: dict[str, object]) -> bool:
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL exception-boundary-audit unreadable-artifact={type(exc).__name__}")
        return False
    try:
        _assert_classified(value)
    except ValueError:
        print("FAIL exception-boundary-audit unclassified-handler")
        return False
    if recorded != value:
        print("FAIL exception-boundary-audit drift=source-or-policy")
        return False
    print(
        "PASS exception-boundary-audit "
        f"entries={value['summary']['total']} fingerprint={value['source_fingerprint']}"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", type=Path, metavar="ARTIFACT")
    modes.add_argument("--write", type=Path, metavar="ARTIFACT")
    args = parser.parse_args(argv)
    root = _repository_root()
    value = build_artifact(root)
    if args.write is not None:
        _write(args.write, value)
        print(
            "WROTE exception-boundary-audit "
            f"entries={value['summary']['total']} fingerprint={value['source_fingerprint']}"
        )
        return 0
    return 0 if _check(args.check, value) else 1


if __name__ == "__main__":
    sys.exit(main())
