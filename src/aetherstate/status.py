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


def _extraction_view(cfg, store, jobs) -> dict:
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
        except Exception:
            pass                                   # status must never 500 (09 F3)
    if jobs is not None:
        out["breakers"] = [{"session": sid, "disabled_until_turn": turn}
                           for sid, turn in sorted(jobs._disabled_until.items())]
        out["consecutive_fails"] = dict(jobs._fails)
    return out


def make_status_router(cfg, store=None, jobs=None, pipeline=None) -> APIRouter:
    router = APIRouter(prefix="/aether")

    @router.get("/status")
    async def status():
        try:      # Phase 0a: prompt-cache hit rates (status must never 500 — 09 F3)
            cache = (pipeline.cache.snapshot(cfg) if pipeline is not None
                     else {"enabled": bool(getattr(cfg.upstream, "cache_key", True))})
        except Exception:
            cache = {}
        return {
            "name": "aetherstate",
            "version": __version__,
            "mode": "enriched",              # P2+: Tier-0 + header composition active
            "degradation": "none",
            "specialization": cfg.specialization.name,   # Q27 / doc 05 (none|rpg)
            "config_source": cfg.source,
            "upstream_configured": bool(cfg.upstream.base_url),
            "data_dir": cfg.server.data_dir,
            "uptime_s": round(time.monotonic() - _STARTED, 1),
            "sessions": (store.db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
                         if store else 0),
            "cache": cache,                  # Phase 0a: prompt-cache key + hit rates
            "extraction": _extraction_view(cfg, store, jobs),
            "linter": {"enabled": cfg.linter.enabled,        # violations by rule (10 SS4)
                       "violations": (store.lint_counts() if store else {})},
            "director": {"enabled": cfg.director.enabled,    # beat firings (10 SS4)
                         "libraries": list(cfg.director.beat_libraries),
                         "firings": (store.director_counts() if store else {})},
            "telemetry": "none, ever",
        }

    return router
