"""Global ordered database-schema registry assembly."""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import cast

from .schema_migrations import SchemaMigration


MigrationFactory = Callable[[], tuple[SchemaMigration, ...]]


def _lazy_migration_factory(module: str, name: str) -> MigrationFactory:
    """Resolve one runtime registry factory without making its domain a static dependency."""
    candidate = getattr(import_module(module, package=__package__), name)
    if not callable(candidate):
        raise RuntimeError("database schema migration factory is unavailable")
    return cast(MigrationFactory, candidate)


def database_schema_migrations() -> tuple[SchemaMigration, ...]:
    """Assemble registered domains without owning any schema behavior."""
    store_schema_migrations = _lazy_migration_factory(
        ".store_schema", "store_schema_migrations"
    )
    worldlex_schema_migrations = _lazy_migration_factory(
        ".worldlex_store", "worldlex_schema_migrations"
    )
    turn_lifecycle_schema_migrations = _lazy_migration_factory(
        ".turn_lifecycle", "turn_lifecycle_schema_migrations"
    )
    playerlex_schema_migrations = _lazy_migration_factory(
        ".playerlex", "playerlex_schema_migrations"
    )
    player_lessons_schema_migrations = _lazy_migration_factory(
        ".player_lessons", "player_lessons_schema_migrations"
    )

    return tuple(sorted(
        store_schema_migrations()
        + worldlex_schema_migrations()
        + turn_lifecycle_schema_migrations()
        + playerlex_schema_migrations()
        + player_lessons_schema_migrations(),
        key=lambda migration: migration.version,
    ))
