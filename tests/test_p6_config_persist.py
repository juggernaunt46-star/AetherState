"""Regression: Console config save must merge, not clobber (control._persist_config).

Guards the two bugs reported at 1.0:
  1. dashboard save dropped [server].host/port -> restart fell back to 127.0.0.1:9130;
  2. upstream api_key written to config.toml in plaintext, world-readable.
"""
from __future__ import annotations

import hashlib
import os
import stat

import httpx
import pytest
from fastapi import FastAPI

from aetherstate.config import Config, load_config
from aetherstate.control import _persist_config, _toml_dumps, make_control_router
from aetherstate.store import Store


def _write(p, text):
    p.write_text(text, encoding="utf-8")


def test_console_save_preserves_host_port_and_unmanaged_sections(tmp_path):
    p = tmp_path / "config.toml"
    _write(p, (
        '[server]\nhost = "0.0.0.0"\nport = 9999\ndata_dir = "%s"\n'
        '[upstream]\nbase_url = "http://old"\napi_key = "sk-PLAINTEXT"\n'
        '[injection]\nmax_tokens = 1500\n'
        '[director]\nminutes_per_turn = 7\n'
    ) % str(tmp_path).replace("\\", "/"))

    cfg = load_config(p)
    assert (cfg.server.host, cfg.server.port) == ("0.0.0.0", 9999)

    cfg.upstream.base_url = "http://new"          # simulate a Console connection edit
    assert _persist_config(cfg) is True

    r = load_config(p)
    assert (r.server.host, r.server.port) == ("0.0.0.0", 9999)     # bug 1: host/port survive
    assert r.injection.max_tokens == 1500                          # unmanaged section survives
    assert r.director.minutes_per_turn == 7
    assert r.upstream.base_url == "http://new"                     # managed edit persisted
    assert r.upstream.api_key == "sk-PLAINTEXT"


def test_first_save_with_no_file_writes_host_port(tmp_path):
    c = Config()
    c.server.data_dir = str(tmp_path)
    c.server.host, c.server.port = "1.2.3.4", 8080
    assert _persist_config(c) is True
    r = load_config(tmp_path / "config.toml")
    assert (r.server.host, r.server.port) == ("1.2.3.4", 8080)


def test_console_specialization_knobs_survive_restart(tmp_path):
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    cfg.specialization.name = "rpg"
    cfg.specialization.combat_opening_primer = False
    cfg.specialization.enemy_rolls = False
    cfg.specialization.contract = "compact"

    assert _persist_config(cfg) is True
    reloaded = load_config(tmp_path / "config.toml")

    assert reloaded.specialization.name == "rpg"
    assert reloaded.specialization.combat_opening_primer is False
    assert reloaded.specialization.enemy_rolls is False
    assert reloaded.specialization.contract == "compact"


@pytest.mark.skipif(os.name != "posix", reason="0600 is a POSIX perm; no-op on NTFS")
def test_saved_config_is_not_world_readable(tmp_path):
    c = Config()
    c.server.data_dir = str(tmp_path)
    c.upstream.api_key = "sk-secret"
    assert _persist_config(c) is True
    mode = stat.S_IMODE(os.stat(tmp_path / "config.toml").st_mode)
    assert mode == 0o600, oct(mode)


def test_env_supplied_key_is_never_written_to_disk(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    _write(p, '[upstream]\nbase_url = "http://x"\n')
    monkeypatch.setenv("AETHERSTATE_UPSTREAM__API_KEY", "sk-FROM-ENV")
    cfg = load_config(p)                           # env override lands in cfg.upstream.api_key
    cfg.server.data_dir = str(tmp_path)
    assert cfg.upstream.api_key == "sk-FROM-ENV"
    assert _persist_config(cfg) is True
    assert "sk-FROM-ENV" not in p.read_text(encoding="utf-8")


def test_toml_emitter_roundtrips_nested_shapes():
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    data = {
        "server": {"host": "h", "port": 1, "cors_origins": ["a", "b"]},
        "assist": {
            "endpoints": [{"name": "n", "base_url": "u", "max_concurrent": 2}],
            "groups": {"extraction": "main", "embeddings": "off"},
        },
        "extraction": {"debounce_s": 20.0, "thinking": "auto"},
        "consent": {"safewords": []},
        "manual_override": {"enabled": False},
    }
    parsed = tomllib.loads(_toml_dumps(data))
    assert parsed == data


def test_persist_writes_back_to_loaded_config_path(tmp_path):
    """--config path may differ from data_dir/config.toml; save must hit the loaded file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conf = tmp_path / "custom" / "myconf.toml"
    conf.parent.mkdir()
    conf.write_text(
        '[server]\nhost = "0.0.0.0"\nport = 7000\ndata_dir = "%s"\n'
        '[upstream]\nbase_url = "http://a"\n' % str(data_dir).replace("\\", "/"),
        encoding="utf-8")

    cfg = load_config(conf)
    assert cfg.source_path == str(conf)

    cfg.upstream.base_url = "http://b"
    assert _persist_config(cfg) is True

    assert not (data_dir / "config.toml").exists()      # did NOT write to data_dir fallback
    r = load_config(conf)                               # wrote back to the loaded --config path
    assert r.upstream.base_url == "http://b"
    assert (r.server.host, r.server.port) == ("0.0.0.0", 7000)


def _snapshot(path):
    info = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
    )


async def test_read_only_console_post_changes_runtime_without_persisting_personal_config(
        tmp_path):
    personal = tmp_path / "personal"
    personal.mkdir()
    source = personal / "config.toml"
    backup = personal / "config.toml.bak"
    source.write_text(
        '[upstream]\nbase_url = "http://personal"\napi_key = "test-secret"\n'
        '[specialization]\nname = "rpg"\nwar_room = true\n',
        encoding="utf-8",
    )
    backup.write_text('[upstream]\nbase_url = "http://prior"\n', encoding="utf-8")
    before = {path.name: _snapshot(path) for path in (source, backup)}
    isolated_data = tmp_path / "isolated" / "aetherstate"

    cfg = load_config(source, read_only=True)
    cfg.server.data_dir = str(isolated_data)
    app = FastAPI()
    app.include_router(make_control_router(cfg, Store(":memory:")))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://isolated-proxy",
    ) as client:
        response = await client.post(
            "/aether/specialization",
            json={"war_room": False},
        )

    assert response.status_code == 200
    assert response.json()["persisted"] is False
    assert cfg.specialization.war_room is False
    assert cfg.source_path == ""
    assert cfg.persistence_enabled is False
    assert {path.name: _snapshot(path) for path in (source, backup)} == before
    assert not (isolated_data / "config.toml").exists()
    assert not list(tmp_path.glob("isolated/**/*.toml"))
    assert [
        path for path in tmp_path.rglob("*")
        if path.is_file() and b"test-secret" in path.read_bytes()
    ] == [source]
