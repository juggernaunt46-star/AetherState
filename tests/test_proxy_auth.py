"""Authorization fallback behavior for the transparent upstream relay."""
from __future__ import annotations

import httpx
import pytest

import aetherstate.secret_store as secret_store
from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.secret_store import CredentialStore
from aetherstate.store import Store
from tests.test_credentials import SecureMemoryBackend
from tests.mock_upstream import MockUpstream, Reply


async def _relay_authorization(
    incoming: str | None | list[tuple[str, str]],
    configured_key: str = "saved-test-key",
    credential_ref: str = "",
) -> list[str]:
    mock = MockUpstream()
    mock.enqueue(Reply())

    cfg = Config()
    cfg.upstream.base_url = "http://mock-upstream/v1"
    cfg.upstream.api_key = configured_key
    cfg.upstream.credential_ref = credential_ref

    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock),
        base_url="http://mock-upstream",
    )
    app = create_app(cfg, client_factory=lambda: upstream_client, store=Store(":memory:"))
    if incoming is None:
        headers = {}
    elif isinstance(incoming, str):
        headers = {"Authorization": incoming}
    else:
        headers = incoming

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy",
        ) as client:
            response = await client.get("/v1/models", headers=headers)
        assert response.status_code == 200
        return [
            value
            for name, value in mock.requests[0].raw_headers
            if name.lower() == "authorization"
        ]
    finally:
        await app.state.jobs.stop()
        await upstream_client.aclose()


@pytest.mark.parametrize("incoming", [None, "", "Bearer", "Bearer ", "bearer    "])
async def test_configured_key_replaces_absent_authorization(incoming):
    assert await _relay_authorization(incoming) == ["Bearer saved-test-key"]


@pytest.mark.parametrize("incoming", ["", "Bearer", "Bearer ", "bearer    "])
async def test_placeholder_authorization_is_preserved_without_configured_key(incoming):
    assert await _relay_authorization(incoming, configured_key="") == [incoming]


async def test_duplicate_placeholder_authorization_preserves_first_once_without_configured_key():
    incoming = [("Authorization", "Bearer "), ("Authorization", "")]

    assert await _relay_authorization(incoming, configured_key="") == ["Bearer "]


@pytest.mark.parametrize("incoming", ["Bearer frontend-test-key", "Basic synthetic-token"])
async def test_usable_frontend_authorization_is_preserved(incoming):
    assert await _relay_authorization(incoming, configured_key="") == [incoming]


@pytest.mark.parametrize("incoming", ["Bearer frontend-test-key", "Basic synthetic-token"])
async def test_configured_key_is_compartment_authority_over_frontend_secret(incoming):
    assert await _relay_authorization(incoming) == ["Bearer saved-test-key"]


async def test_vault_credential_is_resolved_only_for_upstream_transport(monkeypatch):
    store = CredentialStore(SecureMemoryBackend())
    reference = store.put("vault-only-test-key")
    monkeypatch.setattr(secret_store, "_default_store", store)

    authorization = await _relay_authorization(
        "Bearer frontend-test-key",
        configured_key="",
        credential_ref=reference,
    )

    assert authorization == ["Bearer vault-only-test-key"]


@pytest.mark.parametrize(
    ("incoming", "expected"),
    [
        (
            [("Authorization", "Bearer first-test-key"), ("Authorization", "")],
            "Bearer first-test-key",
        ),
        (
            [("Authorization", ""), ("Authorization", "Basic synthetic-token")],
            "Basic synthetic-token",
        ),
        (
            [
                ("Authorization", "Bearer first-test-key"),
                ("authorization", "Basic second-synthetic-token"),
            ],
            "Bearer first-test-key",
        ),
    ],
)
async def test_duplicate_authorization_forwards_first_usable_once(incoming, expected):
    assert await _relay_authorization(incoming, configured_key="") == [expected]
