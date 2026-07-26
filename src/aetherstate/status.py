"""Control-plane skeleton: /aether/status (10 SS5). Separate router — a control-plane
exception can never touch the OpenAI relay surface (09 F3).

P3b: surfaces the extraction subsystem — capability cache (rung + native dialect per
(base_url, model)), demotion strike counts, effective mode/routing, and per-session
09 C2 breaker state. Local-only data; there is no telemetry (invariant 4)."""
from __future__ import annotations

import time

from fastapi import APIRouter

from . import __version__

_STARTED = time.monotonic()


def _record_failure(health, subsystem: str, error_code: str, exc: Exception) -> None:
    if health is None:
        return
    try:
        health.record_failure(
            subsystem,
            error_code,
            exception=exc,
        )
    except Exception:
        pass


def _record_success(health, recovery_proof: str) -> None:
    if health is None:
        return
    try:
        health.record_success(recovery_proof)
    except Exception:
        pass


def _empty_health(schema: str) -> dict[str, object]:
    return {
        "schema": schema,
        "state": "none",
        "active_condition_count": 0,
        "total_condition_count": 0,
        "durable_available": False,
        "conditions": [],
    }


def _health_projection(health, *, diagnostic: bool = False) -> dict[str, object]:
    schema = (
        "aetherstate-system-health-diagnostic/1"
        if diagnostic
        else "aetherstate-system-health/1"
    )
    if health is None:
        return _empty_health(schema)
    try:
        return health.diagnostic_export() if diagnostic else health.snapshot()
    except Exception:
        return _empty_health(schema)


def _extraction_view(cfg, store, jobs, health=None) -> dict:
    ge = getattr(cfg.assist, "group_endpoints", None)
    out: dict = {"mode": cfg.extraction.mode,
                 "thinking": cfg.extraction.thinking,
                 "groups": cfg.assist.groups.model_dump(),
                 "group_endpoints": ge.model_dump() if ge is not None else {},
                 "force_rung": cfg.upstream.force_rung or None,
                 "assist_endpoints": [
                     {"name": e.name, "model": e.model, "tier": e.tier,
                      "base_url": e.base_url, "max_concurrent": e.max_concurrent}
                     for e in cfg.assist.endpoints],
                 "caps": [], "breakers": []}
    if store is not None:
        try:
            out["caps"] = [{"base_url": r["base_url"], "model": r["model"],
                            "rung": r["rung"], "native": r["native"] or None,
                            "anyof": (None if r["anyof"] == -1 else bool(r["anyof"])),
                            "failures": r["failures"],
                            "probed_at": round(r["probed_at"], 1)}
                           for r in store.caps_all()]
        except Exception as exc:
            _record_failure(
                health,
                "status",
                "extraction_snapshot_failed",
                exc,
            )
        else:
            _record_success(health, "extraction_snapshot_succeeded")
    if jobs is not None:
        out["breakers"] = [{"session": sid, "disabled_until_turn": turn}
                           for sid, turn in sorted(jobs._disabled_until.items())]
        out["consecutive_fails"] = dict(jobs._fails)
    return out


def _status_summary(cfg, store, health) -> dict[str, object]:
    linter_enabled = bool(cfg.linter.enabled)
    director_enabled = bool(cfg.director.enabled)
    director_libraries = list(cfg.director.beat_libraries)
    try:
        sessions = (
            store.db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            if store
            else 0
        )
        violations = store.lint_counts() if store else {}
        firings = store.director_counts() if store else {}
    except Exception as exc:
        _record_failure(
            health,
            "status",
            "status_summary_invariant_failed",
            exc,
        )
        return {
            "sessions": 0,
            "linter": {
                "enabled": linter_enabled,
                "violations": {},
            },
            "director": {
                "enabled": director_enabled,
                "libraries": director_libraries,
                "firings": {},
            },
        }
    _record_success(health, "status_summary_succeeded")
    return {
        "sessions": sessions,
        "linter": {
            "enabled": linter_enabled,
            "violations": violations,
        },
        "director": {
            "enabled": director_enabled,
            "libraries": director_libraries,
            "firings": firings,
        },
    }


def make_status_router(cfg, store=None, jobs=None, pipeline=None) -> APIRouter:
    router = APIRouter(prefix="/aether")
    health = getattr(store, "system_health", None)

    @router.get("/status")
    async def status():
        try:      # Phase 0a: prompt-cache hit rates (status must never 500 — 09 F3)
            cache = (pipeline.cache.snapshot(cfg) if pipeline is not None
                     else {"enabled": bool(getattr(cfg.upstream, "cache_key", True))})
        except Exception as exc:
            cache = {}
            _record_failure(
                health,
                "status",
                "prompt_cache_snapshot_failed",
                exc,
            )
        else:
            _record_success(health, "prompt_cache_snapshot_succeeded")
        extraction = _extraction_view(cfg, store, jobs, health)
        summary = _status_summary(cfg, store, health)
        health_view = _health_projection(health)
        return {
            "name": "aetherstate",
            "version": __version__,
            "mode": "enriched",              # P2+: Tier-0 + header composition active
            "degradation": health_view["state"],
            "health": health_view,
            "specialization": cfg.specialization.name,   # Q27 / doc 05 (none|rpg)
            "config_source": cfg.source,
            "upstream_configured": bool(cfg.upstream.base_url),
            "data_dir": cfg.server.data_dir,
            "uptime_s": round(time.monotonic() - _STARTED, 1),
            "sessions": summary["sessions"],
            "cache": cache,                  # Phase 0a: prompt-cache key + hit rates
            "extraction": extraction,
            "linter": summary["linter"],
            "director": summary["director"],
            "telemetry": "none, ever",
        }

    @router.get("/health/diagnostics")
    async def health_diagnostics():
        return _health_projection(health, diagnostic=True)

    return router
