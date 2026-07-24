from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, NoReturn, Sequence


ROOT = Path(__file__).resolve().parents[1]
STATUS_TIMEOUT_SECONDS = 30.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
FAILURE_TAIL_LINES = 80
FAILURE_TAIL_CHARS = 12_000


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _fail(code: str, detail: str = "") -> NoReturn:
    raise SmokeFailure(code, detail)


def sanitized_environment(
    inherited: Mapping[str, str] | None = None,
    *,
    data_dir: Path | None = None,
) -> dict[str, str]:
    source = os.environ if inherited is None else inherited
    clean = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("AETHERSTATE_")
        and not key.upper().startswith("PYTHON")
        and key.upper() not in {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__"}
    }
    if data_dir is not None:
        clean["AETHERSTATE_SERVER__DATA_DIR"] = str(data_dir)
    return clean


def validate_status(payload: object, installed_version: str, temp_data_dir: Path) -> None:
    if not isinstance(payload, dict):
        _fail("status_type", "response")
    expected: dict[str, object] = {
        "name": "aetherstate",
        "version": installed_version,
        "mode": "enriched",
        "degradation": "none",
        "config_source": "defaults",
        "upstream_configured": False,
        "sessions": 0,
        "data_dir": str(temp_data_dir),
        "telemetry": "none, ever",
    }
    missing = [key for key in expected if key not in payload]
    if missing:
        _fail("status_missing", ",".join(missing))
    types: dict[str, type] = {
        "name": str,
        "version": str,
        "mode": str,
        "degradation": str,
        "config_source": str,
        "upstream_configured": bool,
        "sessions": int,
        "data_dir": str,
        "telemetry": str,
    }
    for key, expected_type in types.items():
        value = payload[key]
        if type(value) is not expected_type:
            _fail("status_type", key)
    if payload["upstream_configured"] is not False:
        _fail("status_upstream")
    if payload["telemetry"] != "none, ever":
        _fail("status_telemetry")
    if payload["data_dir"] != str(temp_data_dir):
        _fail("status_data_dir")
    for key, value in expected.items():
        if key not in {"upstream_configured", "telemetry", "data_dir"} and payload[key] != value:
            _fail("status_value", key)


def bounded_log_tail(
    log_path: Path,
    *,
    max_lines: int = FAILURE_TAIL_LINES,
    max_chars: int = FAILURE_TAIL_CHARS,
) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = "".join(text.splitlines(keepends=True)[-max(1, max_lines) :])
    return tail[-max(1, max_chars) :]


def require_within_temp_root(temp_root: Path, *paths: Path) -> None:
    root = temp_root.resolve()
    for path in paths:
        candidate = path.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            _fail("unsafe_temp_path", str(candidate))


def select_wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        _fail("wheel_count", f"found={len(wheels)}")
    return wheels[0]


