from __future__ import annotations

import json
import stat
import sys
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path

import pytest

from aetherstate import playstack, process_tee
from aetherstate.playstack import (
    _console_text,
    _default_release_root,
    _stop_instruction,
    HttpResult,
    ProcessInfo,
    StackController,
    StackError,
    StackPaths,
    parse_netstat,
    read_process_log_tail,
    seed_isolated_sillytavern_profile,
    sync_extension,
    validate_isolated_root,
)


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _bench(tmp_path: Path, *, isolated: bool = False) -> StackPaths:
    release = tmp_path / "release"
    personal = release / "AetherState-personal"
    nli = release / "nli-shim"
    st = release / "SillyTavern"
    _touch(personal / "src" / "aetherstate" / "__init__.py")
    _touch(personal / ".venv" / "Scripts" / "python.exe")
    _touch(
        personal / "aetherstate-data" / "config.toml",
        '[upstream]\nmodel = "fixture-narrator-model"\ncredential_ref = "opaque-test-ref"\n',
    )
    _touch(nli / ".venv" / "Scripts" / "python.exe")
    _touch(nli / "server.py")
    _touch(nli / "selected-backend.txt", "factcg\n")
    _touch(st / "server.js")
    _touch(
        st / "default" / "content" / "settings.json",
        json.dumps({
            "main_api": "koboldhorde",
            "amount_gen": 350,
            "max_context": 8192,
            "power_user": {},
            "extension_settings": {},
            "oai_settings": {
                "preset_settings_openai": "Default",
                "temp_openai": 1.0,
                "top_p_openai": 1.0,
                "top_k_openai": 0,
                "min_p_openai": 0,
                "openai_max_context": 4095,
                "openai_max_tokens": 300,
                "max_context_unlocked": False,
                "stream_openai": True,
            },
        }),
    )
    _touch(
        st / "default" / "content" / "presets" / "openai" / "Default.json",
        json.dumps({
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": 0,
            "min_p": 0,
            "openai_max_context": 4095,
            "openai_max_tokens": 300,
            "max_context_unlocked": False,
            "stream_openai": True,
        }),
    )
    _touch(release / "node.exe")
    _touch(personal / "st-extension" / "manifest.json", '{"name":"AetherState"}\n')
    _touch(personal / "st-extension" / "index.js", "// current build\n")
    _touch(personal / "st-extension" / "style.css", "/* current build */\n")
    isolated_root = (
        release / "Local-Only" / "playtest-roots" / "case-001"
        if isolated
        else None
    )
    return StackPaths.for_release_root(
        release,
        node_executable=release / "node.exe",
        isolated_root=isolated_root,
    )


def _public_bench(tmp_path: Path, *, isolated: bool = False) -> StackPaths:
    project = tmp_path / "AetherState"
    nli = project / "nli-shim"
    st = tmp_path / "SillyTavern"
    _touch(project / "src" / "aetherstate" / "__init__.py")
    _touch(project / ".venv" / "Scripts" / "python.exe")
    _touch(
        project / "aetherstate-data" / "config.toml",
        '[upstream]\nmodel = "public-fixture-model"\ncredential_ref = "opaque-test-ref"\n',
    )
    _touch(nli / ".venv" / "Scripts" / "python.exe")
    _touch(nli / "server.py")
    _touch(nli / "selected-backend.txt", "factcg\n")
    _touch(st / "server.js")
    _touch(
        st / "default" / "content" / "settings.json",
        json.dumps({
            "main_api": "koboldhorde",
            "power_user": {},
            "extension_settings": {},
            "oai_settings": {},
        }),
    )
    _touch(
        st / "default" / "content" / "presets" / "openai" / "Default.json",
        "{}",
    )
    _touch(project / "node.exe")
    _touch(project / "st-extension" / "manifest.json", '{"name":"AetherState"}\n')
    _touch(project / "st-extension" / "index.js", "// current build\n")
    _touch(project / "st-extension" / "style.css", "/* current build */\n")
    isolated_root = project / "Local-Only" / "playtest-roots" / "case-001" if isolated else None
    return StackPaths.for_release_root(
        project,
        node_executable=project / "node.exe",
        isolated_root=isolated_root,
        environment={},
    )


@dataclass
class _FakeProcess:
    info: ProcessInfo
    service: str
    port: int


