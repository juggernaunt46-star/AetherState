from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import types
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


def test_run_logged_uses_bounded_capture_and_stable_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        kwargs["stdout"].write(b"old\n" * 300 + b"terminal failure\n")
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    log_path = tmp_path / "command.log"
    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._run_logged(
            ["python", "-m", "pip", "check"],
            cwd=tmp_path,
            env={"SAFE": "yes"},
            log_path=log_path,
        )

    assert caught.value.code == "command_failed"
    assert "terminal failure" in caught.value.detail
    assert len(caught.value.detail) <= smoke.FAILURE_TAIL_CHARS + 20
    assert calls[0]["command"] == ["python", "-m", "pip", "check"]
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["env"] == {"SAFE": "yes"}
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["stderr"] is subprocess.STDOUT
    assert calls[0]["check"] is False


def _fake_orchestration_runner(records: list[dict[str, object]]):
    def fake_run(command, *, cwd, env, log_path):
        command = [str(part) for part in command]
        records.append(
            {
                "command": command,
                "cwd": Path(cwd),
                "env": dict(env),
                "log_path": Path(log_path),
            }
        )
        if command[1:3] == ["-m", "venv"]:
            executable = Path(command[3]) / (
                Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
        if command[1:3] == ["-m", "build"]:
            dist_dir = Path(command[command.index("--outdir") + 1])
            (dist_dir / "aetherstate-1.24.0-py3-none-any.whl").write_bytes(b"wheel")
            (dist_dir / "aetherstate-1.24.0.tar.gz").write_bytes(b"sdist")

    return fake_run


def _record_real_containment(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    checked: list[tuple[Path, Path]] = []
    real = smoke.require_within_temp_root

    def wrapper(root: Path, *paths: Path) -> None:
        real(root, *paths)
        checked.extend((root.resolve(), path.resolve()) for path in paths)

    monkeypatch.setattr(smoke, "require_within_temp_root", wrapper)
    return checked


def test_build_source_orchestration_isolated_ordered_sanitized_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("AETHERSTATE_UPSTREAM__API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "poison")
    monkeypatch.setenv("VIRTUAL_ENV", "poison")
    records: list[dict[str, object]] = []
    monkeypatch.setattr(smoke, "_run_logged", _fake_orchestration_runner(records))
    checked = _record_real_containment(monkeypatch)

    smoke._build_source(source)

    commands = [record["command"] for record in records]
    assert [command[1:3] for command in commands] == [
        ["-m", "venv"],
        ["-m", "pip"],
        ["-m", "build"],
        ["-m", "venv"],
        ["-m", "pip"],
        ["-m", "pip"],
        [str(Path(__file__).resolve().parents[1] / "tools" / "smoke_clean_wheel.py"), "--installed-smoke"],
    ]
    assert commands[2][-1] == str(source.resolve())
    assert commands[5][-2:] == ["pip", "check"]
    build_env = Path(commands[0][3])
    wheel_env = Path(commands[3][3])
    assert build_env.name == "build-env"
    assert wheel_env.name == "wheel-env"
    assert build_env != wheel_env
    temp_root = Path(records[0]["cwd"])
    executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    build_python = build_env / executable
    wheel_python = wheel_env / executable
    assert commands[1] == [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(build_python),
        "install",
        "pip==25.2",
        "build",
    ]
    assert commands[4] == [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(wheel_python),
        "install",
        "pip==25.2",
        str(temp_root / "dist" / "aetherstate-1.24.0-py3-none-any.whl"),
    ]
    assert "--trusted-host" not in commands[1]
    assert "--trusted-host" not in commands[4]
    assert commands[2][0] == str(build_python)
    assert commands[5][0] == str(wheel_python)
    assert commands[6][0] == str(wheel_env / executable)
    assert all(
        "PYTHONPATH" not in record["env"]
        and "VIRTUAL_ENV" not in record["env"]
        and not any(key.startswith("AETHERSTATE_") for key in record["env"])
        for record in records
    )
    expected_names = {
        "build-env",
        "wheel-env",
        "dist",
        "create-build-env.log",
        "install-build.log",
        "build.log",
        "create-wheel-env.log",
        "install-wheel.log",
        "pip-check.log",
        "installed-smoke.log",
    }
    assert expected_names <= {path.name for _root, path in checked}
    assert all(root == temp_root and path.is_relative_to(root) for root, path in checked)
    assert not temp_root.exists()


def test_wheel_dir_orchestration_creates_only_wheel_environment_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    wheel = artifact_dir / "aetherstate-1.24.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    records: list[dict[str, object]] = []
    monkeypatch.setattr(smoke, "_run_logged", _fake_orchestration_runner(records))
    checked = _record_real_containment(monkeypatch)

    smoke._wheel_dir(artifact_dir)

    commands = [record["command"] for record in records]
    assert [command[1:3] for command in commands] == [
        ["-m", "venv"],
        ["-m", "pip"],
        ["-m", "pip"],
        [str(Path(__file__).resolve().parents[1] / "tools" / "smoke_clean_wheel.py"), "--installed-smoke"],
    ]
    assert commands[1][-1] == str(wheel)
    assert commands[2][-2:] == ["pip", "check"]
    assert all("build-env" not in " ".join(command) for command in commands)
    temp_root = Path(records[0]["cwd"])
    executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    wheel_python = temp_root / "wheel-env" / executable
    assert commands[1] == [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(wheel_python),
        "install",
        "pip==25.2",
        str(wheel),
    ]
    assert "--trusted-host" not in commands[1]
    assert commands[2][0] == str(wheel_python)
    assert commands[3][0] == str(wheel_python)
    assert {
        "wheel-env",
        "create-wheel-env.log",
        "install-wheel.log",
        "pip-check.log",
        "installed-smoke.log",
    } <= {path.name for _root, path in checked}
    assert not temp_root.exists()


def test_target_install_failure_aborts_without_fallback_and_cleans_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    wheel = artifact_dir / "aetherstate-1.24.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    records: list[list[str]] = []
    temp_roots: list[Path] = []

    def fail_install(command, *, cwd, env, log_path):
        command = [str(part) for part in command]
        records.append(command)
        temp_roots.append(Path(cwd))
        if command[1:3] == ["-m", "venv"]:
            executable = Path(command[3]) / (
                Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            return
        raise smoke.SmokeFailure("command_failed", "exit=1")

    monkeypatch.setattr(smoke, "_run_logged", fail_install)

    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._wheel_dir(artifact_dir)

    executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    wheel_python = temp_roots[0] / "wheel-env" / executable
    assert caught.value.code == "command_failed"
    assert records == [
        [sys.executable, "-m", "venv", str(temp_roots[0] / "wheel-env")],
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(wheel_python),
            "install",
            "pip==25.2",
            str(wheel),
        ],
    ]
    assert not temp_roots[0].exists()


def test_process_group_options_cover_windows_and_posix() -> None:
    windows = smoke._process_group_options("nt")
    posix = smoke._process_group_options("posix")

    assert windows == {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
        "start_new_session": False,
    }
    assert posix == {"creationflags": 0, "start_new_session": True}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death contract")
def test_linux_new_session_child_exits_when_exact_helper_parent_exits(tmp_path: Path) -> None:
    ready_path = tmp_path / "ready"
    pid_path = tmp_path / "pid"
    terminated_path = tmp_path / "terminated"
    leaked_path = tmp_path / "leaked"
    child_program = (
        "import pathlib, signal, sys, time\n"
        "ready, terminated, leaked = map(pathlib.Path, sys.argv[1:])\n"
        "def stop(_signum, _frame):\n"
        "    terminated.write_text('terminated', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "time.sleep(1.0)\n"
        "leaked.write_text('leaked', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    helper_program = (
        "import os, pathlib, subprocess, sys, time\n"
        "from tools import smoke_clean_wheel as smoke\n"
        "ready, pid_path, terminated, leaked = map(pathlib.Path, sys.argv[1:])\n"
        f"child_program = {child_program!r}\n"
        "factory = getattr(smoke, '_linux_parent_death_preexec', lambda _parent: None)\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_program, str(ready), str(terminated), str(leaked)],\n"
        "    start_new_session=True,\n"
        "    preexec_fn=factory(os.getpid()),\n"
        ")\n"
        "deadline = time.monotonic() + 5\n"
        "while not ready.exists() and child.poll() is None and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not ready.exists():\n"
        "    child.kill()\n"
        "    raise SystemExit(2)\n"
        "pid_path.write_text(str(child.pid), encoding='utf-8')\n"
    )
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            helper_program,
            str(ready_path),
            str(pid_path),
            str(terminated_path),
            str(leaked_path),
        ],
        cwd=smoke.ROOT,
    )
    child_pid: int | None = None
    try:
        assert helper.wait(timeout=10) == 0
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while not terminated_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert terminated_path.is_file()
        time.sleep(1.1)
        assert not leaked_path.exists()
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_linux_parent_death_setup_fails_closed_when_prctl_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Prctl:
        restype = None

        def __call__(self, *_args: int) -> int:
            return -1

    monkeypatch.setattr(smoke.ctypes, "CDLL", lambda *_args, **_kwargs: types.SimpleNamespace(prctl=Prctl()))
    monkeypatch.setattr(smoke.ctypes, "get_errno", lambda: 38)

    setup = smoke._linux_parent_death_preexec(os.getpid())

    with pytest.raises(OSError) as caught:
        setup()

    assert caught.value.errno == 38


def test_linux_parent_death_setup_checks_parent_after_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Prctl:
        restype = None

        def __call__(self, *args: int) -> int:
            events.append(("prctl", args))
            return 0

    class ParentChanged(BaseException):
        pass

    monkeypatch.setattr(smoke.ctypes, "CDLL", lambda *_args, **_kwargs: types.SimpleNamespace(prctl=Prctl()))
    monkeypatch.setattr(smoke.os, "getppid", lambda: events.append(("getppid",)) or 222)
    monkeypatch.setattr(
        smoke.os,
        "_exit",
        lambda code: events.append(("exit", code)) or (_ for _ in ()).throw(ParentChanged()),
    )

    setup = smoke._linux_parent_death_preexec(111)

    with pytest.raises(ParentChanged):
        setup()

    assert events == [
        ("prctl", (1, signal.SIGTERM, 0, 0, 0)),
        ("getppid",),
        ("exit", 1),
    ]


def test_installed_distribution_requires_venv_noneditable_matching_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "wheel-env"
    module_path = environment / "Lib" / "site-packages" / "aetherstate" / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_bytes(b"")
    module = types.SimpleNamespace(__file__=str(module_path), __version__="1.24.0")
    distribution = types.SimpleNamespace(
        version="1.24.0",
        read_text=lambda name: json.dumps({"archive_info": {}}) if name == "direct_url.json" else None,
    )
    monkeypatch.setitem(sys.modules, "aetherstate", module)
    monkeypatch.setattr(smoke.sys, "prefix", str(environment))
    monkeypatch.setattr(smoke.importlib.metadata, "distribution", lambda name: distribution)

    assert smoke._installed_distribution() == ("1.24.0", module_path.resolve())

    distribution.read_text = lambda name: json.dumps({"dir_info": {"editable": True}})
    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._installed_distribution()
    assert caught.value.code == "editable_install"


def test_poll_status_uses_exact_endpoint_and_fails_immediately_on_child_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requested: list[tuple[str, float]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"name":"aetherstate"}'

    monkeypatch.setattr(
        smoke.urllib.request,
        "urlopen",
        lambda url, timeout: requested.append((url, timeout)) or Response(),
    )
    process = types.SimpleNamespace(poll=lambda: None)
    assert smoke._poll_status(process, 43210, tmp_path / "server.log") == {"name": "aetherstate"}
    assert requested == [("http://127.0.0.1:43210/aether/status", 1.0)]

    exited = types.SimpleNamespace(poll=lambda: 23)
    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._poll_status(exited, 43210, tmp_path / "server.log")
    assert caught.value.code == "server_exited"
    assert "exit=23" in caught.value.detail


def test_shutdown_requests_graceful_then_terminate_and_kill_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Process:
        pid = 9876

        def poll(self):
            return None

        def send_signal(self, value):
            events.append(("signal", value))

        def wait(self, timeout):
            events.append(("wait", timeout))
            if len([event for event in events if event[0] == "wait"]) < 3:
                raise subprocess.TimeoutExpired("server", timeout)
            return 0

        def terminate(self):
            events.append(("terminate",))

        def kill(self):
            events.append(("kill",))

    if os.name != "nt":
        monkeypatch.setattr(smoke.os, "killpg", lambda pid, value: events.append(("killpg", pid, value)))
    smoke._shutdown(Process())

    if os.name == "nt":
        assert events[0] == ("signal", signal.CTRL_BREAK_EVENT)
    else:
        assert events[0] == ("killpg", 9876, signal.SIGINT)
    assert events[1:] == [
        ("wait", smoke.SHUTDOWN_TIMEOUT_SECONDS),
        ("terminate",),
        ("wait", 5),
        ("kill",),
        ("wait", 5),
    ]


def _install_smoke_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_database: bool,
    create_config: bool = False,
):
    state: dict[str, object] = {"shutdown": [], "ports": [], "processes": []}
    monkeypatch.setattr(smoke, "_installed_distribution", lambda: ("1.24.0", Path("installed")))
    monkeypatch.setattr(smoke, "_free_loopback_port", lambda: 45678)

    class Process:
        def __init__(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            state["processes"].append(self)

        def poll(self):
            return None

    monkeypatch.setattr(smoke.subprocess, "Popen", Process)

    def poll(process, port, log_path):
        data_dir = Path(process.kwargs["env"]["AETHERSTATE_SERVER__DATA_DIR"])
        if create_database:
            data_dir.mkdir(parents=True)
            (data_dir / "aetherstate.db").write_bytes(b"db")
        if create_config:
            Path(process.command[process.command.index("--config") + 1]).write_text("changed")
        return _valid_status("1.24.0", data_dir)

    monkeypatch.setattr(smoke, "_poll_status", poll)
    monkeypatch.setattr(smoke, "_shutdown", lambda process: state["shutdown"].append(process))
    monkeypatch.setattr(smoke, "_prove_port_released", lambda port: state["ports"].append(port))
    return state


def test_installed_smoke_proves_exact_process_status_artifacts_port_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AETHERSTATE_UPSTREAM__API_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "poison")
    state = _install_smoke_fakes(monkeypatch, create_database=True)
    checked = _record_real_containment(monkeypatch)

    smoke._installed_smoke()

    process = state["processes"][0]
    command = process.command
    assert command[:4] == [sys.executable, "-I", "-m", "aetherstate"]
    assert command[command.index("--config-read-only")] == "--config-read-only"
    assert command[-4:] == ["--host", "127.0.0.1", "--port", "45678"]
    env = process.kwargs["env"]
    assert {key for key in env if key.startswith("AETHERSTATE_")} == {
        "AETHERSTATE_SERVER__DATA_DIR"
    }
    assert "PYTHONPATH" not in env
    assert process.kwargs["creationflags"] == smoke._process_group_options(os.name)["creationflags"]
    assert process.kwargs["start_new_session"] == smoke._process_group_options(os.name)["start_new_session"]
    temp_root = Path(process.kwargs["cwd"])
    assert {"config.toml", "data", "server.log"} <= {path.name for _root, path in checked}
    assert state["shutdown"] == [process]
    assert state["ports"] == [45678]
    assert not temp_root.exists()


