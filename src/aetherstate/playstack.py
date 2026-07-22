"""Owned Windows play-bench launcher for the local AetherState development stack.

This is deliberately a small project-specific controller, not a general process supervisor. It
starts the existing NLI helper, AetherState proxy, and SillyTavern bench; proves their identities;
and records enough process identity to stop only what it launched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 dependency
    import tomli as tomllib

from . import __version__
from .prompts import NARRATOR_ENVELOPE

_PERSONAL_PORTS = (8199, 9130, 8000)
_LIVE_TEST_PORTS = (18199, 19130, 18000)
_EXTENSION_FILES = ("manifest.json", "index.js", "style.css")
_STATE_SCHEMA = 1
_RP_CONTEXT_TOKENS = 32768
_RP_RESPONSE_TOKENS = 8192
_RP_PRESET_NAME = "AetherState RP"
_LOG_TAIL_BYTES = 256 * 1024
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_SAFE_CHILD_ENVIRONMENT = frozenset({
    "APPDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMPUTERNAME",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SESSIONNAME",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TZ",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
    "WT_PROFILE_ID",
    "WT_SESSION",
})
_PROXY_TRANSPORT_ENVIRONMENT = frozenset({
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
})


class StackError(RuntimeError):
    """An actionable play-stack failure safe to show in the launcher window."""


def _resolve_from(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _environment_value(environment: Mapping[str, str], name: str) -> str:
    wanted = name.upper()
    for key, value in environment.items():
        if key.upper() == wanted:
            return str(value)
    return ""


def _looks_like_project(path: Path) -> bool:
    package = path / "src" / "aetherstate"
    return package.is_dir() and (path / "st-extension").is_dir()


def _resolve_project_dir(release: Path, project_root: str | Path | None) -> Path:
    if project_root is not None:
        return _resolve_from(release, project_root)
    for candidate in (release, release / "AetherState-personal"):
        if _looks_like_project(candidate):
            return candidate.resolve()
    return release.resolve()


def _first_component_root(candidates: list[Path], marker: str) -> Path:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
        if (resolved / marker).is_file():
            return resolved
    return unique[0]


def _resolve_nli_dir(
    release: Path,
    project: Path,
    nli_root: str | Path | None,
) -> Path:
    if nli_root is not None:
        return _resolve_from(release, nli_root)
    return _first_component_root(
        [project / "nli-shim", release / "nli-shim", project.parent / "nli-shim"],
        "server.py",
    )


def _resolve_sillytavern_dir(
    release: Path,
    project: Path,
    sillytavern_root: str | Path | None,
    environment: Mapping[str, str],
) -> Path:
    if sillytavern_root is not None:
        return _resolve_from(release, sillytavern_root)
    candidates: list[Path] = []
    configured = _environment_value(environment, "SILLYTAVERN_DIR").strip()
    if configured:
        candidates.append(_resolve_from(release, configured))
    candidates.extend((
        project / "SillyTavern",
        release / "SillyTavern",
        project.parent / "SillyTavern",
    ))
    user_profile = _environment_value(environment, "USERPROFILE").strip()
    if user_profile:
        home = Path(user_profile)
        candidates.extend((home / "SillyTavern", home / "Documents" / "SillyTavern"))
    local_app_data = _environment_value(environment, "LOCALAPPDATA").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "SillyTavern")
    return _first_component_root(candidates, "server.js")


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    command_line: str
    creation_time: str


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    display_name: str
    port: int
    executable: Path
    args: tuple[str, ...]
    cwd: Path
    signature: str
    log_path: Path
    console_title: str
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StackResult:
    ready: bool
    services: dict[str, str]
    route_verified: bool = False


@dataclass(frozen=True)
class StackPaths:
    release_root: Path
    project_dir: Path
    nli_dir: Path
    sillytavern_dir: Path
    personal_python: Path
    nli_python: Path
    node_executable: Path
    state_path: Path
    nli_port: int
    proxy_port: int
    sillytavern_port: int
    extension_source: Path
    extension_destination: Path
    personal_data_dir: Path
    aetherstate_data_dir: Path
    sillytavern_data_dir: Path
    process_log_dir: Path
    narrator_model: str | None
    isolated_root: Path | None = None
    isolated_root_input: Path | None = None

    @property
    def ports(self) -> tuple[int, int, int]:
        return (self.nli_port, self.proxy_port, self.sillytavern_port)

    @property
    def personal_dir(self) -> Path:
        """Compatibility alias for the former private-workspace field name."""
        return self.project_dir

    @classmethod
    def for_release_root(
        cls,
        release_root: str | Path,
        *,
        project_root: str | Path | None = None,
        nli_root: str | Path | None = None,
        sillytavern_root: str | Path | None = None,
        node_executable: str | Path | None = None,
        isolated_root: str | Path | None = None,
        narrator_model: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> StackPaths:
        release = Path(release_root).resolve()
        parent_environment = os.environ if environment is None else environment
        project = _resolve_project_dir(release, project_root)
        nli = _resolve_nli_dir(release, project, nli_root)
        st = _resolve_sillytavern_dir(
            release,
            project,
            sillytavern_root,
            parent_environment,
        )
        isolated_input = Path(isolated_root) if isolated_root is not None else None
        isolated = isolated_input.resolve() if isolated_input is not None else None
        as_data = isolated / "aetherstate" if isolated else project / "aetherstate-data"
        st_data = isolated / "sillytavern" if isolated else st / "data"
        log_dir = (
            isolated / "diagnostics" / "owned-processes"
            if isolated
            else as_data / "diagnostics" / "owned-processes"
        )
        nli_port, proxy_port, sillytavern_port = (
            _LIVE_TEST_PORTS if isolated else _PERSONAL_PORTS
        )
        node = _resolve_from(release, node_executable) if node_executable else _find_node()
        selected_model = str(narrator_model or "").strip()
        if not selected_model:
            selected_model = _environment_value(
                parent_environment,
                "AETHERSTATE_UPSTREAM__MODEL",
            ).strip()
        return cls(
            release_root=release,
            project_dir=project,
            nli_dir=nli,
            sillytavern_dir=st,
            personal_python=project / ".venv" / "Scripts" / "python.exe",
            nli_python=nli / ".venv" / "Scripts" / "python.exe",
            node_executable=node,
            state_path=release / (
                ".aetherstate-live-test-stack.json"
                if isolated
                else ".aetherstate-play-stack.json"
            ),
            nli_port=nli_port,
            proxy_port=proxy_port,
            sillytavern_port=sillytavern_port,
            extension_source=project / "st-extension",
            extension_destination=st_data / "default-user" / "extensions" / "AetherState",
            personal_data_dir=project / "aetherstate-data",
            aetherstate_data_dir=as_data,
            sillytavern_data_dir=st_data,
            process_log_dir=log_dir,
            narrator_model=selected_model or None,
            isolated_root=isolated,
            isolated_root_input=isolated_input,
        )


class System(Protocol):
    def listeners(self) -> dict[int, set[int]]: ...

    def process_info(self, pid: int) -> ProcessInfo | None: ...

    def spawn(self, spec: ServiceSpec, env: dict[str, str]) -> ProcessInfo: ...

    def terminate(self, pid: int) -> bool: ...

    def request(self, url: str, timeout_s: float = 2.0) -> HttpResult: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def open_url(self, url: str) -> None: ...


def _find_node() -> Path:
    found = shutil.which("node")
    return Path(found).resolve() if found else Path("node.exe")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_like(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if (
        stat.S_ISLNK(metadata.st_mode)
        or int(getattr(metadata, "st_file_attributes", 0)) & _WINDOWS_REPARSE_POINT
    ):
        return True
    try:
        is_junction = getattr(path, "is_junction", None)
        if is_junction and is_junction():
            return True
    except OSError:
        return True
    return False


def _validate_isolated_location(
    root: str | Path,
    normal_data_roots: list[Path],
    release_root: str | Path,
    *,
    lexical_root: str | Path | None = None,
    require_existing: bool = False,
) -> Path:
    """Bind a disposable root to one real, non-linked Local-Only descendant."""
    release = Path(release_root).resolve()
    allowed_parent = release / "Local-Only"
    lexical = Path(lexical_root) if lexical_root is not None else Path(root)
    if not lexical.is_absolute() or ".." in lexical.parts:
        raise StackError(f"Play-stack state has an invalid isolated root: {lexical}")
    try:
        relative = lexical.relative_to(allowed_parent)
    except ValueError as exc:
        raise StackError(
            f"Refusing unsafe isolated root outside {allowed_parent}: {lexical}"
        ) from exc
    if not relative.parts:
        raise StackError(f"Refusing unsafe isolated root: {allowed_parent}")

    probe = allowed_parent
    for part in relative.parts:
        if _is_link_like(probe):
            raise StackError(f"Refusing symlinked isolated root: {lexical}")
        probe /= part
    if _is_link_like(probe):
        raise StackError(f"Refusing symlinked isolated root: {lexical}")

    resolved = Path(root).resolve()
    allowed_resolved = allowed_parent.resolve()
    if resolved == allowed_resolved or not _is_within(resolved, allowed_resolved):
        raise StackError(
            f"Refusing unsafe isolated root outside {allowed_parent}: {lexical}"
        )
    for normal in normal_data_roots:
        normal_resolved = normal.resolve()
        if (
            resolved == normal_resolved
            or _is_within(resolved, normal_resolved)
            or _is_within(normal_resolved, resolved)
        ):
            raise StackError(f"Refusing unsafe isolated root near protected data: {resolved}")
    if require_existing:
        if not lexical.exists():
            raise StackError(f"Recorded isolated root does not exist: {lexical}")
        if not lexical.is_dir():
            raise StackError(f"Recorded isolated root is not a directory: {lexical}")
    return resolved


def validate_isolated_root(
    root: str | Path,
    normal_data_roots: list[Path],
    release_root: str | Path,
    *,
    lexical_root: str | Path | None = None,
) -> Path:
    """Require an absent/newly-empty root in the release's Local-Only tree."""
    resolved = _validate_isolated_location(
        root,
        normal_data_roots,
        release_root,
        lexical_root=lexical_root,
    )
    if resolved.exists():
        if not resolved.is_dir():
            raise StackError(f"Disposable root is not a directory: {resolved}")
        if any(resolved.iterdir()):
            raise StackError(f"Disposable root exists and is not empty: {resolved}")
    return resolved


