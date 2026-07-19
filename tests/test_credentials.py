from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

import aetherstate.secret_store as secret_store
from aetherstate.app import create_app
from aetherstate.config import Config, load_config
from aetherstate.control import _persist_config, make_control_router
from aetherstate.secret_store import (
    CredentialStore,
    CredentialStoreUnavailable,
    migrate_legacy_credentials,
)
from aetherstate.store import Store


class SecureMemoryBackend:
    aetherstate_secure = True
    priority = 1

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class FailingSecureBackend(SecureMemoryBackend):
    def set_password(self, service, account, value):
        raise RuntimeError("synthetic backend detail must stay private")


def _stored_values(backend: SecureMemoryBackend) -> set[str]:
    return set(backend.values.values())


def test_os_vault_uses_opaque_reference_and_roundtrips_secret():
    backend = SecureMemoryBackend()
    store = CredentialStore(backend)

    reference = store.put("synthetic-provider-key")

    assert reference.startswith("cred_")
    assert "synthetic-provider-key" not in reference
    assert store.get(reference) == "synthetic-provider-key"
    store.delete(reference)
    assert not backend.values


def test_unknown_or_file_like_backend_is_refused():
    class UnknownBackend:
        priority = 99

    store = CredentialStore(UnknownBackend())

    try:
        store.put("synthetic-provider-key")
    except CredentialStoreUnavailable as exc:
        assert str(exc) == "secure credential storage is unavailable"
    else:
        raise AssertionError("an unapproved credential backend must fail closed")


def test_legacy_keys_migrate_out_of_source_and_backup(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text(
        '[server]\ndata_dir = "%s"\n'
        '[upstream]\nbase_url = "https://provider.invalid/v1"\napi_key = "legacy-main"\n'
        '[[assist.endpoints]]\nname = "helper"\nbase_url = "https://helper.invalid/v1"\n'
        'api_key = "legacy-helper"\n'
        % str(tmp_path).replace("\\", "/"),
        encoding="utf-8",
    )
    cfg = load_config(source)
    assert "legacy-main" in (tmp_path / "config.toml.bak").read_text(encoding="utf-8")
    backend = SecureMemoryBackend()

    result = migrate_legacy_credentials(
        cfg, _persist_config, store=CredentialStore(backend)
    )

    assert result.migrated == 2
    assert result.failed is False
    assert cfg.upstream.api_key == ""
    assert cfg.assist.endpoints[0].api_key == ""
    assert cfg.upstream.credential_ref.startswith("cred_")
    assert cfg.assist.endpoints[0].credential_ref.startswith("cred_")
    assert _stored_values(backend) == {"legacy-main", "legacy-helper"}
    for path in (source, tmp_path / "config.toml.bak"):
        serialized = path.read_text(encoding="utf-8")
        assert "legacy-main" not in serialized
        assert "legacy-helper" not in serialized
        assert "api_key" not in serialized
        assert "credential_ref" in serialized


def test_failed_migration_retains_the_only_plaintext_copy(tmp_path):
    source = tmp_path / "config.toml"
    source.write_text(
        '[server]\ndata_dir = "%s"\n[upstream]\napi_key = "legacy-main"\n'
        % str(tmp_path).replace("\\", "/"),
        encoding="utf-8",
    )
    cfg = load_config(source)
    before_source = source.read_bytes()
    before_backup = (tmp_path / "config.toml.bak").read_bytes()

    result = migrate_legacy_credentials(
        cfg, _persist_config, store=CredentialStore(FailingSecureBackend())
    )

    assert result.failed is True
    assert cfg.upstream.api_key == "legacy-main"
    assert cfg.upstream.credential_ref == ""
    assert source.read_bytes() == before_source
    assert (tmp_path / "config.toml.bak").read_bytes() == before_backup


def test_app_fails_closed_when_legacy_plaintext_cannot_be_secured(tmp_path, monkeypatch):
    source = tmp_path / "config.toml"
    source.write_text(
        '[server]\ndata_dir = "%s"\n[upstream]\napi_key = "legacy-main"\n'
        % str(tmp_path).replace("\\", "/"),
        encoding="utf-8",
    )
    cfg = load_config(source)
    monkeypatch.setattr(
        secret_store,
        "_default_store",
        CredentialStore(FailingSecureBackend()),
    )

    with pytest.raises(CredentialStoreUnavailable, match="could not be secured"):
        create_app(cfg, store=Store(":memory:"))

    assert cfg.upstream.api_key == "legacy-main"
    assert "legacy-main" in source.read_text(encoding="utf-8")


async def test_console_save_keeps_key_only_in_secure_store(tmp_path):
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    backend = SecureMemoryBackend()
    app = FastAPI()
    app.include_router(
        make_control_router(
            cfg,
            Store(":memory:"),
            credential_store=CredentialStore(backend),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-aetherstate"
    ) as client:
        response = await client.post(
            "/aether/connection",
            json={
                "target": "upstream",
                "base_url": "https://provider.invalid/v1",
                "model": "synthetic-model",
                "api_key": "console-only-secret",
            },
        )
        view = await client.get("/aether/connection")

    assert response.status_code == 200
    assert response.json()["upstream"]["has_key"] is True
    assert "console-only-secret" not in response.text
    assert "console-only-secret" not in view.text
    assert _stored_values(backend) == {"console-only-secret"}
    serialized = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "console-only-secret" not in serialized
    assert "api_key" not in serialized
    assert cfg.upstream.api_key == ""
    assert cfg.upstream.credential_ref in serialized


async def test_console_save_fails_closed_without_secure_backend(tmp_path):
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    cfg.upstream.base_url = "https://original.invalid/v1"
    cfg.upstream.model = "original-model"
    app = FastAPI()
    app.include_router(
        make_control_router(
            cfg,
            Store(":memory:"),
            credential_store=CredentialStore(object()),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-aetherstate"
    ) as client:
        response = await client.post(
            "/aether/connection",
            json={
                "target": "upstream",
                "base_url": "https://replacement.invalid/v1",
                "model": "replacement-model",
                "api_key": "must-not-persist",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"error": "secure credential storage is unavailable"}
    assert cfg.upstream.api_key == ""
    assert cfg.upstream.credential_ref == ""
    assert cfg.upstream.base_url == "https://original.invalid/v1"
    assert cfg.upstream.model == "original-model"
    assert not list(tmp_path.rglob("*.toml"))


async def test_console_save_rolls_back_runtime_and_vault_when_persistence_fails(tmp_path):
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    cfg.persistence_enabled = False
    cfg.upstream.base_url = "https://original.invalid/v1"
    backend = SecureMemoryBackend()
    app = FastAPI()
    app.include_router(
        make_control_router(
            cfg,
            Store(":memory:"),
            credential_store=CredentialStore(backend),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local-aetherstate"
    ) as client:
        response = await client.post(
            "/aether/connection",
            json={
                "target": "upstream",
                "base_url": "https://replacement.invalid/v1",
                "api_key": "must-be-rolled-back",
            },
        )

    assert response.status_code == 500
    assert response.json() == {"error": "connection settings could not be saved"}
    assert cfg.upstream.base_url == "https://original.invalid/v1"
    assert cfg.upstream.api_key == ""
    assert cfg.upstream.credential_ref == ""
    assert not backend.values
