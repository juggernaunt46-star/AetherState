from __future__ import annotations

from dataclasses import replace

import pytest

import aetherstate.system_health as system_health_module
from aetherstate.app import create_app
from aetherstate.config import Config
from aetherstate.database_schema import database_schema_migrations
from aetherstate.store import Store
from aetherstate.system_health import SystemHealth


def _migration_identity() -> tuple[int, str, str]:
    migration = database_schema_migrations()[-1]
    return migration.version, migration.name, migration.domain


def test_global_registry_adds_one_ordered_system_health_owner() -> None:
    migrations = database_schema_migrations()

    assert [migration.version for migration in migrations] == list(range(1, 8))
    assert _migration_identity() == (
        7,
        "system-health-1.24-baseline",
        "system-health",
    )
    assert len({migration.name for migration in migrations}) == len(migrations)


def test_store_exposes_health_on_the_shared_connection_and_runner(tmp_path) -> None:
    store = Store(tmp_path / "health.sqlite3")
    try:
        assert isinstance(store.system_health, SystemHealth)
        assert store.system_health._connection is store.db
        assert store.system_health._migration_runner is store.schema_migrations
        assert store.schema_migrations.applied()[-1] == _migration_identity()
    finally:
        store.close()


def test_legacy_store_migrates_additively_without_changing_existing_rows(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy = Store(path)
    try:
        before = dict(legacy.get_or_create_session("preserved-session"))
    finally:
        legacy.close()

    migrated = Store(path)
    try:
        assert dict(migrated.get_or_create_session("preserved-session")) == before
        assert migrated.schema_migrations.applied()[-1] == _migration_identity()
        assert migrated.db.execute(
            "SELECT count(*) FROM aetherstate_system_health"
        ).fetchone()[0] == 0
    finally:
        migrated.close()


def test_health_migration_failure_keeps_store_available_with_memory_truth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = system_health_module.system_health_schema_migrations()[0]

    def fail_transform(_connection) -> None:
        raise RuntimeError("private migration detail")

    monkeypatch.setattr(
        system_health_module,
        "system_health_schema_migrations",
        lambda: (replace(real, transform=fail_transform),),
    )

    store = Store(tmp_path / "fallback.sqlite3")
    try:
        correlation_id = store.system_health.record_failure(
            "startup",
            "pending_extraction_resume_failed",
            exception=RuntimeError("private request text"),
        )
        snapshot = store.system_health.snapshot()

        assert correlation_id
        assert snapshot["state"] == "degraded"
        assert snapshot["durable_available"] is False
        assert snapshot["conditions"][0]["error_code"] == (
            "pending_extraction_resume_failed"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_lifespan_resume_failure_records_and_success_recovers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config()
    cfg.server.data_dir = str(tmp_path)
    store = Store(":memory:")
    app = create_app(cfg, store=store)

    async def stop_without_side_effects() -> None:
        return None

    monkeypatch.setattr(app.state.jobs, "stop", stop_without_side_effects)
    monkeypatch.setattr(
        app.state.jobs,
        "resume_pending",
        lambda: (_ for _ in ()).throw(RuntimeError("private pending job")),
    )

    async with app.router.lifespan_context(app):
        pass

    failed = app.state.store.system_health.snapshot()
    condition = next(
        item
        for item in failed["conditions"]
        if item["error_code"] == "pending_extraction_resume_failed"
    )
    assert condition["active"] is True
    assert failed["state"] == "degraded"

    monkeypatch.setattr(app.state.jobs, "resume_pending", lambda: None)
    async with app.router.lifespan_context(app):
        pass

    recovered = app.state.store.system_health.snapshot()
    condition = next(
        item
        for item in recovered["conditions"]
        if item["error_code"] == "pending_extraction_resume_failed"
    )
    assert condition["active"] is False
    assert condition["recovered_at"] is not None
    assert recovered["state"] == "none"
    store.close()
