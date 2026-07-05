"""Control API core + read-only inspector 'Now' view (07 P2; 10 SS3/SS5 core; 02 SS12b).

Separate router from the relay (09 F3). Every write goes through state.apply_delta with
source='user' — the same authority matrix as OOC commands and the extension panel.
Localhost trust in P2 (12 [ui]: auth_token empty = single-user default)."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .state import apply_delta, current_state, state_summary, translate_path, validate_op

log = logging.getLogger("aetherstate.control")


def _session(store, sid: str):
    return store.db.execute("SELECT * FROM sessions WHERE session_id=? OR external_id=?",
                            (sid, sid)).fetchone()


def _head(store, branch_id: str) -> int:
    row = store.db.execute("SELECT head_turn FROM branches WHERE branch_id=?",
                           (branch_id,)).fetchone()
    return row["head_turn"] if row else -1


def _toml_val(v) -> str:
    """Serialize one scalar/array to a TOML value. bool must precede int (bool is an int)."""
    import json as _json
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _json.dumps(v)          # JSON string == valid TOML basic string for our content
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return _json.dumps(str(v))


def _toml_dumps(d: dict, prefix: str = "") -> str:
    """Minimal, dependency-free TOML emitter for the nested dicts this module builds.
    Handles scalars, arrays of scalars, sub-tables (dict) and arrays-of-tables (list[dict]).
    Scalars are emitted before any sub-table header at each level (TOML ordering rule).
    Round-trips through tomllib; comments are not preserved (Console owns config.toml)."""
    scalars, tables = [], []
    for k, v in d.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            tables.append((k, v))       # array-of-tables (empty list -> scalar `k = []`)
        else:
            scalars.append((k, v))
    out = [f"{k} = {_toml_val(v)}" for k, v in scalars]
    for k, v in tables:
        name = f"{prefix}{k}"
        if isinstance(v, list):
            for item in v:
                body = _toml_dumps(item)
                out.append(f"\n[[{name}]]" + (("\n" + body) if body else ""))
        else:
            body = _toml_dumps(v, f"{name}.")
            out.append(f"\n[{name}]" + (("\n" + body) if body else ""))
    return "\n".join(out)


def _persist_config(cfg) -> bool:
    """Merge the Console-managed values into config.toml so connection/extraction changes
    survive a restart WITHOUT clobbering host/port/data_dir or any hand-tuned section the
    Console doesn't manage (the old writer emitted a partial [server] block and dropped
    host/port -- restart then fell back to 127.0.0.1:9130). We read the existing file, overlay
    only the managed keys, and re-emit the whole thing.

    Hardening: chmod 0600 on POSIX so the upstream key isn't world-readable; if the key is
    supplied via env (AETHERSTATE_UPSTREAM__API_KEY) it is authoritative and is NOT written to
    disk (and is actively dropped from the file), keeping the secret off disk entirely."""
    import os as _os
    from pathlib import Path as _P

    try:
        import tomllib as _toml  # py311+
    except ModuleNotFoundError:            # py310
        import tomli as _toml               # type: ignore[no-redef]

    try:
        # write back to the exact file the config loaded from (--config path); fall back to
        # data_dir/config.toml only when the load path is unknown (e.g. pure-defaults start).
        target = _P(cfg.source_path) if getattr(cfg, "source_path", "") \
            else _P(cfg.server.data_dir) / "config.toml"
        base: dict = {}
        if target.is_file():               # start from what's on disk -> unmanaged sections survive
            try:
                base = _toml.loads(target.read_text(encoding="utf-8"))
            except Exception:
                base = {}

        def _sec(name: str) -> dict:
            d = base.get(name)
            if not isinstance(d, dict):
                d = {}
                base[name] = d
            return d

        # [server]: always re-assert host/port/data_dir. The Console never edits these, but the
        # old writer dropped them -- re-emitting from the live cfg is what fixes the restart bug.
        srv = _sec("server")
        srv["host"] = cfg.server.host
        srv["port"] = int(cfg.server.port)
        srv["data_dir"] = cfg.server.data_dir
        srv["cors_origins"] = list(cfg.server.cors_origins)

        # [upstream]
        up = _sec("upstream")
        up["base_url"] = cfg.upstream.base_url
        if _os.environ.get("AETHERSTATE_UPSTREAM__API_KEY"):
            up.pop("api_key", None)        # env is authoritative -> keep the secret off disk
        else:
            up["api_key"] = cfg.upstream.api_key

        # [[assist.endpoints]] + [assist.groups] (Console fully owns these)
        assist = _sec("assist")
        assist["endpoints"] = [
            {"name": e.name, "base_url": e.base_url, "api_key": e.api_key,
             "model": e.model, "tier": e.tier, "max_concurrent": int(e.max_concurrent)}
            for e in cfg.assist.endpoints]
        g = cfg.assist.groups
        assist["groups"] = {
            "extraction": g.extraction or cfg.extraction.mode,
            "director_selection": g.director_selection, "linter_nli": g.linter_nli,
            "memory_reflection": g.memory_reflection, "embeddings": g.embeddings,
            "lore_gen": g.lore_gen}

        # [extraction] (user-tunable subset)
        x = cfg.extraction
        _sec("extraction").update({
            "cadence_turns": int(x.cadence_turns), "intake_chars": int(x.intake_chars),
            "debounce_s": float(x.debounce_s), "thinking": x.thinking})

        _sec("manual_override")["enabled"] = bool(cfg.manual_override.enabled)
        _sec("user_guard")["name"] = cfg.user_guard.name
        _sec("consent")["safewords"] = list(cfg.consent.safewords)

        base.pop("source", None)           # runtime-only load marker; never persist
        header = ("# AetherState config -- the [server]/[upstream]/[assist]/[extraction] keys "
                  "below are\n# managed by the Console. Other sections are preserved on save; "
                  "comments are not.\n")
        target.write_text(header + _toml_dumps(base) + "\n", encoding="utf-8")
        if _os.name == "posix":            # POSIX-only; NTFS ACLs differ and chmod is a no-op there
            try:
                _os.chmod(target, 0o600)
            except OSError:
                pass
        return True
    except Exception:
        return False


def make_control_router(cfg, store, jobs=None) -> APIRouter:
    router = APIRouter(prefix="/aether")

    @router.get("/console")
    async def console():
        """The AetherState Console (Q11 addendum 2; P5 UI base) — same-origin, no CORS."""
        return FileResponse(Path(__file__).parent / "static" / "console.html",
                            media_type="text/html")

    @router.get("/override")
    async def override_get():
        return {"enabled": bool(cfg.manual_override.enabled)}

    @router.post("/override")
    async def override_set(request: Request):
        """Visible manual-override toggle (Q11): runtime-only; config.toml persists."""
        try:
            payload = await request.json()
            cfg.manual_override.enabled = bool(payload.get("enabled"))
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        return {"enabled": bool(cfg.manual_override.enabled)}

    @router.post("/session/{sid}/genesis")
    async def genesis_seed(sid: str, request: Request, force: int = 0):
        """Turn-0 genesis at chat-open (handoff 2026-07-04, REQUIRED). The extension
        posts card/greeting/speaker the moment a chat opens — state is seeded BEFORE
        the first message. Idempotent via the genesis marker; the first-request
        pipeline path stays as fallback for no-extension setups.

        ?force=1 clears the marker first so a session that once seeded empty (the
        pre-fix thinking-model bug marked those 'done' forever) can re-seed without
        starting a new chat. The slash command sends force — explicit user intent."""
        from . import genesis as _genesis
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        row = _session(store, sid)
        if not row:
            session_id, _branch = store.create_session(external_id=sid)
            row = _session(store, session_id)
        prior = store.genesis_state(row["session_id"])
        if force and prior:
            store.genesis_mark(row["session_id"], "")
        speaker = str(payload.get("speaker", ""))[:80]
        card_len = len(str(payload.get("card", "")))
        greeting_len = len(str(payload.get("greeting", "")))
        doc = {"messages": [
            {"role": "system", "content": str(payload.get("card", ""))[:12000]},
            {"role": "assistant", "content": str(payload.get("greeting", ""))[:4000]},
            {"role": "user", "content": str(payload.get("opening", ""))[:4000]}]}
        applied = _genesis.seed_rules(store, cfg, row["session_id"],
                                      row["active_branch"], doc, speaker=speaker)
        scheduled = False
        if jobs is not None and cfg.extraction.mode not in ("off", "rules"):
            try:
                import asyncio
                card, opening = _genesis.card_and_prompt(doc)
                if card.strip():
                    ep, _, _ = jobs.endpoint_for(row["session_id"])
                    t = asyncio.get_running_loop().create_task(_genesis.seed_llm(
                        store, cfg, jobs.ladder.get_client, ep, row["session_id"],
                        row["active_branch"], card, opening, speaker=speaker))
                    jobs._tasks.add(t)
                    t.add_done_callback(jobs._tasks.discard)
                    scheduled = True
            except Exception:
                pass                          # stage A alone is a valid seed (fail-open)
        elif store.genesis_state(row["session_id"]) == "rules":
            store.genesis_mark(row["session_id"], "done")
        log.info("genesis endpoint: ext=%s sid=%s prior=%r force=%s card=%d "
                 "greeting=%d speaker=%r stageA_applied=%d stageB_scheduled=%s",
                 sid, row["session_id"][:8], prior, bool(force), card_len,
                 greeting_len, speaker, applied, scheduled)
        return {"session_id": row["session_id"], "applied": applied,
                "stage": "rules", "scheduled": scheduled, "prior_state": prior,
                "forced": bool(force), "card_len": card_len,
                "greeting_len": greeting_len, "speaker": speaker}

    @router.post("/hint")
    async def hint(request: Request):
        """05 SS5: fire-and-forget frontend hints (swipe/edit/delete/chat_changed).
        Recorded for the classifier; the proxy NEVER depends on them."""
        try:
            payload = await request.json()
            store.db.execute(
                "INSERT INTO hints(session_ext, event, message_index, ts)"
                " VALUES(?,?,?,strftime('%s','now'))",
                (str(payload.get("session", ""))[:80], str(payload.get("event", ""))[:40],
                 int(payload.get("messageIndex", -1))))
            store.db.commit()
        except Exception:
            pass                              # hints are advisory by contract (05 SS5)
        return {"ok": True}

    @router.post("/session/{sid}/mode")
    async def set_mode(sid: str, request: Request):
        """05 SS7: enrichment on/off per session. passthrough = byte-exact relay."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        try:
            mode = (await request.json()).get("mode", "")
        except Exception:
            mode = ""
        if mode not in ("enriched", "passthrough"):
            return JSONResponse({"error": "mode must be enriched|passthrough"},
                                status_code=422)
        store.session_mode_set(row["session_id"], mode)
        return {"session_id": row["session_id"], "mode": mode}

    @router.get("/session/{sid}/writeback")
    async def writeback(sid: str, cursor: int = 0):
        """05 SS6 v1: chat-metadata patch only. world_info/authors_note stay empty until
        route-split (06 B.1) lands proxy-side — everything is proxy-spliced today, so
        returning components here would double-inject (02 SS12 route guard)."""
        row = _session(store, sid)
        if not row:                           # pre-first-message poll: quiet empty patch
            return {"cursor": cursor, "world_info": [], "authors_note": None,
                    "chat_metadata_patch": {}}
        head = _head(store, row["active_branch"])
        return {"cursor": max(cursor, head), "world_info": [], "authors_note": None,
                "chat_metadata_patch": {"aetherstate": {
                    "session": row["session_id"], "last_extracted_turn": head,
                    "frozen": bool(row["frozen"]), "mode": store.session_mode(row["session_id"])}}}

    @router.get("/extraction")
    async def extraction_get():
        e = cfg.extraction
        return {"mode": e.mode, "cadence_turns": e.cadence_turns,
                "intake_chars": e.intake_chars, "debounce_s": e.debounce_s}

    @router.post("/extraction")
    async def extraction_set(request: Request):
        """User-set update cadence + transcript intake budget.
        Live immediately; persisted to config.toml."""
        try:
            p = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if "cadence_turns" in p:
            try:
                cfg.extraction.cadence_turns = min(50, max(1, int(p["cadence_turns"])))
            except (TypeError, ValueError):
                return JSONResponse({"error": "cadence_turns must be 1-50"}, status_code=422)
        if "intake_chars" in p:
            try:
                cfg.extraction.intake_chars = min(200000, max(0, int(p["intake_chars"])))
            except (TypeError, ValueError):
                return JSONResponse({"error": "intake_chars must be 0-200000"}, status_code=422)
        if "debounce_s" in p:
            try:
                cfg.extraction.debounce_s = min(600.0, max(3.0, float(p["debounce_s"])))
            except (TypeError, ValueError):
                return JSONResponse({"error": "debounce_s must be 3-600"}, status_code=422)
        e = cfg.extraction
        log.info("extraction settings: cadence=%d intake=%d debounce=%.0fs",
                 e.cadence_turns, e.intake_chars, e.debounce_s)
        return {"mode": e.mode, "cadence_turns": e.cadence_turns,
                "intake_chars": e.intake_chars, "debounce_s": e.debounce_s,
                "persisted": _persist_config(cfg)}

    @router.post("/groups")
    async def set_group(request: Request):
        """05 SS7: assist feature-group mirrors (Q8, live-toggleable). Runtime-only."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        valid_modes = {"off", "rules", "main", "assist"}
        out = {}
        for group, mode in payload.items():
            if hasattr(cfg.assist.groups, group) and mode in valid_modes:
                setattr(cfg.assist.groups, group, mode)
                out[group] = mode
        return {"applied": out}

    @router.get("/connection")
    async def connection_get():
        return {"upstream": {"base_url": cfg.upstream.base_url, "has_key": bool(cfg.upstream.api_key)},
                "assist": [{"name": e.name, "base_url": e.base_url, "has_key": bool(e.api_key),
                            "model": e.model, "tier": e.tier} for e in cfg.assist.endpoints]}

    @router.post("/connection/models")
    async def connection_models(request: Request):
        """List models from an endpoint, server-side (no CORS, uses the truststore/Avast fix)."""
        try:
            p = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        base = str(p.get("base_url", "")).strip().rstrip("/")
        key = str(p.get("api_key", ""))
        if not base:
            return JSONResponse({"error": "base_url required"}, status_code=400)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            import httpx
            auth_ok, auth_status = None, None
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.get(base + "/models", headers=headers)
                ids = []
                try:
                    ids = sorted([m.get("id") for m in (r.json().get("data") or []) if m.get("id")])
                except Exception:
                    pass
                # /models is often public (Venice), so a real auth check needs an authed call.
                if ids:
                    try:
                        cr = await c.post(base + "/chat/completions", headers=headers,
                                          json={"model": ids[0],
                                                "messages": [{"role": "user", "content": "hi"}],
                                                "max_tokens": 1})
                        # only 401/403 mean the KEY is bad; 5xx/model errors are not auth failures
                        auth_status, auth_ok = cr.status_code, cr.status_code not in (401, 403)
                    except Exception as exc:
                        auth_ok, auth_status = False, type(exc).__name__
            return {"ok": r.status_code < 400, "status": r.status_code, "models": ids,
                    "auth_ok": auth_ok, "auth_status": auth_status}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:140], "models": []}

    @router.post("/connection")
    async def connection_set(request: Request):
        """Point AetherState's upstream (main writer) or assist (mechanics) at an endpoint.
        Live immediately (the relay reads cfg per request) + persisted to config.toml."""
        try:
            p = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        target = p.get("target", "upstream")
        if target == "upstream":
            if "base_url" in p:
                cfg.upstream.base_url = str(p["base_url"]).strip()
            if p.get("api_key"):
                cfg.upstream.api_key = str(p["api_key"])
        elif target == "assist":
            from .config import AssistEndpointConfig
            e = cfg.assist.endpoints[0] if cfg.assist.endpoints else None
            if e is None:
                e = AssistEndpointConfig(name="custom")
                cfg.assist.endpoints.append(e)
            if "base_url" in p:
                e.base_url = str(p["base_url"]).strip()
            if p.get("api_key"):
                e.api_key = str(p["api_key"])
            if "model" in p:
                e.model = str(p["model"])
            if p.get("tier"):
                e.tier = str(p["tier"])
        else:
            return JSONResponse({"error": "target must be upstream|assist"}, status_code=422)
        return {"ok": True, "persisted": _persist_config(cfg),
                "upstream": {"base_url": cfg.upstream.base_url, "has_key": bool(cfg.upstream.api_key)},
                "assist": [{"name": e.name, "base_url": e.base_url, "has_key": bool(e.api_key),
                            "model": e.model, "tier": e.tier} for e in cfg.assist.endpoints]}

    @router.get("/sessions")
    async def sessions():
        rows = store.db.execute(
            "SELECT s.session_id, s.external_id, s.label, s.frontend, s.frozen, s.last_seen,"
            " s.active_branch, b.head_turn FROM sessions s"
            " LEFT JOIN branches b ON b.branch_id=s.active_branch"
            " ORDER BY s.last_seen DESC").fetchall()
        return {"sessions": [dict(r) for r in rows]}

    @router.post("/session/{sid}/label")
    async def set_label(sid: str, request: Request):
        """Rename a session (user-facing friendly name)."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        try:
            label = str((await request.json()).get("label", ""))
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        store.session_label_set(row["session_id"], label)
        return {"session_id": row["session_id"], "label": label[:120]}

    @router.delete("/session/{sid}")
    async def delete_session(sid: str):
        """Delete a session and all its data."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        store.session_delete(row["session_id"])
        return {"deleted": row["session_id"]}

    @router.get("/session/{sid}/state")
    async def now_view(sid: str):
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        state = current_state(store, row["active_branch"])
        return {"session_id": row["session_id"], "external_id": row["external_id"],
                "branch_id": row["active_branch"], "frozen": bool(row["frozen"]),
                "head_turn": _head(store, row["active_branch"]),
                "state": state_summary(state)}

    @router.post("/session/{sid}/freeze")
    async def freeze(sid: str):
        return _user_ops(sid, [{"op": "freeze", "reason": "user"}])

    @router.post("/session/{sid}/unfreeze")
    async def unfreeze(sid: str):   # unfreeze is user-only by design (02 SS6)
        return _user_ops(sid, [{"op": "unfreeze"}])

    @router.patch("/session/{sid}/state")
    async def patch_state(sid: str, request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        ops = []
        rejected = []
        if "path" in payload:
            op = translate_path(str(payload["path"]), str(payload.get("value", "")))
            if op is None:
                return JSONResponse({"applied": 0, "rejected": [{
                    "path": payload["path"],
                    "reason": "unknown/unsupported path (02 SS12b: rejected visibly)"}]},
                    status_code=422)
            ops.append(op)
        for op in payload.get("ops", []):
            if validate_op(op) is not None:
                ops.append(op)
            else:
                rejected.append({"op": op, "reason": "malformed op (02 SS11)"})
        result = _user_ops(sid, ops, extra_rejected=rejected)
        return result

    def _user_ops(sid: str, ops: list, extra_rejected: list | None = None):
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        res = apply_delta(store, row["session_id"], row["active_branch"],
                          _head(store, row["active_branch"]), ops, "user", cfg)
        rejected = (extra_rejected or []) + [
            {"op": q["op"].get("op"), "reason": q["reason"]} for q in res.quarantined]
        return {"applied": len(res.applied), "rejected": rejected,
                "frozen": bool(res.state.get("frozen"))}

    return router
