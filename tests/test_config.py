"""09 F1: config never prevents startup; precedence env > file > defaults."""
from __future__ import annotations

import hashlib

from aetherstate.__main__ import _apply_runtime_overrides
from aetherstate.config import Config, load_config


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


def _file_snapshot(path):
    info = path.stat()
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "mode": info.st_mode,
    }


def test_read_only_load_borrows_source_without_backup_write_or_persistence_target(tmp_path):
    source = tmp_path / "config.toml"
    backup = tmp_path / "config.toml.bak"
    source.write_text(
        '[server]\nport = 9130\n[upstream]\nbase_url = "http://personal"\n'
        'api_key = "test-secret"\n',
        encoding="utf-8",
    )
    backup.write_text('[server]\nport = 7000\n', encoding="utf-8")
    before = {path.name: _file_snapshot(path) for path in (source, backup)}

    cfg = load_config(source, read_only=True)

    assert cfg.source == "file"
    assert cfg.upstream.base_url == "http://personal"
    assert cfg.upstream.api_key == "test-secret"
    assert cfg.source_path == ""
    assert cfg.persistence_enabled is False
    assert {path.name: _file_snapshot(path) for path in (source, backup)} == before


def test_read_only_load_does_not_create_a_missing_backup(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text('[server]\nport = 19130\n', encoding="utf-8")

    cfg = load_config(source, read_only=True)

    assert cfg.server.port == 19130
    assert not (tmp_path / "config.toml.bak").exists()


def test_creator_generation_defaults_are_large_and_old_configs_inherit_them(tmp_path):
    assert Config().creator.max_tokens == 32768
    assert Config().creator.timeout_s == 600.0
    assert Config().creator.validation_retries == 1

    legacy = tmp_path / "legacy.toml"
    legacy.write_text('[upstream]\nmodel = "main-model"\n', encoding="utf-8")
    loaded = load_config(legacy)
    assert loaded.creator.max_tokens == 32768
    assert loaded.creator.timeout_s == 600.0
    assert loaded.creator.validation_retries == 1


def test_creator_generation_config_loads_read_only_without_mutating_source(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text(
        '[creator]\nmax_tokens = 49152\ntimeout_s = 720\nvalidation_retries = 1\n',
        encoding="utf-8",
    )
    before = _file_snapshot(source)

    cfg = load_config(source, read_only=True)

    assert cfg.creator.max_tokens == 49152
    assert cfg.creator.timeout_s == 720.0
    assert cfg.creator.validation_retries == 1
    assert _file_snapshot(source) == before
    assert not (tmp_path / "config.toml.bak").exists()


def test_experience_profiles_use_sanitized_explicit_overrides(tmp_path):
    try:
        from aetherstate.config import finalize_experience_profile_base
        from aetherstate.experience import config_for_experience
    except (ImportError, ModuleNotFoundError) as exc:
        raise AssertionError(f"experience profile contract is missing: {exc}") from exc

    source = tmp_path / "config.toml"
    source.write_text(
        "[upstream]\n"
        'base_url = "https://provider.test"\n'
        'api_key = "must-not-survive"\n'
        'credential_ref = "vault:test"\n'
        "[injection]\n"
        "max_tokens = 1777\n"
        "[specialization]\n"
        'name = "rpg"\n',
        encoding="utf-8",
    )
    cfg = load_config(source)
    cfg.upstream.api_key = ""
    _apply_runtime_overrides(cfg, port=19131)
    finalize_experience_profile_base(cfg)

    chat_cfg = config_for_experience(cfg, "chat")
    rpg_cfg = config_for_experience(cfg, "rpg")

    for effective in (chat_cfg, rpg_cfg):
        assert effective.server.port == 19131
        assert effective.injection.max_tokens == 1777
        assert effective.upstream.base_url == "https://provider.test"
        assert effective.upstream.credential_ref == "vault:test"
        assert effective.upstream.api_key == ""
    assert chat_cfg.specialization.name == "none"
    assert rpg_cfg.specialization.name == "rpg"
    assert cfg.specialization.name == "rpg"
    assert cfg.upstream.api_key == ""
    assert "must-not-survive" not in repr(cfg._experience_profile_base)


def test_experience_profile_finalization_keeps_transient_key_only_at_transport_boundary():
    from aetherstate.config import finalize_experience_profile_base
    from aetherstate.experience import config_for_experience

    cfg = Config()
    cfg.upstream.api_key = "synthetic-runtime-key"

    finalize_experience_profile_base(cfg)

    assert cfg.upstream.api_key == "synthetic-runtime-key"
    assert config_for_experience(cfg, "chat").upstream.api_key == ""
    assert config_for_experience(cfg, "rpg").upstream.api_key == ""
    assert "synthetic-runtime-key" not in repr(cfg._experience_profile_base)


def test_programmatic_config_overrides_survive_experience_profile_finalization():
    from aetherstate.config import finalize_experience_profile_base
    from aetherstate.experience import config_for_experience

    cfg = Config()
    cfg.server.turn_trace = True
    cfg.user_guard.name = "Configured Player"
    cfg.specialization.name = "rpg"

    finalize_experience_profile_base(cfg)

    chat_cfg = config_for_experience(cfg, "chat")
    rpg_cfg = config_for_experience(cfg, "rpg")
    for effective in (chat_cfg, rpg_cfg):
        assert effective.server.turn_trace is True
        assert effective.user_guard.name == "Configured Player"
    assert chat_cfg.specialization.name == "none"
    assert rpg_cfg.specialization.name == "rpg"


def test_direct_runtime_config_change_reaches_later_experience_requests():
    from aetherstate.config import finalize_experience_profile_base
    from aetherstate.experience import config_for_experience

    cfg = Config()
    finalize_experience_profile_base(cfg)

    cfg.injection.max_tokens = 1777

    assert config_for_experience(cfg, "chat").injection.max_tokens == 1777
    assert config_for_experience(cfg, "rpg").injection.max_tokens == 1777