@pytest.mark.parametrize(
    ("create_database", "create_config", "code"),
    [(False, False, "database_missing"), (True, True, "config_written")],
)
def test_installed_smoke_failure_still_shuts_down_and_cleans_scoped_root(
    monkeypatch: pytest.MonkeyPatch,
    create_database: bool,
    create_config: bool,
    code: str,
) -> None:
    state = _install_smoke_fakes(
        monkeypatch,
        create_database=create_database,
        create_config=create_config,
    )

    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._installed_smoke()

    process = state["processes"][0]
    assert caught.value.code == code
    assert state["shutdown"] == [process]
    assert state["ports"] == [45678]
    assert not Path(process.kwargs["cwd"]).exists()


def test_free_selected_port_is_proven_released() -> None:
    smoke._prove_port_released(smoke._free_loopback_port())


@pytest.mark.skipif(os.name == "nt", reason="POSIX TIME_WAIT release semantics")
def test_posix_port_release_proof_accepts_refusal_despite_time_wait_rebind_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter([0.0, 0.0, 11.0])
    bind_attempts: list[tuple[str, int]] = []

    class TimeWaitSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args):
            return None

        def bind(self, address):
            bind_attempts.append(address)
            raise OSError(98, "Address already in use")

    def refuse_connection(*_args, **_kwargs):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(smoke.time, "sleep", lambda _value: None)
    monkeypatch.setattr(smoke.socket, "socket", lambda *_args: TimeWaitSocket())
    monkeypatch.setattr(smoke.socket, "create_connection", refuse_connection)

    smoke._prove_port_released(45678)

    assert bind_attempts == []


def test_port_release_failure_is_bounded_with_socket_and_time_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter([0.0, 0.0, 11.0])
    sleeps: list[float] = []
    connections: list[tuple[tuple[str, int], float]] = []

    class BusySocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def setsockopt(self, *_args):
            return None

        def bind(self, _address):
            raise OSError("still busy")

    class AcceptedConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def accept_connection(address, timeout):
        connections.append((address, timeout))
        return AcceptedConnection()

    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(smoke.time, "sleep", lambda value: sleeps.append(value))
    if os.name == "nt":
        monkeypatch.setattr(smoke.socket, "socket", lambda *_args: BusySocket())
    else:
        monkeypatch.setattr(smoke.socket, "create_connection", accept_connection)

    with pytest.raises(smoke.SmokeFailure) as caught:
        smoke._prove_port_released(45678)

    assert caught.value.code == "port_not_released"
    if os.name == "nt":
        assert "still busy" in caught.value.detail
        assert connections == []
    else:
        assert "listener_still_accepting" in caught.value.detail
        assert connections == [(("127.0.0.1", 45678), 0.5)]
    assert sleeps == [0.1]