class FakeSystem:
    def __init__(self) -> None:
        self.time = 0.0
        self.next_pid = 100
        self.processes: dict[int, _FakeProcess] = {}
        self.port_pids: dict[int, set[int]] = {}
        self.responses: dict[str, list[HttpResult | Exception]] = {}
        self.spawned: list[str] = []
        self.environments: dict[str, dict[str, str]] = {}
        self.terminated: list[int] = []
        self.opened: list[str] = []

    def listeners(self) -> dict[int, set[int]]:
        return {port: set(pids) for port, pids in self.port_pids.items() if pids}

    def process_info(self, pid: int) -> ProcessInfo | None:
        process = self.processes.get(pid)
        return process.info if process else None

    def spawn(self, spec, env) -> ProcessInfo:
        self.environments[spec.name] = dict(env)
        pid = self.next_pid
        self.next_pid += 1
        info = ProcessInfo(
            pid=pid,
            command_line=(
                f'"{sys.executable}" -m aetherstate.process_tee --service {spec.name} '
                f'-- "{spec.executable}" {spec.signature}'
            ),
            creation_time=f"2026071212{pid:04d}.000000-000",
        )
        self.processes[pid] = _FakeProcess(info=info, service=spec.name, port=spec.port)
        self.port_pids.setdefault(spec.port, set()).add(pid)
        self.spawned.append(spec.name)
        return info

    def terminate(self, pid: int) -> bool:
        process = self.processes.pop(pid, None)
        self.terminated.append(pid)
        if process:
            self.port_pids.get(process.port, set()).discard(pid)
            return True
        return False

    def request(self, url: str, timeout_s: float = 2.0) -> HttpResult:
        del timeout_s
        values = self.responses.get(url)
        if not values:
            raise ConnectionError(url)
        value = values[0] if len(values) == 1 else values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def monotonic(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.time += seconds

    def open_url(self, url: str) -> None:
        self.opened.append(url)


def _ready(system: FakeSystem, paths: StackPaths) -> None:
    system.responses[f"http://127.0.0.1:{paths.nli_port}/"] = [
        HttpResult(200, {"status": "ok", "backend": "factcg"})
    ]
    system.responses[f"http://127.0.0.1:{paths.proxy_port}/aether/status"] = [
        HttpResult(
            200,
            {
                "name": "aetherstate",
                "version": "1.23.0",
                "data_dir": str(paths.aetherstate_data_dir),
                "upstream_configured": True,
            },
        )
    ]
    system.responses[f"http://127.0.0.1:{paths.sillytavern_port}/"] = [
        HttpResult(200, None)
    ]
    system.responses[f"http://127.0.0.1:{paths.proxy_port}/v1/models"] = [
        HttpResult(200, {"object": "list", "data": []})
    ]


def _controller(
    paths: StackPaths,
    system: FakeSystem,
    *,
    parent_environment: dict[str, str] | None = None,
) -> StackController:
    kwargs = {}
    if parent_environment is not None:
        kwargs["parent_environment"] = parent_environment
    return StackController(
        paths,
        system=system,
        poll_interval_s=0.01,
        readiness_timeouts={"nli": 0.03, "proxy": 0.03, "sillytavern": 0.03, "route": 0.03},
        **kwargs,
    )


def test_parse_netstat_uses_exact_local_listening_ports() -> None:
    output = """
      TCP    127.0.0.1:9130     0.0.0.0:0       LISTENING       440
      TCP    127.0.0.1:49130    127.0.0.1:9130  ESTABLISHED     441
      TCP    [::1]:8000         [::]:0          LISTENING       442
      UDP    0.0.0.0:8199       *:*                             443
      TCP    127.0.0.1:8199     0.0.0.0:0       LISTENING       444
    """

    assert parse_netstat(output) == {9130: {440}, 8000: {442}, 8199: {444}}


def test_personal_and_live_test_stacks_use_distinct_ports_and_state_files(
        tmp_path: Path) -> None:
    personal = _bench(tmp_path)
    live_test = _bench(tmp_path, isolated=True)

    assert personal.ports == (8199, 9130, 8000)
    assert personal.state_path == personal.release_root / ".aetherstate-play-stack.json"
    assert live_test.ports == (18199, 19130, 18000)
    assert live_test.state_path == (
        live_test.release_root / ".aetherstate-live-test-stack.json"
    )
    assert personal.state_path != live_test.state_path


def test_public_checkout_layout_uses_project_local_nli_and_sibling_sillytavern(
        tmp_path: Path) -> None:
    paths = _public_bench(tmp_path)
    project = tmp_path / "AetherState"

    assert paths.release_root == project.resolve()
    assert paths.project_dir == project.resolve()
    assert paths.personal_dir == paths.project_dir
    assert paths.nli_dir == (project / "nli-shim").resolve()
    assert paths.sillytavern_dir == (tmp_path / "SillyTavern").resolve()
    assert paths.personal_python == project / ".venv" / "Scripts" / "python.exe"
    assert paths.extension_source == project / "st-extension"
    assert paths.personal_data_dir == project / "aetherstate-data"
    assert paths.state_path == project / ".aetherstate-play-stack.json"


def test_legacy_workspace_layout_still_resolves_personal_checkout(tmp_path: Path) -> None:
    paths = _bench(tmp_path)

    assert paths.project_dir == (paths.release_root / "AetherState-personal").resolve()
    assert paths.personal_dir == paths.project_dir
    assert paths.nli_dir == (paths.release_root / "nli-shim").resolve()
    assert paths.sillytavern_dir == (paths.release_root / "SillyTavern").resolve()


def test_explicit_component_roots_override_automatic_layout(tmp_path: Path) -> None:
    release = tmp_path / "workspace"
    project = tmp_path / "custom-project"
    nli = tmp_path / "custom-nli"
    sillytavern = tmp_path / "custom-sillytavern"

    paths = StackPaths.for_release_root(
        release,
        project_root=project,
        nli_root=nli,
        sillytavern_root=sillytavern,
        node_executable=tmp_path / "node.exe",
        environment={},
    )

    assert paths.project_dir == project.resolve()
    assert paths.nli_dir == nli.resolve()
    assert paths.sillytavern_dir == sillytavern.resolve()


def test_default_release_root_is_the_public_checkout_not_its_parent() -> None:
    assert _default_release_root() == Path(playstack.__file__).resolve().parents[2]


def test_stop_instruction_keeps_isolated_cleanup_separate_from_personal_play(
        tmp_path: Path) -> None:
    personal = _bench(tmp_path)
    live_test = _bench(tmp_path, isolated=True)

    personal_instruction = _stop_instruction(personal)
    assert personal_instruction.startswith("Ready. Stop with:\n")
    assert "aetherstate.playstack stop" in personal_instruction
    assert f'--project-root "{personal.project_dir}"' in personal_instruction
    assert f'--nli-root "{personal.nli_dir}"' in personal_instruction
    assert f'--sillytavern-root "{personal.sillytavern_dir}"' in personal_instruction
    assert "--cleanup-isolated" not in personal_instruction
    live_instruction = _stop_instruction(live_test)
    assert 'aetherstate.playstack stop' in live_instruction
    assert f'--release-root "{live_test.release_root}"' in live_instruction
    assert f'--isolated-root "{live_test.isolated_root}"' in live_instruction
    assert live_instruction.endswith("--cleanup-isolated")


@pytest.mark.parametrize(
    ("isolated", "expected_ports"),
    [(False, (8199, 9130, 8000)), (True, (18199, 19130, 18000))],
)
def test_service_specs_launch_every_component_on_the_selected_ports(
        tmp_path: Path, isolated: bool, expected_ports: tuple[int, int, int]) -> None:
    paths = _bench(tmp_path, isolated=isolated)
    specs = {spec.name: spec for spec in StackController(paths)._specs()}
    nli_port, proxy_port, sillytavern_port = expected_ports
    nli_server = str((paths.nli_dir / "server.py").resolve())
    st_server = str((paths.sillytavern_dir / "server.js").resolve())

    assert tuple(specs[name].port for name in ("nli", "proxy", "sillytavern")) \
        == expected_ports
    assert specs["nli"].args == (nli_server,)
    expected_nli_environment = {"NLI_BACKEND": "factcg"}
    expected_proxy_args = ("-m", "aetherstate")
    expected_sillytavern_args = (
        st_server,
        "--dataRoot",
        str(paths.sillytavern_data_dir),
    )
    if isolated:
        expected_nli_environment["NLI_PORT"] = str(nli_port)
        expected_proxy_args += (
            "--config",
            str(paths.personal_data_dir / "config.toml"),
            "--config-read-only",
            "--port",
            str(proxy_port),
            "--cors-origin",
            f"http://localhost:{sillytavern_port}",
            "--cors-origin",
            f"http://127.0.0.1:{sillytavern_port}",
            "--assist-endpoint-url",
            f"nli-local=http://127.0.0.1:{nli_port}/v1",
        )
        expected_sillytavern_args += ("--port", str(sillytavern_port))
    assert specs["nli"].environment == expected_nli_environment
    assert specs["proxy"].args == expected_proxy_args
    assert specs["sillytavern"].args == expected_sillytavern_args


def test_owned_process_tee_keeps_console_output_and_bounded_log(
        tmp_path: Path, monkeypatch, capfd) -> None:
    log_path = tmp_path / "owned.log"
    monkeypatch.setattr(process_tee, "_set_console_title", lambda _title: None)

    result = process_tee.run(
        [
            sys.executable,
            "-c",
            "import sys; print('normal output'); print('error output', file=sys.stderr)",
        ],
        log_path=log_path,
        title="Synthetic owned service",
    )

    captured = capfd.readouterr()
    assert result == 0
    assert "normal output" in captured.out
    assert "error output" in captured.out
    logged = log_path.read_text(encoding="utf-8")
    assert "normal output" in logged
    assert "error output" in logged


def test_owned_process_tee_records_nonzero_exit_and_keeps_recent_large_output(
        tmp_path: Path, monkeypatch, capfd) -> None:
    log_path = tmp_path / "owned.log"
    monkeypatch.setattr(process_tee, "_set_console_title", lambda _title: None)
    monkeypatch.setattr(process_tee, "_MAX_LOG_BYTES", 128)

    result = process_tee.run(
        [
            sys.executable,
            "-c",
            "import sys; print('x' * 512); print('fatal tail', file=sys.stderr); raise SystemExit(7)",
        ],
        log_path=log_path,
        title="Synthetic owned service",
        service="synthetic",
    )

    captured = capfd.readouterr()
    logged = log_path.read_text(encoding="utf-8")
    assert result == 7
    assert "fatal tail" in captured.out
    assert "synthetic exited with code 7" in captured.out
    assert "synthetic exited with code 7" in logged
    assert "x" in logged
    assert log_path.stat().st_size <= 128


def test_owned_process_wrapper_start_failure_is_written_to_its_log(
        tmp_path: Path, monkeypatch, capfd) -> None:
    log_path = tmp_path / "owned.log"

    def fail_run(*_args, **_kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(process_tee, "run", fail_run)

    result = process_tee.main([
        "--log",
        str(log_path),
        "--title",
        "Synthetic owned service",
        "--service",
        "synthetic",
        "--",
        "missing.exe",
    ])

    captured = capfd.readouterr()
    assert result == 1
    assert "synthetic failed to start: OSError" in captured.out
    assert "synthetic failed to start: OSError" in log_path.read_text(encoding="utf-8")


def test_sillytavern_log_tail_starts_after_latest_sent_request_boundary(
        tmp_path: Path) -> None:
    log_path = tmp_path / "sillytavern.log"
    log_path.write_text(
        "messages: [private outbound payload]\n"
        "}\n"
        "Streaming request in progress\n"
        "Streaming request finished\n"
        "ForbiddenError: Invalid CSRF token.\n",
        encoding="utf-8",
    )

    assert read_process_log_tail(
        log_path,
        max_lines=40,
        after_last_request=True,
    ) == [
        "Streaming request in progress",
        "Streaming request finished",
        "ForbiddenError: Invalid CSRF token.",
    ]


def test_console_text_escapes_unicode_that_cp1252_cannot_print() -> None:
    class LegacyConsole:
        encoding = "cp1252"

    assert _console_text("post-request \ufffd evidence", LegacyConsole()) \
        == r"post-request \ufffd evidence"


def test_log_view_refuses_a_state_path_outside_the_owned_log_directory(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)
    private_path = _touch(tmp_path / "private.txt", "must not be read")
    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    state["processes"][0]["log_path"] = str(private_path)
    paths.state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(StackError, match="unsafe nli log path"):
        controller.log_tails()


def test_isolated_root_must_be_new_empty_and_outside_normal_data(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    normal = [paths.personal_data_dir, paths.sillytavern_dir / "data"]

    with pytest.raises(StackError, match="unsafe isolated root"):
        validate_isolated_root(
            paths.personal_data_dir / "nested",
            normal,
            paths.release_root,
        )

    occupied = paths.release_root / "Local-Only" / "occupied"
    _touch(occupied / "private.json", "do not inspect")
    with pytest.raises(StackError, match="not empty"):
        validate_isolated_root(occupied, normal, paths.release_root)

    fresh = paths.release_root / "Local-Only" / "fresh"
    assert validate_isolated_root(fresh, normal, paths.release_root) == fresh.resolve()


def test_extension_sync_replaces_stale_copy_and_proves_hash_parity(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    destination = paths.extension_destination
    _touch(destination / "stale.js", "old")
    _touch(destination / "index.js", "old build")

    hashes = sync_extension(paths.extension_source, destination)

    assert set(hashes) == {"index.js", "manifest.json", "style.css"}
    assert not (destination / "stale.js").exists()
    for name in hashes:
        assert (destination / name).read_bytes() == (paths.extension_source / name).read_bytes()


def test_start_refuses_foreign_listener_without_killing_it(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    foreign = ProcessInfo(999, "python unrelated_server.py", "20260712120000.000000-000")
    system.processes[999] = _FakeProcess(foreign, "foreign", 9130)
    system.port_pids[9130] = {999}

    with pytest.raises(StackError, match=r"port 9130.*PID 999.*not owned"):
        _controller(paths, system).start(open_browser=False)

    assert system.terminated == []
    assert system.spawned == []
    assert 999 in system.processes


def test_live_test_start_checks_only_its_dedicated_ports(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    personal = ProcessInfo(998, "python personal_proxy.py", "20260712120000.000000-000")
    foreign = ProcessInfo(999, "python unrelated_server.py", "20260712120001.000000-000")
    system.processes[998] = _FakeProcess(personal, "personal", 9130)
    system.processes[999] = _FakeProcess(foreign, "foreign", 19130)
    system.port_pids[9130] = {998}
    system.port_pids[19130] = {999}

    with pytest.raises(StackError, match=r"port 19130.*PID 999.*not owned"):
        _controller(paths, system).start(open_browser=False)

    assert system.terminated == []
    assert system.spawned == []
    assert set(system.processes) == {998, 999}


def test_start_refuses_symlinked_owned_process_log_target(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    paths.process_log_dir.mkdir(parents=True)
    outside = _touch(tmp_path / "outside.log", "private")
    try:
        (paths.process_log_dir / "nli.log").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable for this Windows test account")
    system = FakeSystem()
    _ready(system, paths)

    with pytest.raises(StackError, match="unsafe NLI helper log path"):
        _controller(paths, system).start(open_browser=False)

    assert system.spawned == []


def test_clean_start_tracks_identity_and_reaches_all_readiness_gates(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    _ready(system, paths)

    result = _controller(paths, system).start(open_browser=False)

    assert result.ready is True
    assert result.services == {"nli": "ready", "proxy": "ready", "sillytavern": "ready"}
    assert result.route_verified is True
    assert system.spawned == ["nli", "proxy", "sillytavern"]
    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in state["processes"]] == system.spawned
    assert all(item["creation_time"] for item in state["processes"])
    assert all(item["launch_signature"] == "aetherstate.process_tee"
               for item in state["processes"])
    assert {Path(item["log_path"]).name for item in state["processes"]} == {
        "aetherstate.log",
        "nli.log",
        "sillytavern.log",
    }
    assert all(
        Path(item["log_path"]).parent == paths.process_log_dir
        for item in state["processes"]
    )
    assert "key" not in paths.state_path.read_text(encoding="utf-8").lower()
    assert paths.extension_destination.is_dir()


def test_start_confines_parent_credentials_and_transport_configuration(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    _ready(system, paths)
    parent_environment = {
        "SystemRoot": r"C:\Windows",
        "PATH": r"C:\Windows\System32",
        "AETHERSTATE_UPSTREAM__API_KEY": "placeholder",
        "AETHERSTATE_UPSTREAM__MODEL": "fixture-narrator-model",
        "AETHERSTATE_CREATOR__TIMEOUT_S": "600",
        "HTTPS_PROXY": "http://proxy.invalid",
        "OPENAI_API_KEY": "placeholder",
        "VENICE_INFERENCE_KEY": "placeholder",
        "GITHUB_TOKEN": "placeholder",
        "AWS_SECRET_ACCESS_KEY": "placeholder",
    }

    _controller(
        paths,
        system,
        parent_environment=parent_environment,
    ).start(open_browser=False)

    proxy_env = system.environments["proxy"]
    assert proxy_env["AETHERSTATE_UPSTREAM__API_KEY"] == "placeholder"
    assert proxy_env["AETHERSTATE_UPSTREAM__MODEL"] == "fixture-narrator-model"
    assert proxy_env["AETHERSTATE_CREATOR__TIMEOUT_S"] == "600"
    assert proxy_env["HTTPS_PROXY"] == "http://proxy.invalid"
    for blocked in (
        "OPENAI_API_KEY",
        "VENICE_INFERENCE_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert blocked not in proxy_env

    for service in ("nli", "sillytavern"):
        child_env = system.environments[service]
        assert child_env["SystemRoot"] == r"C:\Windows"
        assert child_env["PATH"] == r"C:\Windows\System32"
        assert child_env["PYTHONUNBUFFERED"] == "1"
        assert "HTTPS_PROXY" not in child_env
        assert not any(name.startswith("AETHERSTATE_") for name in child_env)
        for blocked in (
            "OPENAI_API_KEY",
            "VENICE_INFERENCE_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
        ):
            assert blocked not in child_env

    assert system.environments["nli"]["NLI_BACKEND"] == "factcg"
    assert system.environments["nli"]["NLI_PORT"] == str(paths.nli_port)


def test_start_fails_fast_when_owned_service_exits_before_readiness(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)

    class ExitedNliSystem(FakeSystem):
        def spawn(self, spec, env) -> ProcessInfo:
            info = super().spawn(spec, env)
            if spec.name == "nli":
                self.processes.pop(info.pid)
                self.port_pids.get(spec.port, set()).discard(info.pid)
            return info

    system = ExitedNliSystem()

    with pytest.raises(
        StackError,
        match="NLI helper exited before it became ready",
    ):
        _controller(paths, system).start(open_browser=False)

    assert system.time == 0.0
    assert system.spawned == ["nli"]
    assert not paths.state_path.exists()


def test_isolated_proxy_installs_narrator_cards_only_in_disposable_st_root(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    proxy = next(spec for spec in StackController(paths)._specs() if spec.name == "proxy")

    assert proxy.environment == {
        "AETHERSTATE_SERVER__DATA_DIR": str(paths.aetherstate_data_dir),
        "AETHERSTATE_SERVER__TURN_TRACE": "true",
        "AETHERSTATE_SPECIALIZATION__NARRATOR_CARD_DIR": str(
            paths.sillytavern_data_dir / "default-user" / "characters"
        ),
    }
    assert proxy.args[:5] == (
        "-m",
        "aetherstate",
        "--config",
        str(paths.personal_data_dir / "config.toml"),
        "--config-read-only",
    )


def test_normal_personal_play_enables_retained_turn_diagnostics(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    proxy = next(spec for spec in StackController(paths)._specs() if spec.name == "proxy")

    assert paths.aetherstate_data_dir == paths.personal_data_dir
    assert paths.process_log_dir == (
        paths.personal_data_dir / "diagnostics" / "owned-processes"
    )
    assert proxy.environment == {
        "AETHERSTATE_SERVER__DATA_DIR": str(paths.personal_data_dir),
        "AETHERSTATE_SERVER__TURN_TRACE": "true",
    }


def test_owned_service_logs_match_the_three_visible_console_streams(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    specs = {spec.name: spec for spec in StackController(paths)._specs()}

    assert specs["proxy"].console_title == str(paths.personal_python)
    assert specs["nli"].console_title == str(paths.nli_python)
    assert specs["sillytavern"].console_title == "SillyTavern WebServer"
    assert {spec.log_path for spec in specs.values()} == {
        paths.process_log_dir / "aetherstate.log",
        paths.process_log_dir / "nli.log",
        paths.process_log_dir / "sillytavern.log",
    }


def test_isolated_profile_seeds_safe_rp_generation_settings(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)

    result = seed_isolated_sillytavern_profile(paths)

    settings_path = paths.sillytavern_data_dir / "default-user" / "settings.json"
    preset_path = (
        paths.sillytavern_data_dir
        / "default-user"
        / "OpenAI Settings"
        / "AetherState RP.json"
    )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    assert result == {"context": 32768, "response": 8192, "preset": "AetherState RP"}
    assert settings["amount_gen"] == 8192
    assert settings["max_context"] == 32768
    assert settings["main_api"] == "openai"
    assert settings["oai_settings"] | {
        "preset_settings_openai": "AetherState RP",
        "temp_openai": 0.9,
        "top_p_openai": 0.95,
        "top_k_openai": 40,
        "min_p_openai": 0.05,
        "openai_max_context": 32768,
        "openai_max_tokens": 8192,
        "max_context_unlocked": True,
        "stream_openai": True,
        "chat_completion_source": "custom",
        "custom_url": "http://127.0.0.1:19130/v1",
        "custom_model": "fixture-narrator-model",
    } == settings["oai_settings"]
    assert settings["power_user"]["auto_connect"] is True
    assert settings["power_user"]["reasoning"] | {
        "auto_parse": False,
        "add_to_prompts": False,
        "auto_expand": True,
        "show_hidden": True,
        "prefix": "<think>",
        "suffix": "</think>",
        "separator": "\n",
        "max_additions": 1,
    } == settings["power_user"]["reasoning"]
    assert settings["extension_settings"]["aetherstate"] == {
        "enabled": True,
        "proxy_url": "http://127.0.0.1:19130",
        "hud": {"open": True, "compact": False},
    }
    assert preset | {
        "temperature": 0.9,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.05,
        "openai_max_context": 32768,
        "openai_max_tokens": 8192,
        "max_context_unlocked": True,
        "stream_openai": True,
    } == preset
    for prompt_settings in (settings["oai_settings"], preset):
        main = next(row for row in prompt_settings["prompts"]
                    if row.get("identifier") == "main")
        assert main["content"].startswith("[AETHERSTATE NARRATOR CONTRACT aether-narrator/2]")
        assert "fictional chat between" not in main["content"]
        assert prompt_settings["use_sysprompt"] is False
        assert prompt_settings["new_chat_prompt"] == ""
        assert prompt_settings["new_example_chat_prompt"] == ""
    assert not (paths.sillytavern_dir / "data").exists()


def test_isolated_profile_uses_explicit_model_without_reading_provider_secrets(
        tmp_path: Path) -> None:
    paths = _public_bench(tmp_path, isolated=True)
    paths = StackPaths.for_release_root(
        paths.release_root,
        node_executable=paths.node_executable,
        isolated_root=paths.isolated_root,
        narrator_model="explicit-narrator-model",
        environment={},
    )

    seed_isolated_sillytavern_profile(paths)

    settings = json.loads(
        (paths.sillytavern_data_dir / "default-user" / "settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings["oai_settings"]["custom_model"] == "explicit-narrator-model"


def test_isolated_profile_fails_closed_without_a_configured_model(tmp_path: Path) -> None:
    paths = _public_bench(tmp_path, isolated=True)
    (paths.personal_data_dir / "config.toml").write_text(
        '[upstream]\ncredential_ref = "opaque-test-ref"\n',
        encoding="utf-8",
    )

    with pytest.raises(StackError, match="configured main model"):
        seed_isolated_sillytavern_profile(paths)


def test_python_launch_trampoline_tracks_the_actual_listener_identity(tmp_path: Path) -> None:
    paths = _bench(tmp_path)

    class TrampolineSystem(FakeSystem):
        def spawn(self, spec, env) -> ProcessInfo:
            parent = super().spawn(spec, env)
            if spec.name in {"nli", "proxy"}:
                self.port_pids[spec.port].discard(parent.pid)
                child_pid = self.next_pid
                self.next_pid += 1
                child = ProcessInfo(
                    pid=child_pid,
                    command_line=f'"base-python.exe" {spec.signature}',
                    creation_time=f"2026071212{child_pid:04d}.000000-000",
                )
                self.processes[child_pid] = _FakeProcess(child, spec.name, spec.port)
                self.port_pids[spec.port].add(child_pid)
            return parent

    system = TrampolineSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)

    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in state["processes"]}
    assert by_name["nli"]["listener_pid"] != by_name["nli"]["pid"]
    assert by_name["proxy"]["listener_pid"] != by_name["proxy"]["pid"]
    assert by_name["sillytavern"]["listener_pid"] == by_name["sillytavern"]["pid"]
    assert controller.status().ready is True


def test_tee_wrappers_track_and_stop_distinct_listeners_for_all_three_services(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path)

    class WrappedListenerSystem(FakeSystem):
        def spawn(self, spec, env) -> ProcessInfo:
            parent = super().spawn(spec, env)
            self.port_pids[spec.port].discard(parent.pid)
            child_pid = self.next_pid
            self.next_pid += 1
            child = ProcessInfo(
                pid=child_pid,
                command_line=f'"{spec.executable}" {spec.signature}',
                creation_time=f"2026071212{child_pid:04d}.000000-000",
            )
            self.processes[child_pid] = _FakeProcess(child, spec.name, spec.port)
            self.port_pids[spec.port].add(child_pid)
            return parent

    system = WrappedListenerSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)

    state = json.loads(paths.state_path.read_text(encoding="utf-8"))
    assert all(item["listener_pid"] != item["pid"] for item in state["processes"])
    owned = set(system.processes)

    result = controller.stop()

    assert result.ready is False
    assert set(system.terminated) == owned
    assert system.listeners() == {}


def test_partial_start_failure_reports_component_and_stops_owned_children(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    system.responses["http://127.0.0.1:8199/"] = [
        HttpResult(200, {"status": "ok", "backend": "factcg"})
    ]
    system.responses["http://127.0.0.1:9130/aether/status"] = [HttpResult(503, None)]

    with pytest.raises(StackError, match=r"AetherState proxy.*HTTP 503"):
        _controller(paths, system).start(open_browser=False)

    assert system.spawned == ["nli", "proxy"]
    assert set(system.terminated) == {100, 101}
    assert system.listeners() == {}
    assert not paths.state_path.exists()


def test_spawn_failure_is_named_and_cleans_up_already_started_service(tmp_path: Path) -> None:
    paths = _bench(tmp_path)

    class SpawnFailSystem(FakeSystem):
        def spawn(self, spec, env) -> ProcessInfo:
            if spec.name == "proxy":
                raise OSError("synthetic spawn failure")
            return super().spawn(spec, env)

    system = SpawnFailSystem()
    system.responses["http://127.0.0.1:8199/"] = [
        HttpResult(200, {"status": "ok", "backend": "factcg"})
    ]

    with pytest.raises(StackError, match=r"AetherState proxy.*could not start.*OSError"):
        _controller(paths, system).start(open_browser=False)

    assert system.spawned == ["nli"]
    assert system.terminated == [100]
    assert system.listeners() == {}
    assert not paths.state_path.exists()


def test_second_start_replaces_only_the_recorded_owned_stack(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)
    first_pids = set(system.processes)

    _ready(system, paths)
    controller.start(open_browser=False)

    assert first_pids == set(system.terminated)
    assert set(system.processes).isdisjoint(first_pids)
    assert system.spawned == ["nli", "proxy", "sillytavern"] * 2


def test_second_isolated_start_requires_exact_cleanup_of_the_previous_root(
        tmp_path: Path) -> None:
    first = _bench(tmp_path, isolated=True)
    second = StackPaths.for_release_root(
        first.release_root,
        node_executable=first.node_executable,
        isolated_root=first.release_root / "Local-Only" / "playtest-roots" / "case-002",
    )
    system = FakeSystem()
    _ready(system, first)
    _controller(first, system).start(open_browser=False)
    assert first.isolated_root is not None and first.isolated_root.exists()

    _ready(system, second)
    with pytest.raises(StackError, match="does not match the recorded isolated root"):
        _controller(second, system).start(open_browser=False)

    assert first.isolated_root.exists()
    assert system.terminated == []

    _controller(first, system).stop(cleanup_isolated=True)
    _ready(system, second)
    _controller(second, system).start(open_browser=False)

    assert not first.isolated_root.exists()
    assert second.isolated_root is not None and second.isolated_root.exists()


def _write_empty_isolated_state(paths: StackPaths, recorded_root: object) -> None:
    paths.state_path.write_text(
        json.dumps({
            "schema": 1,
            "source_version": "1.21.0",
            "isolated_root": recorded_root,
            "processes": [],
        }),
        encoding="utf-8",
    )


def test_isolated_cleanup_accepts_only_the_exact_recorded_root(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)
    assert paths.isolated_root is not None
    marker = _touch(paths.isolated_root / "synthetic-marker.txt", "owned disposable data")
    owned = set(system.processes)

    result = controller.stop(cleanup_isolated=True)

    assert result.ready is False
    assert set(system.terminated) == owned
    assert not marker.exists()
    assert not paths.isolated_root.exists()
    assert not paths.state_path.exists()


@pytest.mark.parametrize("cleanup_isolated", [False, True])
def test_isolated_stop_refuses_a_different_cli_root_before_touching_the_stack(
        tmp_path: Path, cleanup_isolated: bool) -> None:
    recorded = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    _ready(system, recorded)
    _controller(recorded, system).start(open_browser=False)
    assert recorded.isolated_root is not None
    marker = _touch(recorded.isolated_root / "synthetic-marker.txt", "keep")
    owned = set(system.processes)
    requested = StackPaths.for_release_root(
        recorded.release_root,
        node_executable=recorded.node_executable,
        isolated_root=(
            recorded.release_root / "Local-Only" / "playtest-roots" / "different"
        ),
    )

    with pytest.raises(StackError, match="does not match the recorded isolated root"):
        _controller(requested, system).stop(cleanup_isolated=cleanup_isolated)

    assert set(system.processes) == owned
    assert system.terminated == []
    assert marker.is_file()
    assert recorded.state_path.is_file()


def test_isolated_cleanup_refuses_a_stale_missing_recorded_root(tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    assert paths.isolated_root is not None
    _write_empty_isolated_state(paths, str(paths.isolated_root))

    with pytest.raises(StackError, match="does not exist"):
        _controller(paths, FakeSystem()).stop(cleanup_isolated=True)

    assert paths.state_path.is_file()


@pytest.mark.parametrize("recorded_root", [None, "", "relative-root", 7])
def test_isolated_stop_refuses_malformed_recorded_root(
        tmp_path: Path, recorded_root: object) -> None:
    paths = _bench(tmp_path, isolated=True)
    _write_empty_isolated_state(paths, recorded_root)

    with pytest.raises(StackError, match="invalid isolated root"):
        _controller(paths, FakeSystem()).stop(cleanup_isolated=True)

    assert paths.state_path.is_file()


@pytest.mark.parametrize(
    "protected_name",
    (
        "release-root",
        "local-only-root",
        "personal-checkout",
        "personal-data",
        "sillytavern-checkout",
        "sillytavern-data",
        "outside",
    ),
)
def test_isolated_cleanup_refuses_outside_and_protected_roots(
        tmp_path: Path, protected_name: str) -> None:
    normal = _bench(tmp_path)
    candidates = {
        "release-root": normal.release_root,
        "local-only-root": normal.release_root / "Local-Only",
        "personal-checkout": normal.personal_dir,
        "personal-data": normal.personal_data_dir,
        "sillytavern-checkout": normal.sillytavern_dir,
        "sillytavern-data": normal.sillytavern_dir / "data",
        "outside": tmp_path / "outside-disposable-looking-root",
    }
    candidate = candidates[protected_name]
    candidate.mkdir(parents=True, exist_ok=True)
    sentinel = _touch(candidate / "must-survive.txt", "protected")
    paths = StackPaths.for_release_root(
        normal.release_root,
        node_executable=normal.node_executable,
        isolated_root=candidate,
    )
    _write_empty_isolated_state(paths, str(paths.isolated_root))

    with pytest.raises(StackError, match="unsafe isolated root"):
        _controller(paths, FakeSystem()).stop(cleanup_isolated=True)

    assert sentinel.is_file()
    assert paths.state_path.is_file()


def test_isolated_cleanup_refuses_a_symlink_even_when_its_target_is_safe(
        tmp_path: Path, monkeypatch) -> None:
    normal = _bench(tmp_path)
    target = normal.release_root / "Local-Only" / "playtest-roots" / "target"
    target.mkdir(parents=True)
    sentinel = _touch(target / "must-survive.txt", "protected")
    link = normal.release_root / "Local-Only" / "playtest-roots" / "linked-root"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        link.mkdir()
        sentinel = _touch(link / "must-survive.txt", "protected")
        real_is_link_like = playstack._is_link_like
        monkeypatch.setattr(
            playstack,
            "_is_link_like",
            lambda path: path == link or real_is_link_like(path),
        )
    paths = StackPaths.for_release_root(
        normal.release_root,
        node_executable=normal.node_executable,
        isolated_root=link,
    )
    _write_empty_isolated_state(paths, str(paths.isolated_root))

    with pytest.raises(StackError, match="symlink"):
        _controller(paths, FakeSystem()).stop(cleanup_isolated=True)

    assert sentinel.is_file()
    assert link.is_symlink() or link.is_dir()
    assert paths.state_path.is_file()


def test_isolated_cleanup_refuses_a_windows_junction_on_python_310_contract(
        tmp_path: Path, monkeypatch) -> None:
    paths = _bench(tmp_path, isolated=True)
    assert paths.isolated_root is not None
    paths.isolated_root.mkdir(parents=True)
    sentinel = _touch(paths.isolated_root / "must-survive.txt", "protected")
    _write_empty_isolated_state(paths, str(paths.isolated_root))
    real_lstat = Path.lstat

    def junction_lstat(path: Path):
        if path == paths.isolated_root:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=0x0400,
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", junction_lstat)

    with pytest.raises(StackError, match="symlinked isolated root"):
        _controller(paths, FakeSystem()).stop(cleanup_isolated=True)

    assert sentinel.is_file()
    assert paths.state_path.is_file()


def test_personal_and_live_test_stacks_start_and_stop_independently(tmp_path: Path) -> None:
    personal = _bench(tmp_path)
    live_test = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    personal_controller = _controller(personal, system)
    live_test_controller = _controller(live_test, system)

    _ready(system, personal)
    personal_controller.start(open_browser=False)
    personal_pids = set(system.processes)

    _ready(system, live_test)
    live_test_controller.start(open_browser=False)
    live_test_pids = set(system.processes) - personal_pids

    assert system.terminated == []
    assert set(system.listeners()) == {
        8199, 9130, 8000, 18199, 19130, 18000,
    }
    assert personal.state_path.is_file()
    assert live_test.state_path.is_file()
    assert personal_controller.status().ready is True
    assert live_test_controller.status().ready is True

    live_test_controller.stop()

    assert set(system.terminated) == live_test_pids
    assert personal_pids == set(system.processes)
    assert personal_controller.status().ready is True
    assert personal.state_path.is_file()
    assert not live_test.state_path.exists()

    personal_controller.stop()

    assert set(system.terminated) == personal_pids | live_test_pids
    assert system.listeners() == {}


def test_stop_releases_owned_ports_and_does_not_touch_unrecorded_process(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    _ready(system, paths)
    controller = _controller(paths, system)
    controller.start(open_browser=False)
    owned = set(system.processes)
    foreign = ProcessInfo(999, "node other.js", "20260712120000.000000-000")
    system.processes[999] = _FakeProcess(foreign, "foreign", 7777)
    system.port_pids[7777] = {999}

    result = controller.stop()

    assert result.ready is False
    assert set(system.terminated) == owned
    assert 999 in system.processes
    assert system.listeners() == {7777: {999}}
    assert not paths.state_path.exists()


def test_start_opens_pages_only_after_route_is_verified(tmp_path: Path) -> None:
    paths = _bench(tmp_path)
    system = FakeSystem()
    _ready(system, paths)

    _controller(paths, system).start(open_browser=True)

    assert system.opened == [
        "http://127.0.0.1:9130/aether/console",
        "http://127.0.0.1:8000/",
    ]


def test_live_test_start_opens_only_dedicated_pages_after_route_verification(
        tmp_path: Path) -> None:
    paths = _bench(tmp_path, isolated=True)
    system = FakeSystem()
    _ready(system, paths)

    _controller(paths, system).start(open_browser=True)

    assert system.opened == [
        "http://127.0.0.1:19130/aether/console",
        "http://127.0.0.1:18000/",
    ]
