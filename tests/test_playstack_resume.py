from __future__ import annotations

import json
from pathlib import Path

import pytest

from aetherstate.playstack import (
    StackController,
    StackError,
    StackPaths,
    validate_resumable_isolated_root,
)


def _normal_roots(release: Path) -> list[Path]:
    return [release / "AetherState-personal" / "aetherstate-data", release / "SillyTavern" / "data"]


def test_resumable_root_requires_existing_launcher_owned_layout(tmp_path: Path) -> None:
    release = tmp_path / "release"
    root = release / "Local-Only" / "Live-Testing" / "run-01"
    (root / "aetherstate").mkdir(parents=True)
    (root / "sillytavern").mkdir()

    assert validate_resumable_isolated_root(
        root,
        _normal_roots(release),
        release,
        lexical_root=root,
    ) == root.resolve()


@pytest.mark.parametrize("missing", ("aetherstate", "sillytavern"))
def test_resumable_root_rejects_partial_layout(tmp_path: Path, missing: str) -> None:
    release = tmp_path / "release"
    root = release / "Local-Only" / "Live-Testing" / "run-01"
    root.mkdir(parents=True)
    for name in ("aetherstate", "sillytavern"):
        if name != missing:
            (root / name).mkdir()

    with pytest.raises(StackError, match="launcher-owned data directories"):
        validate_resumable_isolated_root(
            root,
            _normal_roots(release),
            release,
            lexical_root=root,
        )


def test_resumable_root_still_rejects_outside_local_only(tmp_path: Path) -> None:
    release = tmp_path / "release"
    root = release / "elsewhere" / "run-01"
    (root / "aetherstate").mkdir(parents=True)
    (root / "sillytavern").mkdir()

    with pytest.raises(StackError, match="outside"):
        validate_resumable_isolated_root(
            root,
            _normal_roots(release),
            release,
            lexical_root=root,
        )


class _StatusSystem:
    def listeners(self) -> dict[int, set[int]]:
        return {}

    def process_info(self, pid: int):
        return None


def test_isolated_status_rejects_a_different_recorded_root(tmp_path: Path) -> None:
    release = tmp_path / "release"
    active = release / "Local-Only" / "Live-Testing" / "active"
    requested = release / "Local-Only" / "Live-Testing" / "different"
    active.mkdir(parents=True)
    paths = StackPaths.for_release_root(
        release,
        node_executable=tmp_path / "node.exe",
        isolated_root=requested,
    )
    paths.state_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "source_version": "test",
                "isolated_root": str(active.resolve()),
                "processes": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StackError, match="does not match the recorded isolated root"):
        StackController(paths, system=_StatusSystem()).status()