def validate_resumable_isolated_root(
    root: str | Path,
    normal_data_roots: list[Path],
    release_root: str | Path,
    *,
    lexical_root: str | Path | None = None,
) -> Path:
    """Require an existing disposable root with the launcher-owned data layout."""

    resolved = _validate_isolated_location(
        root,
        normal_data_roots,
        release_root,
        lexical_root=lexical_root,
        require_existing=True,
    )
    required = (resolved / "aetherstate", resolved / "sillytavern")
    missing = [path.name for path in required if not path.is_dir()]
    if missing:
        raise StackError(
            "Cannot resume disposable root without launcher-owned data directories: "
            + ", ".join(missing)
        )
    return resolved


def _safe_remove_isolated(
    root: Path,
    normal_data_roots: list[Path],
    release_root: Path,
    *,
    lexical_root: Path | None = None,
) -> None:
    resolved = _validate_isolated_location(
        root,
        normal_data_roots,
        release_root,
        lexical_root=lexical_root,
        require_existing=True,
    )
    shutil.rmtree(resolved)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_extension(source: str | Path, destination: str | Path) -> dict[str, str]:
    """Replace the installed Companion copy and return verified source/destination hashes."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.name.casefold() != "aetherstate":
        raise StackError(f"Refusing unsafe extension destination: {destination_path}")
    for name in _EXTENSION_FILES:
        if not (source_path / name).is_file():
            raise StackError(f"AetherState extension source is missing {name}")
    if destination_path.is_symlink():
        raise StackError(f"Refusing symlinked extension destination: {destination_path}")
    if destination_path.exists():
        shutil.rmtree(destination_path)
    destination_path.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for name in _EXTENSION_FILES:
        src = source_path / name
        dst = destination_path / name
        shutil.copy2(src, dst)
        source_hash = _sha256(src)
        if _sha256(dst) != source_hash:
            raise StackError(f"Installed extension failed byte verification: {name}")
        hashes[name] = source_hash
    return hashes


_NETSTAT_LISTENER = re.compile(
    r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$",
    re.IGNORECASE,
)


def parse_netstat(output: str) -> dict[int, set[int]]:
    listeners: dict[int, set[int]] = {}
    for line in output.splitlines():
        match = _NETSTAT_LISTENER.match(line)
        if match:
            port, pid = int(match.group(1)), int(match.group(2))
            listeners.setdefault(port, set()).add(pid)
    return listeners


def _put_environment(environment: dict[str, str], name: str, value: str) -> None:
    wanted = name.upper()
    for existing in tuple(environment):
        if existing.upper() == wanted:
            environment.pop(existing)
    environment[name] = value


def _service_environment(
    parent: Mapping[str, str],
    spec: ServiceSpec,
) -> dict[str, str]:
    """Build the smallest child environment and confine transport config to the proxy."""
    environment: dict[str, str] = {}
    for name, value in parent.items():
        canonical = name.upper()
        if canonical in _SAFE_CHILD_ENVIRONMENT:
            _put_environment(environment, name, str(value))
        elif spec.name == "proxy" and (
            canonical.startswith("AETHERSTATE_")
            or canonical in _PROXY_TRANSPORT_ENVIRONMENT
        ):
            _put_environment(environment, name, str(value))
    _put_environment(environment, "PYTHONUNBUFFERED", "1")
    for name, value in spec.environment.items():
        _put_environment(environment, name, value)
    return environment


class WindowsSystem:
    def listeners(self) -> dict[int, set[int]]:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise StackError("Could not inspect Windows listening ports with netstat")
        return parse_netstat(proc.stdout)

    def process_info(self, pid: int) -> ProcessInfo | None:
        script = (
            f'$p=Get-CimInstance Win32_Process -Filter "ProcessId = {int(pid)}"; '
            "if($null -ne $p){[pscustomobject]@{"
            "pid=[int]$p.ProcessId;command_line=[string]$p.CommandLine;"
            "creation_time=$p.CreationDate.ToUniversalTime().ToString('o')}"
            "|ConvertTo-Json -Compress}"
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            return None
        try:
            data = json.loads(raw)
            return ProcessInfo(
                pid=int(data["pid"]),
                command_line=str(data["command_line"] or ""),
                creation_time=str(data["creation_time"] or ""),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def spawn(self, spec: ServiceSpec, env: dict[str, str]) -> ProcessInfo:
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        command = [
            sys.executable,
            "-m",
            "aetherstate.process_tee",
            "--log",
            str(spec.log_path),
            "--title",
            spec.console_title,
            "--service",
            spec.name,
            "--",
            str(spec.executable),
            *spec.args,
        ]
        proc = subprocess.Popen(
            command,
            cwd=spec.cwd,
            env=env,
            creationflags=creation_flags,
            close_fds=False,
        )
        for _ in range(20):
            info = self.process_info(proc.pid)
            if info is not None:
                return info
            time.sleep(0.05)
        proc.terminate()
        raise StackError(f"Could not verify the launched {spec.display_name} process identity")

    def terminate(self, pid: int) -> bool:
        proc = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        return proc.returncode == 0

    def request(self, url: str, timeout_s: float = 2.0) -> HttpResult:
        request = urllib.request.Request(url, headers={"User-Agent": "AetherState-PlayStack/1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = int(exc.code)
        payload: dict[str, Any] | None = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return HttpResult(status=status, payload=payload)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def open_url(self, url: str) -> None:
        webbrowser.open(url)


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackError(f"Cannot seed the isolated profile: invalid {label}") from exc
    if not isinstance(data, dict):
        raise StackError(f"Cannot seed the isolated profile: {label} is not an object")
    return data


def _configured_narrator_model(paths: StackPaths) -> str:
    if paths.narrator_model:
        return paths.narrator_model
    config_path = paths.personal_data_dir / "config.toml"
    selected = ""
    in_upstream = False
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("["):
                    in_upstream = bool(
                        re.fullmatch(r"\[\s*upstream\s*\](?:\s*#.*)?", stripped)
                    )
                    continue
                if not in_upstream or not re.match(r"^\s*model\s*=", line):
                    continue
                parsed = tomllib.loads(f"[upstream]\n{line}")
                upstream = parsed.get("upstream")
                model = upstream.get("model") if isinstance(upstream, dict) else None
                selected = str(model or "").strip()
                break
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise StackError("Cannot seed the isolated profile: invalid AetherState config") from exc
    if not selected:
        raise StackError(
            "Cannot seed the isolated profile without a configured main model; "
            "set it in Console Connection or pass --model"
        )
    return selected


def _install_narrator_envelope(settings: dict[str, Any]) -> None:
    """Make SillyTavern's stable prompt head agree with the typed narrator role."""
    prompts = settings.get("prompts")
    prompts = prompts if isinstance(prompts, list) else []
    main = next((row for row in prompts
                 if isinstance(row, dict) and row.get("identifier") == "main"), None)
    if main is None:
        main = {"identifier": "main", "name": "AetherState Narrator Contract",
                "role": "system", "system_prompt": True}
        prompts.insert(0, main)
    main.update({"content": NARRATOR_ENVELOPE, "role": "system", "system_prompt": True})
    settings["prompts"] = prompts
    settings["use_sysprompt"] = False
    # Generic ST separators are model-facing meta chatter, not story or authority.
    settings["new_chat_prompt"] = ""
    settings["new_example_chat_prompt"] = ""