def select_built_artifacts(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        _fail("artifact_count", f"wheels={len(wheels)} sdists={len(sdists)}")
    return wheels[0], sdists[0]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--build-source", type=Path)
    modes.add_argument("--wheel-dir", type=Path)
    modes.add_argument("--installed-smoke", action="store_true")
    return parser.parse_args(argv)


def _venv_python(venv_dir: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    executable = venv_dir / relative
    if not executable.is_file():
        _fail("venv_python_missing", str(executable))
    return executable


def _run_logged(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
) -> None:
    with log_path.open("wb") as log:
        result = subprocess.run(
            [str(part) for part in command],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode:
        tail = bounded_log_tail(log_path)
        _fail("command_failed", f"exit={result.returncode}\n{tail}")


def _create_venv(venv_dir: Path, *, temp_root: Path, env: Mapping[str, str], log_path: Path) -> Path:
    require_within_temp_root(temp_root, venv_dir, log_path)
    _run_logged(
        [sys.executable, "-m", "venv", venv_dir],
        cwd=temp_root,
        env=env,
        log_path=log_path,
    )
    return _venv_python(venv_dir)


def _install_and_smoke_wheel(
    wheel: Path,
    *,
    wheel_env: Path,
    temp_root: Path,
    env: Mapping[str, str],
) -> None:
    wheel_python = _create_venv(
        wheel_env,
        temp_root=temp_root,
        env=env,
        log_path=temp_root / "create-wheel-env.log",
    )
    _run_logged(
        [wheel_python, "-m", "pip", "install", wheel],
        cwd=temp_root,
        env=env,
        log_path=temp_root / "install-wheel.log",
    )
    _run_logged(
        [wheel_python, "-m", "pip", "check"],
        cwd=temp_root,
        env=env,
        log_path=temp_root / "pip-check.log",
    )
    _run_logged(
        [wheel_python, Path(__file__).resolve(), "--installed-smoke"],
        cwd=temp_root,
        env=env,
        log_path=temp_root / "installed-smoke.log",
    )


def _build_source(source: Path) -> None:
    source = source.resolve()
    if not source.is_dir():
        _fail("source_missing", str(source))
    env = sanitized_environment()
    with tempfile.TemporaryDirectory(prefix="aetherstate-clean-wheel-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        build_env = temp_root / "build-env"
        wheel_env = temp_root / "wheel-env"
        dist_dir = temp_root / "dist"
        require_within_temp_root(temp_root, build_env, wheel_env, dist_dir)
        dist_dir.mkdir()
        build_python = _create_venv(
            build_env,
            temp_root=temp_root,
            env=env,
            log_path=temp_root / "create-build-env.log",
        )
        _run_logged(
            [build_python, "-m", "pip", "install", "build"],
            cwd=temp_root,
            env=env,
            log_path=temp_root / "install-build.log",
        )
        _run_logged(
            [build_python, "-m", "build", "--outdir", dist_dir, source],
            cwd=temp_root,
            env=env,
            log_path=temp_root / "build.log",
        )
        wheel, _sdist = select_built_artifacts(dist_dir)
        _install_and_smoke_wheel(
            wheel,
            wheel_env=wheel_env,
            temp_root=temp_root,
            env=env,
        )
    print("PASS clean-wheel build-source")


def _wheel_dir(artifact_dir: Path) -> None:
    artifact_dir = artifact_dir.resolve()
    if not artifact_dir.is_dir():
        _fail("wheel_dir_missing", str(artifact_dir))
    wheel = select_wheel(artifact_dir)
    env = sanitized_environment()
    with tempfile.TemporaryDirectory(prefix="aetherstate-wheel-artifact-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        wheel_env = temp_root / "wheel-env"
        require_within_temp_root(temp_root, wheel_env)
        _install_and_smoke_wheel(
            wheel,
            wheel_env=wheel_env,
            temp_root=temp_root,
            env=env,
        )
    print("PASS clean-wheel wheel-dir")


def _installed_distribution() -> tuple[str, Path]:
    import aetherstate

    module_path = Path(aetherstate.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    try:
        module_path.relative_to(environment_root)
    except ValueError:
        _fail("module_outside_environment", str(module_path))
    distribution = importlib.metadata.distribution("aetherstate")
    version = distribution.version
    if not version or version == "unknown":
        _fail("version_unknown")
    if getattr(aetherstate, "__version__", None) != version:
        _fail("version_mismatch")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        try:
            direct_url_value = json.loads(direct_url)
        except json.JSONDecodeError:
            _fail("direct_url_invalid")
        if direct_url_value.get("dir_info", {}).get("editable") is True:
            _fail("editable_install")
    return version, module_path


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _poll_status(process: subprocess.Popen[bytes], port: int, log_path: Path) -> object:
    deadline = time.monotonic() + STATUS_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{port}/aether/status"
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            _fail(
                "server_exited",
                f"exit={returncode}\n{bounded_log_tail(log_path)}",
            )
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
    _fail("status_timeout", bounded_log_tail(log_path))


def _shutdown(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _prove_port_released(port: int) -> None:
    deadline = time.monotonic() + 10.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(("127.0.0.1", port))
                return
        except OSError as error:
            last_error = str(error)
            time.sleep(0.1)
    _fail("port_not_released", last_error)


def _installed_smoke() -> None:
    installed_version, _module_path = _installed_distribution()
    raw_temp_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="aetherstate-installed-smoke-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        raw_temp_path = temp_root
        try:
            temp_root.relative_to(ROOT.resolve())
        except ValueError:
            pass
        else:
            _fail("temp_inside_checkout", str(temp_root))
        config_path = temp_root / "config.toml"
        data_dir = temp_root / "data"
        log_path = temp_root / "server.log"
        port = _free_loopback_port()
        env = sanitized_environment(data_dir=data_dir)
        command = [
            sys.executable,
            "-I",
            "-m",
            "aetherstate",
            "--config",
            str(config_path),
            "--config-read-only",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=temp_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        try:
            status = _poll_status(process, port, log_path)
            validate_status(status, installed_version, data_dir)
            if not (data_dir / "aetherstate.db").is_file():
                _fail("database_missing")
            if config_path.exists() or config_path.with_suffix(".toml.bak").exists():
                _fail("config_written")
        finally:
            _shutdown(process)
        _prove_port_released(port)
    if raw_temp_path is None or raw_temp_path.exists():
        _fail("temp_cleanup")
    print("PASS clean-wheel installed-smoke")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.build_source is not None:
            _build_source(args.build_source)
        elif args.wheel_dir is not None:
            _wheel_dir(args.wheel_dir)
        else:
            _installed_smoke()
    except SmokeFailure as error:
        print(f"FAIL {error.code}", file=sys.stderr)
        if error.detail:
            print(error.detail[-FAILURE_TAIL_CHARS:], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
