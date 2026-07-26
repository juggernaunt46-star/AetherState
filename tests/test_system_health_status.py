from __future__ import annotations

import pytest


def _condition(value: dict, error_code: str) -> dict:
    return next(
        item for item in value["health"]["conditions"] if item["error_code"] == error_code
    )


@pytest.mark.asyncio
async def test_status_health_is_additive_and_preserves_stable_shape(client) -> None:
    response = await client.get("/aether/status")
    value = response.json()

    assert response.status_code == 200
    assert value["degradation"] == "none"
    assert isinstance(value["degradation"], str)
    assert value["health"]["schema"] == "aetherstate-system-health/1"
    assert value["health"]["state"] == "none"
    assert value["health"]["active_condition_count"] == 0
    assert value["telemetry"] == "none, ever"


@pytest.mark.asyncio
async def test_status_aggregate_uses_unexpected_precedence(
    client, proxy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = proxy_app.state.store.system_health
    health.record_failure("startup", "pending_extraction_resume_failed")

    degraded = (await client.get("/aether/status")).json()
    assert degraded["degradation"] == "degraded"

    monkeypatch.setattr(
        proxy_app.state.store,
        "lint_counts",
        lambda: (_ for _ in ()).throw(RuntimeError("private invariant detail")),
    )
    failed = (await client.get("/aether/status")).json()
    assert failed["degradation"] == "failed"


@pytest.mark.asyncio
async def test_prompt_cache_failure_stays_200_and_recovers_only_after_success(
    client, proxy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = proxy_app.state.pipeline.cache
    real_snapshot = cache.snapshot
    monkeypatch.setattr(
        cache,
        "snapshot",
        lambda _cfg: (_ for _ in ()).throw(
            RuntimeError("secret prompt and C:\\Users\\private\\chat.json")
        ),
    )

    failed_response = await client.get("/aether/status")
    failed = failed_response.json()
    condition = _condition(failed, "prompt_cache_snapshot_failed")

    assert failed_response.status_code == 200
    assert failed["degradation"] == "degraded"
    assert condition["active"] is True
    assert "secret prompt" not in failed_response.text
    assert "C:\\Users\\private" not in failed_response.text

    monkeypatch.setattr(cache, "snapshot", real_snapshot)
    recovered = (await client.get("/aether/status")).json()
    condition = _condition(recovered, "prompt_cache_snapshot_failed")

    assert condition["active"] is False
    assert condition["recovered_at"] is not None
    assert recovered["degradation"] == "none"


@pytest.mark.asyncio
async def test_extraction_failure_stays_200_and_has_independent_recovery(
    client, proxy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = proxy_app.state.store
    real_caps_all = store.caps_all
    monkeypatch.setattr(
        store,
        "caps_all",
        lambda: (_ for _ in ()).throw(RuntimeError("private model response")),
    )

    failed = (await client.get("/aether/status")).json()
    condition = _condition(failed, "extraction_snapshot_failed")

    assert condition["active"] is True
    assert failed["degradation"] == "degraded"

    monkeypatch.setattr(store, "caps_all", real_caps_all)
    recovered = (await client.get("/aether/status")).json()
    condition = _condition(recovered, "extraction_snapshot_failed")

    assert condition["active"] is False
    assert recovered["degradation"] == "none"


@pytest.mark.asyncio
async def test_unexpected_status_summary_failure_stays_200_then_recovers(
    client, proxy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = proxy_app.state.store
    real_lint_counts = store.lint_counts
    monkeypatch.setattr(
        store,
        "lint_counts",
        lambda: (_ for _ in ()).throw(RuntimeError("private invariant detail")),
    )

    failed_response = await client.get("/aether/status")
    failed = failed_response.json()
    condition = _condition(failed, "status_summary_invariant_failed")

    assert failed_response.status_code == 200
    assert failed["degradation"] == "failed"
    assert condition["active"] is True
    assert "private invariant detail" not in failed_response.text

    monkeypatch.setattr(store, "lint_counts", real_lint_counts)
    recovered = (await client.get("/aether/status")).json()
    condition = _condition(recovered, "status_summary_invariant_failed")

    assert condition["active"] is False
    assert recovered["degradation"] == "none"


@pytest.mark.asyncio
async def test_diagnostic_export_is_content_free(client, proxy_app) -> None:
    proxy_app.state.store.system_health.record_failure(
        "status",
        "status_summary_invariant_failed",
        exception=RuntimeError(
            "credential sk-private prompt player prose C:\\Users\\private\\session.json"
        ),
    )

    response = await client.get("/aether/health/diagnostics")
    value = response.json()
    forbidden = (
        "data_dir",
        "config_source",
        "sk-private",
        "player prose",
        "C:\\Users\\private",
        "prompt",
        "response",
    )

    assert response.status_code == 200
    assert value["schema"] == "aetherstate-system-health-diagnostic/1"
    assert value["state"] == "failed"
    assert all(token not in response.text for token in forbidden)