def seed_isolated_sillytavern_profile(paths: StackPaths) -> dict[str, Any]:
    """Seed safe RP settings only inside a disposable SillyTavern data root."""
    if paths.isolated_root is None:
        raise StackError("Refusing to seed playtest settings outside an isolated root")
    settings_source = paths.sillytavern_dir / "default" / "content" / "settings.json"
    preset_source = (
        paths.sillytavern_dir
        / "default"
        / "content"
        / "presets"
        / "openai"
        / "Default.json"
    )
    settings = _read_json_object(settings_source, "SillyTavern default settings")
    preset = _read_json_object(preset_source, "SillyTavern OpenAI preset")
    narrator_model = _configured_narrator_model(paths)

    settings["main_api"] = "openai"
    settings["amount_gen"] = _RP_RESPONSE_TOKENS
    settings["max_context"] = _RP_CONTEXT_TOKENS
    oai = settings.setdefault("oai_settings", {})
    if not isinstance(oai, dict):
        raise StackError("Cannot seed the isolated profile: oai_settings is not an object")
    oai.update({
        "preset_settings_openai": _RP_PRESET_NAME,
        "temp_openai": 0.9,
        "top_p_openai": 0.95,
        "top_k_openai": 40,
        "min_p_openai": 0.05,
        "openai_max_context": _RP_CONTEXT_TOKENS,
        "openai_max_tokens": _RP_RESPONSE_TOKENS,
        "max_context_unlocked": True,
        "stream_openai": True,
        "chat_completion_source": "custom",
        "custom_url": f"http://127.0.0.1:{paths.proxy_port}/v1",
        "custom_model": narrator_model,
    })
    _install_narrator_envelope(oai)
    power_user = settings.setdefault("power_user", {})
    if not isinstance(power_user, dict):
        raise StackError("Cannot seed the isolated profile: power_user is not an object")
    power_user["auto_connect"] = True
    reasoning = power_user.setdefault("reasoning", {})
    if not isinstance(reasoning, dict):
        raise StackError("Cannot seed the isolated profile: reasoning is not an object")
    reasoning.update({
        "auto_parse": False,
        "add_to_prompts": False,
        "auto_expand": True,
        "show_hidden": True,
        "prefix": "<think>",
        "suffix": "</think>",
        "separator": "\n",
        "max_additions": 1,
    })
    extensions = settings.setdefault("extension_settings", {})
    if not isinstance(extensions, dict):
        raise StackError("Cannot seed the isolated profile: extension_settings is not an object")
    extensions["aetherstate"] = {
        "enabled": True,
        "proxy_url": f"http://127.0.0.1:{paths.proxy_port}",
        "hud": {"open": True, "compact": False},
    }

    preset.update({
        "temperature": 0.9,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.05,
        "openai_max_context": _RP_CONTEXT_TOKENS,
        "openai_max_tokens": _RP_RESPONSE_TOKENS,
        "max_context_unlocked": True,
        "stream_openai": True,
    })
    _install_narrator_envelope(preset)
    user_root = paths.sillytavern_data_dir / "default-user"
    _atomic_json(user_root / "settings.json", settings)
    _atomic_json(user_root / "OpenAI Settings" / f"{_RP_PRESET_NAME}.json", preset)
    return {
        "context": _RP_CONTEXT_TOKENS,
        "response": _RP_RESPONSE_TOKENS,
        "preset": _RP_PRESET_NAME,
    }


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackError(f"Play-stack state is unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("schema") != _STATE_SCHEMA:
        raise StackError("Play-stack state has an unsupported schema")
    if not isinstance(data.get("processes"), list):
        raise StackError("Play-stack state has no process list")
    return data


def read_process_log_tail(
    path: str | Path,
    *,
    max_lines: int = 40,
    after_last_request: bool = False,
) -> list[str]:
    """Read a bounded diagnostic tail without loading SillyTavern's sent request dump."""
    if max_lines < 1:
        raise ValueError("max_lines must be positive")
    log_path = Path(path)
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            offset = max(0, size - _LOG_TAIL_BYTES)
            handle.seek(offset)
            raw = handle.read()
    except OSError:
        return []
    if offset:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1:] if boundary >= 0 else b""
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if after_last_request:
        for index in range(len(lines) - 1, -1, -1):
            if lines[index].strip() == "}":
                lines = lines[index + 1:]
                break
    return lines[-max_lines:]


