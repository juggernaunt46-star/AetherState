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


def _persist_config(cfg) -> bool:
    """Rewrite config.toml from the live cfg so Console connection changes survive a restart.
    Emits the managed sections only; comments are not preserved (Console owns this file)."""
    import json as _json
    from pathlib import Path as _P
    try:
        up = cfg.upstream
        L = ["# AetherState config — managed by the Console 'Connection' tab.",
             "# Manual edits to the sections below may be overwritten by the Console.", ""]
        L += ["[server]", "cors_origins = " + _json.dumps(cfg.server.cors_origins), ""]
        L += ["[upstream]", f'base_url = "{up.base_url}"', f'api_key = "{up.api_key}"', ""]
        for e in cfg.assist.endpoints:
            L += ["[[assist.endpoints]]", f'name = "{e.name}"', f'base_url = "{e.base_url}"',
                  f'api_key = "{e.api_key}"', f'model = "{e.model}"', f'tier = "{e.tier}"',
                  f"max_concurrent = {int(e.max_concurrent)}", ""]
        g = cfg.assist.groups
        L += ["[assist.groups]", f'extraction = "{g.extraction or cfg.extraction.mode}"',
              f'memory_reflection = "{g.memory_reflection}"', f'embeddings = "{g.embeddings}"',
              f'linter_nli = "{g.linter_nli}"', f'director_selection = "{g.director_selection}"',
              f'lore_gen = "{g.lore_gen}"', ""]
        x = cfg.extraction
        L += ["[extraction]", f"cadence_turns = {int(x.cadence_turns)}",
              f"intake_chars = {int(x.intake_chars)}", f"debounce_s = {float(x.debounce_s)}",
              f'thinking = "{x.thinking}"', ""]
        L += ["[manual_override]", "enabled = " + ("true" if cfg.manual_override.enabled else "false"), ""]
        L += ["[user_guard]", f'name = "{cfg.user_guard.name}"', ""]
        L += ["[consent]", "safewords = " + _json.dumps(cfg.consent.safewords), ""]
        (_P(cfg.server.data_dir) / "config.toml").write_text("\n".join(L), encoding="utf-8")
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
