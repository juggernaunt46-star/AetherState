from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import smoke_clean_wheel as smoke


def _valid_status(version: str, data_dir: Path) -> dict[str, object]:
    return {
        "name": "aetherstate",
        "version": version,
        "mode": "enriched",
        "degradation": "none",
        "config_source": "defaults",
        "upstream_configured": False,
        "sessions": 0,
        "data_dir": str(data_dir),
        "telemetry": "none, ever",
    }


def test_sanitized_environment_removes_inherited_runtime_and_python_overrides(tmp_path: Path) -> None:
    inherited = {
        "AETHERSTATE_UPSTREAM__API_KEY": "secret",
        "AETHERSTATE_SERVER__PORT": "9999",
        "AETHERSTATE_SERVER__DATA_DIR": "old-data",
        "PYTHONPATH": "old-path",
        "PYTHONHOME": "old-home",
        "VIRTUAL_ENV": "old-venv",
        "PATH": os.environ.get("PATH", ""),
        "UNCHANGED": "kept",
    }

    clean = smoke.sanitized_environment(inherited, data_dir=tmp_path / "data")

    assert clean["UNCHANGED"] == "kept"
    assert "PYTHONPATH" not in clean
    assert "PYTHONHOME" not in clean
    assert "VIRTUAL_ENV" not in clean
    assert {key for key in clean if key.startswith("AETHERSTATE_")} == {
        "AETHERSTATE_SERVER__DATA_DIR"
    }
    assert clean["AETHERSTATE_SERVER__DATA_DIR"] == str(tmp_path / "data")


def test_sanitized_environment_restores_no_aetherstate_override_without_data_dir() -> None:
    clean = smoke.sanitized_environment(
        {"AETHERSTATE_SERVER__DATA_DIR": "old", "AETHERSTATE_OTHER": "old", "SAFE": "yes"}
    )

    assert clean == {"SAFE": "yes"}


def test_exact_installed_status_contract_is_accepted(tmp_path: Path) -> None:
    smoke.validate_status(_valid_status("1.24.0", tmp_path / "data"), "1.24.0", tmp_path / "data")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda status: status.pop("sessions"), "status_missing"),
        (lambda status: status.__setitem__("sessions", False), "status_type"),
        (lambda status: status.__setitem__("version", "unknown"), "status_value"),
        (lambda status: status.__setitem__("upstream_configured", True), "status_upstream"),
        (lambda status: status.__setitem__("telemetry", "enabled"), "status_telemetry"),
        (lambda status: status.__setitem__("data_dir", "somewhere-else"), "status_data_dir"),
    ],
)
def test_status_contract_rejects_drift_with_stable_code(
    tmp_path: Path,
    mutation,
    code: str,
) -> None:
    status = _valid_status("1.24.0", tmp_path / "data")
    mutation(status)

    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke.validate_status(status, "1.24.0", tmp_path / "data")

    assert caught.value.code == code


def test_failure_log_tail_is_bounded(tmp_path: Path) -> None:
    log_path = tmp_path / "failure.log"
    log_path.write_text(
        "".join(f"{index:04d}-" + ("x" * 100) + "\n" for index in range(300)),
        encoding="utf-8",
    )

    tail = smoke.bounded_log_tail(log_path, max_lines=12, max_chars=800)

    assert len(tail) <= 800
    assert len(tail.splitlines()) <= 12
    assert "0299-" in tail
    assert "0000-" not in tail


def test_only_three_cli_modes_are_accepted(tmp_path: Path) -> None:
    assert smoke.parse_args(["--build-source", "."]).build_source == Path(".")
    assert smoke.parse_args(["--wheel-dir", str(tmp_path)]).wheel_dir == tmp_path
    assert smoke.parse_args(["--installed-smoke"]).installed_smoke is True

    with pytest.raises(SystemExit):
        smoke.parse_args([])
    with pytest.raises(SystemExit):
        smoke.parse_args(["--installed-smoke", "--build-source", "."])
    with pytest.raises(SystemExit):
        smoke.parse_args(["--unknown"])


def test_resolved_paths_must_remain_under_unique_temp_root(tmp_path: Path) -> None:
    child = tmp_path / "wheel-env"
    child.mkdir()
    smoke.require_within_temp_root(tmp_path, child)

    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke.require_within_temp_root(tmp_path, tmp_path.parent)

    assert caught.value.code == "unsafe_temp_path"


def test_artifact_selection_requires_exactly_one_wheel_and_sdist(tmp_path: Path) -> None:
    wheel = tmp_path / "aetherstate-1.24.0-py3-none-any.whl"
    sdist = tmp_path / "aetherstate-1.24.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    assert smoke.select_built_artifacts(tmp_path) == (wheel, sdist)

    (tmp_path / "duplicate.whl").write_bytes(b"wheel")
    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke.select_built_artifacts(tmp_path)
    assert caught.value.code == "artifact_count"


def test_wheel_directory_selection_requires_exactly_one_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "aetherstate-1.24.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assert smoke.select_wheel(tmp_path) == wheel

    (tmp_path / "duplicate.whl").write_bytes(b"wheel")
    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke.select_wheel(tmp_path)
    assert caught.value.code == "wheel_count"
