from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = (
    ROOT / "docs" / "hardening" / "post-1.24"
    / "stage-4-exception-boundary-audit.json"
)
TOOL = ROOT / "tools" / "audit_exception_boundaries.py"
ALLOWED_BOUNDARIES = {
    "streaming",
    "rollback",
    "shutdown",
    "optional_cognition",
    "availability_boundary",
}
FORBIDDEN_ARTIFACT_KEYS = {
    "source_text",
    "exception_message",
    "locals",
    "request",
    "prompt",
    "response",
    "credential",
    "absolute_path",
}


def _load_audit_tool():
    name = "stage4_exception_boundary_audit"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _assert_no_forbidden_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in FORBIDDEN_ARTIFACT_KEYS, key
            _assert_no_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_keys(item)


def _tracked_source_paths() -> tuple[Path, ...]:
    output = subprocess.run(
        ["git", "ls-files", "src/aetherstate/*.py", "src/aetherstate/**/*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return tuple(ROOT / item for item in sorted(set(output.splitlines())) if item)


def _broad_handler_count() -> int:
    count = 0
    for path in _tracked_source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if node.type is None:
                count += 1
            elif isinstance(node.type, ast.Name) and node.type.id in {
                "Exception",
                "BaseException",
            }:
                count += 1
    return count


def test_exception_boundary_artifact_accounts_for_every_broad_handler() -> None:
    value = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    entries = value["entries"]

    assert value["schema"] == "aetherstate-exception-boundary-audit/1"
    assert value["summary"]["unclassified"] == 0
    assert value["summary"]["total"] == len(entries) == _broad_handler_count()
    assert {entry["boundary"] for entry in entries} <= ALLOWED_BOUNDARIES
    assert all(entry["reason_code"] for entry in entries)
    assert all(entry["path"].startswith("src/aetherstate/") for entry in entries)
    assert all("\\" not in entry["path"] and ":" not in entry["path"] for entry in entries)


def test_exception_boundary_artifact_is_deterministic_and_current() -> None:
    result = subprocess.run(
        [sys.executable, str(TOOL), "--check", str(ARTIFACT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().startswith("PASS exception-boundary-audit ")


def test_exception_boundary_artifact_contains_no_source_or_payload_fields() -> None:
    value = json.loads(ARTIFACT.read_text(encoding="utf-8"))

    _assert_no_forbidden_keys(value)
    assert str(ROOT) not in json.dumps(value, sort_keys=True)


def test_forbidden_key_guard_rejects_nested_artifact_field() -> None:
    with pytest.raises(AssertionError, match="source_text"):
        _assert_no_forbidden_keys({"safe": [{"source_text": "synthetic"}]})


def test_lifespan_policy_distinguishes_startup_and_shutdown_by_yield_phase(
    tmp_path: Path,
) -> None:
    path = tmp_path / "src" / "aetherstate" / "app.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "async def create_app():\n"
        "    async def lifespan():\n"
        "        try:\n"
        "            startup()\n"
        "        except Exception:\n"
        "            pass\n"
        "        yield\n"
        "        try:\n"
        "            shutdown()\n"
        "        except Exception:\n"
        "            pass\n",
        encoding="utf-8",
    )
    audit = _load_audit_tool()

    entries = [
        audit._entry(path, tmp_path, line, kind, function)
        for line, kind, function in audit._qualified_handlers(path, tmp_path)
    ]

    assert [
        (entry["boundary"], entry["reason_code"])
        for entry in entries
    ] == [
        ("availability_boundary", "APPLICATION_LIFESPAN_STARTUP_AVAILABILITY"),
        ("shutdown", "APPLICATION_LIFESPAN_SHUTDOWN"),
    ]