class StackController:
    def __init__(
        self,
        paths: StackPaths,
        *,
        system: System | None = None,
        poll_interval_s: float = 0.5,
        readiness_timeouts: dict[str, float] | None = None,
        parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.system = system or WindowsSystem()
        self.parent_environment = os.environ if parent_environment is None else parent_environment
        self.poll_interval_s = poll_interval_s
        self.timeouts = {
            "nli": 300.0,
            "proxy": 45.0,
            "sillytavern": 90.0,
            "route": 45.0,
            **(readiness_timeouts or {}),
        }

    def _preflight(self, *, resume_isolated: bool = False) -> None:
        required = {
            "AetherState Python environment": self.paths.personal_python,
            "NLI Python environment": self.paths.nli_python,
            "NLI server": self.paths.nli_dir / "server.py",
            "SillyTavern server": self.paths.sillytavern_dir / "server.js",
            "Node.js": self.paths.node_executable,
        }
        for label, path in required.items():
            if not path.is_file():
                raise StackError(f"Missing {label}: {path}")
        if self.paths.isolated_root is not None:
            validator = (
                validate_resumable_isolated_root
                if resume_isolated
                else validate_isolated_root
            )
            validator(
                self.paths.isolated_root,
                [self.paths.personal_data_dir, self.paths.sillytavern_dir / "data"],
                self.paths.release_root,
                lexical_root=self.paths.isolated_root_input,
            )

    def _backend(self) -> str:
        selected = self.paths.nli_dir / "selected-backend.txt"
        if not selected.is_file():
            return "factcg"
        backend = selected.read_text(encoding="utf-8").strip().lower()
        return backend or "factcg"

    def _specs(self) -> tuple[ServiceSpec, ...]:
        backend = self._backend()
        nli_server = (self.paths.nli_dir / "server.py").resolve()
        st_server = (self.paths.sillytavern_dir / "server.js").resolve()
        # Personal play is the evidence-producing path: keep a bounded diagnostic history in
        # that run's AetherState data directory without requiring a config edit.
        proxy_environment: dict[str, str] = {
            "AETHERSTATE_SERVER__DATA_DIR": str(self.paths.aetherstate_data_dir),
            "AETHERSTATE_SERVER__TURN_TRACE": "true",
        }
        if self.paths.isolated_root is not None:
            proxy_environment["AETHERSTATE_SPECIALIZATION__NARRATOR_CARD_DIR"] = str(
                self.paths.sillytavern_data_dir / "default-user" / "characters"
            )
        nli_environment = {"NLI_BACKEND": backend}
        proxy_args = ("-m", "aetherstate")
        sillytavern_args = (
            str(st_server),
            "--dataRoot",
            str(self.paths.sillytavern_data_dir),
        )
        if self.paths.isolated_root is not None:
            nli_environment["NLI_PORT"] = str(self.paths.nli_port)
            proxy_args += (
                "--config",
                str(self.paths.personal_data_dir / "config.toml"),
                "--config-read-only",
                "--port",
                str(self.paths.proxy_port),
                "--cors-origin",
                f"http://localhost:{self.paths.sillytavern_port}",
                "--cors-origin",
                f"http://127.0.0.1:{self.paths.sillytavern_port}",
                "--assist-endpoint-url",
                f"nli-local=http://127.0.0.1:{self.paths.nli_port}/v1",
            )
            sillytavern_args += ("--port", str(self.paths.sillytavern_port))
        return (
            ServiceSpec(
                name="nli",
                display_name="NLI helper",
                port=self.paths.nli_port,
                executable=self.paths.nli_python,
                args=(str(nli_server),),
                cwd=self.paths.nli_dir,
                signature=str(nli_server),
                log_path=self.paths.process_log_dir / "nli.log",
                console_title=str(self.paths.nli_python),
                environment=nli_environment,
            ),
            ServiceSpec(
                name="proxy",
                display_name="AetherState proxy",
                port=self.paths.proxy_port,
                executable=self.paths.personal_python,
                args=proxy_args,
                cwd=self.paths.personal_dir,
                signature="-m aetherstate",
                log_path=self.paths.process_log_dir / "aetherstate.log",
                console_title=str(self.paths.personal_python),
                environment=proxy_environment,
            ),
            ServiceSpec(
                name="sillytavern",
                display_name="SillyTavern",
                port=self.paths.sillytavern_port,
                executable=self.paths.node_executable,
                args=sillytavern_args,
                cwd=self.paths.sillytavern_dir,
                signature=str(st_server),
                log_path=self.paths.process_log_dir / "sillytavern.log",
                console_title="SillyTavern WebServer",
            ),
        )

    def _prepare_process_logs(self, specs: tuple[ServiceSpec, ...]) -> None:
        log_dir = self.paths.process_log_dir
        if log_dir.is_symlink():
            raise StackError(f"Refusing symlinked play-stack log directory: {log_dir}")
        log_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = log_dir.resolve()
        for spec in specs:
            if spec.log_path.parent.resolve() != resolved_dir or spec.log_path.is_symlink():
                raise StackError(f"Refusing unsafe {spec.display_name} log path")

    def _new_state(self) -> dict[str, Any]:
        return {
            "schema": _STATE_SCHEMA,
            "source_version": __version__,
            "isolated_root": str(self.paths.isolated_root) if self.paths.isolated_root else None,
            "processes": [],
        }

    def _record(self, state: dict[str, Any], spec: ServiceSpec, info: ProcessInfo) -> None:
        state["processes"].append(
            {
                "name": spec.name,
                "display_name": spec.display_name,
                "port": spec.port,
                "pid": info.pid,
                "signature": spec.signature,
                "launch_signature": "aetherstate.process_tee",
                "creation_time": info.creation_time,
                "log_path": str(spec.log_path),
            }
        )
        _atomic_json(self.paths.state_path, state)

    @staticmethod
    def _identity_matches(record: dict[str, Any], info: ProcessInfo) -> bool:
        signature = str(
            record.get("launch_signature") or record.get("signature") or ""
        ).casefold()
        created = str(record.get("creation_time") or "")
        return (
            bool(signature)
            and signature in info.command_line.casefold()
            and bool(created)
            and created == info.creation_time
        )

    @staticmethod
    def _listener_identity_matches(record: dict[str, Any], info: ProcessInfo) -> bool:
        signature = str(record.get("signature") or "").casefold()
        created = str(
            record.get("listener_creation_time") or record.get("creation_time") or ""
        )
        return (
            bool(signature)
            and signature in info.command_line.casefold()
            and bool(created)
            and created == info.creation_time
        )

    def _record_listener(self, state: dict[str, Any], spec: ServiceSpec) -> None:
        pids = sorted(self.system.listeners().get(spec.port, set()))
        if len(pids) != 1:
            detail = "none" if not pids else ", ".join(str(pid) for pid in pids)
            raise StackError(
                f"{spec.display_name} readiness passed but port {spec.port} had "
                f"unexpected listener owners: {detail}"
            )
        info = self.system.process_info(pids[0])
        if info is None or spec.signature.casefold() not in info.command_line.casefold():
            raise StackError(
                f"{spec.display_name} readiness passed but its port owner identity was wrong"
            )
        record = next(item for item in state["processes"] if item["name"] == spec.name)
        record["listener_pid"] = info.pid
        record["listener_creation_time"] = info.creation_time
        _atomic_json(self.paths.state_path, state)

    def _foreign_listener_error(self, listeners: dict[int, set[int]]) -> StackError | None:
        for port in self.paths.ports:
            pids = sorted(listeners.get(port, set()))
            if pids:
                joined = ", ".join(f"PID {pid}" for pid in pids)
                return StackError(
                    f"port {port} is held by {joined} and is not owned by this play stack; "
                    "nothing was terminated"
                )
        return None

    def _wait(
        self,
        spec: ServiceSpec,
        process: ProcessInfo,
        url: str,
        timeout_key: str,
        validate,
    ) -> HttpResult:
        deadline = self.system.monotonic() + self.timeouts[timeout_key]
        last = "no response"
        while self.system.monotonic() <= deadline:
            if self.system.process_info(process.pid) is None:
                raise StackError(
                    f"{spec.display_name} exited before it became ready"
                )
            try:
                result = self.system.request(url)
                problem = validate(result)
                if problem is None:
                    return result
                last = problem
            except Exception as exc:
                last = type(exc).__name__
            self.system.sleep(self.poll_interval_s)
        raise StackError(f"{spec.display_name} did not become ready: {last}")

    def _wait_nli(self, spec: ServiceSpec, process: ProcessInfo) -> None:
        backend = self._backend()

        def validate(result: HttpResult) -> str | None:
            if result.status != 200:
                return f"HTTP {result.status}"
            payload = result.payload or {}
            if payload.get("status") != "ok":
                return "status payload was not ready"
            if str(payload.get("backend") or "").lower() != backend:
                return f"wrong backend (expected {backend})"
            return None

        self._wait(
            spec,
            process,
            f"http://127.0.0.1:{spec.port}/",
            "nli",
            validate,
        )

    def _wait_proxy(self, spec: ServiceSpec, process: ProcessInfo) -> None:
        expected_data = self.paths.aetherstate_data_dir.resolve()

        def validate(result: HttpResult) -> str | None:
            if result.status != 200:
                return f"HTTP {result.status}"
            payload = result.payload or {}
            if payload.get("name") != "aetherstate":
                return "wrong service identity"
            if payload.get("version") != __version__:
                return f"wrong build (expected {__version__})"
            raw_data = str(payload.get("data_dir") or "")
            actual_data = Path(raw_data)
            if not actual_data.is_absolute():
                actual_data = self.paths.personal_dir / actual_data
            if actual_data.resolve() != expected_data:
                return "wrong AetherState data root"
            if not payload.get("upstream_configured"):
                return "main-model upstream is not configured"
            return None

        self._wait(
            spec,
            process,
            f"http://127.0.0.1:{spec.port}/aether/status",
            "proxy",
            validate,
        )

    def _wait_sillytavern(self, spec: ServiceSpec, process: ProcessInfo) -> None:
        def validate(result: HttpResult) -> str | None:
            return None if 200 <= result.status < 400 else f"HTTP {result.status}"

        self._wait(
            spec,
            process,
            f"http://127.0.0.1:{spec.port}/",
            "sillytavern",
            validate,
        )

    def _wait_route(self, proxy_spec: ServiceSpec, process: ProcessInfo) -> None:
        def validate(result: HttpResult) -> str | None:
            if result.status != 200:
                return f"HTTP {result.status}"
            payload = result.payload or {}
            if payload.get("object") != "list" or not isinstance(payload.get("data"), list):
                return "upstream model-list payload was invalid"
            return None

        self._wait(
            proxy_spec,
            process,
            f"http://127.0.0.1:{proxy_spec.port}/v1/models",
            "route",
            validate,
        )

    def start(
        self,
        *,
        open_browser: bool = True,
        check_route: bool = True,
        resume_isolated: bool = False,
    ) -> StackResult:
        if resume_isolated and self.paths.isolated_root is None:
            raise StackError("--resume-isolated requires --isolated-root")
        self._preflight(resume_isolated=resume_isolated)
        if self.paths.state_path.exists():
            previous = _read_state(self.paths.state_path)
            self.stop(
                cleanup_isolated=bool(
                    previous and previous.get("isolated_root") and not resume_isolated
                )
            )
        listeners = self.system.listeners()
        foreign = self._foreign_listener_error(listeners)
        if foreign:
            raise foreign

        if self.paths.isolated_root is not None:
            self.paths.aetherstate_data_dir.mkdir(parents=True, exist_ok=True)
            self.paths.sillytavern_data_dir.mkdir(parents=True, exist_ok=True)
            if not resume_isolated:
                seed_isolated_sillytavern_profile(self.paths)
        sync_extension(self.paths.extension_source, self.paths.extension_destination)

        specs = self._specs()
        self._prepare_process_logs(specs)
        state = self._new_state()
        statuses: dict[str, str] = {}
        started: dict[str, ProcessInfo] = {}
        try:
            for spec in specs:
                environment = _service_environment(self.parent_environment, spec)
                try:
                    info = self.system.spawn(spec, environment)
                except Exception as exc:
                    raise StackError(
                        f"{spec.display_name} could not start: {type(exc).__name__}"
                    ) from exc
                self._record(state, spec, info)
                started[spec.name] = info
                if spec.name == "nli":
                    self._wait_nli(spec, info)
                elif spec.name == "proxy":
                    self._wait_proxy(spec, info)
                else:
                    self._wait_sillytavern(spec, info)
                self._record_listener(state, spec)
                statuses[spec.name] = "ready"
            route_verified = False
            if check_route:
                self._wait_route(specs[1], started["proxy"])
                route_verified = True
            if open_browser:
                self.system.open_url(
                    f"http://127.0.0.1:{self.paths.proxy_port}/aether/console"
                )
                self.system.open_url(
                    f"http://127.0.0.1:{self.paths.sillytavern_port}/"
                )
            return StackResult(True, statuses, route_verified)
        except StackError:
            try:
                self.stop()
            except StackError:
                pass
            raise

    def _bound_isolated_root(
        self,
        state: dict[str, Any],
        *,
        require_existing: bool,
    ) -> tuple[Path, Path]:
        requested = self.paths.isolated_root
        if requested is None:
            raise StackError("Isolated cleanup requires an explicit isolated root")
        raw = state.get("isolated_root")
        if not isinstance(raw, str) or not raw.strip():
            raise StackError("Play-stack state has an invalid isolated root")
        recorded_lexical = Path(raw)
        requested_lexical = self.paths.isolated_root_input or requested
        normal_roots = [
            self.paths.personal_data_dir,
            self.paths.sillytavern_dir / "data",
        ]
        recorded = _validate_isolated_location(
            recorded_lexical,
            normal_roots,
            self.paths.release_root,
            lexical_root=recorded_lexical,
        )
        requested_resolved = _validate_isolated_location(
            requested,
            normal_roots,
            self.paths.release_root,
            lexical_root=requested_lexical,
        )
        if recorded != requested_resolved:
            raise StackError(
                f"Requested isolated root {requested_resolved} does not match the "
                f"recorded isolated root {recorded}"
            )
        if require_existing:
            recorded = _validate_isolated_location(
                recorded,
                normal_roots,
                self.paths.release_root,
                lexical_root=recorded_lexical,
                require_existing=True,
            )
        return recorded, recorded_lexical

    def stop(self, *, cleanup_isolated: bool = False) -> StackResult:
        state = _read_state(self.paths.state_path)
        if state is None:
            return StackResult(False, {})
        cleanup_root: tuple[Path, Path] | None = None
        if self.paths.isolated_root is not None:
            cleanup_root = self._bound_isolated_root(
                state,
                require_existing=cleanup_isolated,
            )
        elif cleanup_isolated:
            raise StackError("Isolated cleanup requires an explicit isolated root")
        listeners = self.system.listeners()
        records = list(reversed(state["processes"]))
        stopped: dict[str, str] = {}
        for record in records:
            pid = int(record.get("pid", 0))
            info = self.system.process_info(pid)
            parent_stopped = False
            if info is not None and self._identity_matches(record, info):
                self.system.terminate(pid)
                parent_stopped = True
            elif info is not None:
                on_stack_port = any(
                    pid in listeners.get(port, set()) for port in self.paths.ports
                )
                if on_stack_port:
                    raise StackError(
                        f"Recorded PID {pid} no longer matches its launch identity; "
                        "refusing to terminate it"
                    )

            listener_pid = int(record.get("listener_pid", pid))
            if listener_pid != pid:
                listener_info = self.system.process_info(listener_pid)
                if listener_info is not None and self._listener_identity_matches(
                    record, listener_info
                ):
                    self.system.terminate(listener_pid)
                    parent_stopped = True
                elif listener_info is not None and listener_pid in listeners.get(
                    int(record.get("port", 0)), set()
                ):
                    raise StackError(
                        f"Recorded listener PID {listener_pid} no longer matches its launch "
                        "identity; refusing to terminate it"
                    )
            stopped[str(record.get("name"))] = "stopped" if parent_stopped else "already stopped"

        remaining = self.system.listeners()
        foreign = self._foreign_listener_error(remaining)
        if foreign:
            raise foreign
        if cleanup_isolated and cleanup_root is not None:
            isolated_root, recorded_lexical = cleanup_root
            _safe_remove_isolated(
                isolated_root,
                [self.paths.personal_data_dir, self.paths.sillytavern_dir / "data"],
                self.paths.release_root,
                lexical_root=recorded_lexical,
            )
        self.paths.state_path.unlink(missing_ok=True)
        return StackResult(False, stopped)

    def status(self) -> StackResult:
        state = _read_state(self.paths.state_path)
        if state is None:
            return StackResult(False, {})
        if self.paths.isolated_root is not None:
            self._bound_isolated_root(state, require_existing=False)
        listeners = self.system.listeners()
        statuses: dict[str, str] = {}
        all_ready = True
        for record in state["processes"]:
            pid = int(record.get("pid", 0))
            listener_pid = int(record.get("listener_pid", pid))
            info = self.system.process_info(listener_pid)
            port = int(record.get("port", 0))
            owned = info is not None and self._listener_identity_matches(record, info)
            listening = listener_pid in listeners.get(port, set())
            ready = owned and listening
            statuses[str(record.get("name"))] = "ready" if ready else "not ready"
            all_ready = all_ready and ready
        return StackResult(all_ready and len(statuses) == 3, statuses)

    def log_tails(self, *, max_lines: int = 40) -> dict[str, list[str]]:
        """Return only the recent outcome evidence for each owned live process."""
        if not 1 <= max_lines <= 200:
            raise StackError("Log line count must be between 1 and 200")
        state = _read_state(self.paths.state_path)
        if state is None:
            return {}
        tails: dict[str, list[str]] = {}
        expected_paths = {
            "proxy": self.paths.process_log_dir / "aetherstate.log",
            "nli": self.paths.process_log_dir / "nli.log",
            "sillytavern": self.paths.process_log_dir / "sillytavern.log",
        }
        for record in state["processes"]:
            name = str(record.get("name") or "unknown")
            if name not in expected_paths:
                raise StackError(f"Play-stack state contains unknown process log: {name}")
            raw_path = str(record.get("log_path") or "")
            if not raw_path:
                tails[name] = ["[not captured by this launch]"]
                continue
            log_path = Path(raw_path)
            if log_path.is_symlink() or log_path.resolve() != expected_paths[name].resolve():
                raise StackError(f"Refusing unsafe {name} log path from play-stack state")
            tails[name] = read_process_log_tail(
                log_path,
                max_lines=max_lines,
                after_last_request=name == "sillytavern",
            )
        return tails


def _default_release_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _print_result(result: StackResult) -> None:
    for name, status in result.services.items():
        print(f"  {name}: {status}")
    if result.ready:
        route = "verified" if result.route_verified else "not checked"
        print(f"  OpenAI-compatible route: {route}")


def _stop_instruction(paths: StackPaths) -> str:
    command = (
        f'"{paths.personal_python}" -m aetherstate.playstack stop '
        f'--release-root "{paths.release_root}" '
        f'--project-root "{paths.project_dir}" '
        f'--nli-root "{paths.nli_dir}" '
        f'--sillytavern-root "{paths.sillytavern_dir}"'
    )
    if paths.isolated_root is not None:
        command += f' --isolated-root "{paths.isolated_root}" --cleanup-isolated'
    return f"Ready. Stop with:\n  {command}"


def _console_text(text: str, stream=None) -> str:
    """Make diagnostic text printable by the active Windows console encoding.

    Owned logs are UTF-8 and may contain replacement characters or ordinary Unicode that a
    legacy cp1252 console cannot encode.  Escaping only those unencodable code points keeps the
    post-request evidence readable instead of letting the log command crash when it is needed.
    """
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:
        return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _print_log_tails(tails: dict[str, list[str]]) -> None:
    labels = {
        "proxy": "AetherState proxy",
        "nli": "NLI helper",
        "sillytavern": "SillyTavern WebServer",
    }
    for name in ("proxy", "nli", "sillytavern"):
        if name not in tails:
            continue
        print(_console_text(f"--- {labels[name]} ---"))
        lines = tails[name]
        text = "\n".join(lines) if lines else "[no post-request output]"
        print(_console_text(text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start and verify the AetherState play stack")
    parser.add_argument("command", choices=("start", "stop", "status", "logs"))
    parser.add_argument(
        "--release-root",
        default=str(_default_release_root()),
        help="checkout root, or the legacy workspace root that contains AetherState-personal",
    )
    parser.add_argument("--project-root", help="explicit AetherState checkout root")
    parser.add_argument("--nli-root", help="explicit nli-shim root")
    parser.add_argument("--sillytavern-root", help="explicit SillyTavern root")
    parser.add_argument("--node-executable", help="explicit Node.js executable")
    parser.add_argument(
        "--model",
        help="model name for a newly seeded isolated SillyTavern profile",
    )
    parser.add_argument("--isolated-root")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-route-check", action="store_true")
    parser.add_argument("--cleanup-isolated", action="store_true")
    parser.add_argument(
        "--resume-isolated",
        action="store_true",
        help="resume an existing validated disposable root without reseeding its data",
    )
    parser.add_argument("--lines", type=int, default=40)
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("[X] The play-stack controller currently supports Windows only.", file=sys.stderr)
        return 2
    paths = StackPaths.for_release_root(
        args.release_root,
        project_root=args.project_root,
        nli_root=args.nli_root,
        sillytavern_root=args.sillytavern_root,
        node_executable=args.node_executable,
        isolated_root=args.isolated_root,
        narrator_model=args.model,
    )
    controller = StackController(paths)
    try:
        if args.resume_isolated and args.command != "start":
            raise StackError("--resume-isolated is valid only with the start command")
        if args.command == "start":
            print("Starting and verifying the AetherState play stack...")
            result = controller.start(
                open_browser=not args.no_browser,
                check_route=not args.no_route_check,
                resume_isolated=args.resume_isolated,
            )
            _print_result(result)
            print(_stop_instruction(paths))
        elif args.command == "stop":
            result = controller.stop(cleanup_isolated=args.cleanup_isolated)
            _print_result(result)
            print("AetherState play stack stopped.")
        elif args.command == "status":
            result = controller.status()
            _print_result(result)
            return 0 if result.ready else 1
        else:
            tails = controller.log_tails(max_lines=args.lines)
            if not tails:
                print("[X] No active AetherState play stack was recorded.", file=sys.stderr)
                return 1
            _print_log_tails(tails)
    except StackError as exc:
        print(f"[X] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
