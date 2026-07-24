from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_SOURCE = ROOT / "st-extension"
REQUIRED_EXTENSION_FILES = ("manifest.json", "index.js", "style.css")
COMPLETION = "Install-only verification complete."
STARTING = "Starting AetherState setup and Console..."


def _cleanup_fake_root(fake_root: Path, tmp_path: Path) -> None:
    resolved_tmp = tmp_path.resolve()
    resolved_fake = fake_root.resolve()
    if resolved_fake == resolved_tmp:
        raise AssertionError("fake root must be a child of pytest tmp_path")
    try:
        resolved_fake.relative_to(resolved_tmp)
    except ValueError as error:
        raise AssertionError("fake root escaped pytest tmp_path") from error
    if resolved_fake.exists():
        shutil.rmtree(resolved_fake)
    assert not resolved_fake.exists()


def _server_processes() -> set[tuple[int, str]]:
    if sys.platform != "win32":
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
        found: set[tuple[int, str]] = set()
        for line in result.stdout.splitlines():
            pid_text, _, command = line.strip().partition(" ")
            if "-m aetherstate" in command or command.endswith("/aetherstate"):
                found.add((int(pid_text), command))
        return found

    script = (
        "$rows = @(Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -match '(^|\\s)-m\\s+aetherstate(\\s|$)' -or "
        "$_.Name -ieq 'aetherstate.exe' } | "
        "ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; command=[string]$_.CommandLine} }); "
        "$rows | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
    )
    if not result.stdout.strip():
        return set()
    decoded = json.loads(result.stdout)
    rows = decoded if isinstance(decoded, list) else [decoded]
    return {(int(row["pid"]), str(row["command"])) for row in rows}


def _assert_installed(fake_root: Path, output: str, before: set[tuple[int, str]]) -> None:
    destination = fake_root / "data" / "default-user" / "extensions" / "AetherState"
    for filename in REQUIRED_EXTENSION_FILES:
        assert (destination / filename).read_bytes() == (EXTENSION_SOURCE / filename).read_bytes()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert lines.count(COMPLETION) == 1
    assert lines[-1] == COMPLETION
    assert STARTING not in output
    assert _server_processes() == before


def test_fake_root_cleanup_is_scoped_and_runs_during_failure(tmp_path: Path) -> None:
    fake_root = tmp_path / "Failure Fake SillyTavern"
    fake_root.mkdir()

    with pytest.raises(RuntimeError, match="synthetic failure"):
        try:
            raise RuntimeError("synthetic failure")
        finally:
            _cleanup_fake_root(fake_root, tmp_path)

    assert not fake_root.exists()
    with pytest.raises(AssertionError, match="escaped"):
        _cleanup_fake_root(tmp_path.parent / "not-owned", tmp_path)


@pytest.mark.parametrize("platform_name", ["win32", "linux"])
def test_explicit_install_only_contract(
    platform_name: str,
    tmp_path: Path,
) -> None:
    if platform_name != sys.platform:
        pytest.skip("opposite-platform installer")

    fake_root = tmp_path / "Fake SillyTavern"
    (fake_root / "data" / "default-user").mkdir(parents=True)
    before = _server_processes()
    if platform_name == "win32":
        command = [
            "cmd.exe",
            "/d",
            "/c",
            "Install-AetherState.bat",
            str(fake_root),
            "--install-only",
        ]
    else:
        command = ["bash", str(ROOT / "install-aetherstate.sh"), str(fake_root), "--install-only"]

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        _assert_installed(fake_root, result.stdout + result.stderr, before)
    finally:
        _cleanup_fake_root(fake_root, tmp_path)


def test_install_only_discovers_sillytavern_dir_from_environment(tmp_path: Path) -> None:
    fake_root = tmp_path / "Discovered SillyTavern"
    (fake_root / "data" / "default-user").mkdir(parents=True)
    before = _server_processes()
    env = os.environ.copy()
    env["SILLYTAVERN_DIR"] = str(fake_root)
    if sys.platform == "win32":
        command = [
            "cmd.exe",
            "/d",
            "/c",
            "Install-AetherState.bat",
            "--install-only",
        ]
    else:
        command = ["bash", str(ROOT / "install-aetherstate.sh"), "--install-only"]

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        _assert_installed(fake_root, result.stdout + result.stderr, before)
    finally:
        _cleanup_fake_root(fake_root, tmp_path)
