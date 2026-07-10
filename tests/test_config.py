"""09 F1: config never prevents startup; precedence env > file > defaults."""
from __future__ import annotations

from aetherstate.config import load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.source == "defaults"
    assert cfg.server.port == 9130


def test_valid_file_loads_and_writes_bak(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[server]\nport = 9999\n[upstream]\nbase_url = "http://x"\n')
    cfg = load_config(p)
    assert cfg.source == "file" and cfg.server.port == 9999
    assert (tmp_path / "config.toml.bak").exists()


def test_invalid_file_falls_back_to_last_known_good(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[server]\nport = 9999\n')
    load_config(p)                       # writes .bak
    p.write_text("this is [ not toml")
    cfg = load_config(p)
    assert cfg.source == "last_known_good" and cfg.server.port == 9999


def test_invalid_file_no_bak_gives_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("garbage [[[")
    cfg = load_config(p)
    assert cfg.source == "defaults"


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHERSTATE_SERVER__PORT", "7777")
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.server.port == 7777
