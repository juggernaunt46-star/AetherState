from __future__ import annotations

import pytest

from aetherstate.__main__ import _apply_runtime_overrides
from aetherstate.config import AssistEndpointConfig, Config


def _config() -> Config:
    cfg = Config()
    cfg.assist.endpoints = [
        AssistEndpointConfig(
            name="nli-local",
            base_url="http://127.0.0.1:8199/v1",
            model="factcg",
        )
    ]
    return cfg


def test_runtime_overrides_change_only_the_live_process_config() -> None:
    cfg = _config()

    _apply_runtime_overrides(
        cfg,
        port=19130,
        cors_origins=("http://127.0.0.1:18000", "http://localhost:18000"),
        assist_endpoint_urls=("nli-local=http://127.0.0.1:18199/v1",),
    )

    assert cfg.server.port == 19130
    assert cfg.server.cors_origins == [
        "http://127.0.0.1:18000",
        "http://localhost:18000",
    ]
    assert cfg.assist.endpoints[0].base_url == "http://127.0.0.1:18199/v1"


def test_runtime_assist_override_rejects_missing_or_malformed_endpoint() -> None:
    cfg = _config()

    with pytest.raises(ValueError, match="NAME=URL"):
        _apply_runtime_overrides(cfg, assist_endpoint_urls=("nli-local",))
    with pytest.raises(ValueError, match="configured assist endpoint not found"):
        _apply_runtime_overrides(
            cfg,
            assist_endpoint_urls=("different=http://127.0.0.1:18199/v1",),
        )
