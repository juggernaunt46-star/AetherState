"""App factory. get_client is injectable so the harness mounts MockUpstream in-process (11 SS1)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pathlib import Path

from .config import Config
from .control import make_control_router
from .extraction import Ladder
from .jobs import JobRunner
from .pipeline import Pipeline
from .proxy import make_relay_router
from .session_engine import SessionEngine
from .status import make_status_router
from .store import Store


def create_app(cfg: Config, client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
               store: Optional[Store] = None) -> FastAPI:
    _client: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:    # 2026-07-04: re-queue extraction left 'pending' by a restart (fail-open)
            app.state.jobs.resume_pending()
        except Exception:
            pass
        yield
        if getattr(app.state, "jobs", None) is not None:
            await app.state.jobs.stop()
        if "c" in _client:
            await _client["c"].aclose()

    app = FastAPI(title="AetherState", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.server.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    def default_factory() -> httpx.AsyncClient:
        if "c" not in _client:
            _client["c"] = httpx.AsyncClient(base_url=cfg.upstream.base_url, timeout=httpx.Timeout(
                connect=10.0, read=None if cfg.upstream.idle_timeout_s == 0 else cfg.upstream.idle_timeout_s,
                write=60.0, pool=None))
        return _client["c"]

    get_client = client_factory or default_factory

    if store is None:
        store = Store(Path(cfg.server.data_dir) / "aetherstate.db")
    engine = SessionEngine(store, cfg.session)
    jobs = JobRunner(store, cfg, Ladder(store, cfg, get_client))
    pipeline = Pipeline(store, engine, cfg, jobs=jobs)
    app.state.store, app.state.engine = store, engine
    app.state.pipeline, app.state.jobs = pipeline, jobs

    app.include_router(make_status_router(cfg, store, jobs, pipeline))   # control plane FIRST (more specific prefix)
    app.include_router(make_control_router(cfg, store, jobs=jobs, pipeline=pipeline))
    app.include_router(make_relay_router(get_client, cfg, engine, pipeline))  # catch-all relay LAST


    return app
