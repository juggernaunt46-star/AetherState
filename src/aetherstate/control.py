"""Control API core + read-only inspector 'Now' view (07 P2; 10 SS3/SS5 core; 02 SS12b).

Separate router from the relay (09 F3). Every write goes through state.apply_delta with
source='user' — the same authority matrix as OOC commands and the extension panel.
Localhost trust in P2 (12 [ui]: auth_token empty = single-user default)."""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .state import apply_delta, current_state, state_summary, translate_path, validate_op
from . import creator as _creator
from . import narrator as _narrator
from . import hud as _hud
from .extraction import Endpoint

log = logging.getLogger("aetherstate.control")


def _session(store, sid: str):
    return store.db.execute("SELECT * FROM sessions WHERE session_id=? OR external_id=?",
                            (sid, sid)).fetchone()


def _head(store, branch_id: str) -> int:
    row = store.db.execute("SELECT head_turn FROM branches WHERE branch_id=?",
                           (branch_id,)).fetchone()
    return row["head_turn"] if row else -1


def _world_seeded(state: dict) -> bool:
    """True once a world genesis has written entities (faction/location/npc) into state."""
    ents = (state.get("entities") or {}).values()
    return any((e or {}).get("kind") in ("faction", "location", "npc") for e in ents)


def _next_turn(store, branch_id: str) -> int:
    """Turn index for a creator write. Genesis checkpoints turn 0 while head_turn can still be -1
    (no real message yet); state_at replays only journal rows with turn_hi > the latest checkpoint,
    so a creator write must land ONE turn past both the head AND the latest checkpoint or it would
    be shadowed (and invisible to current_state). Fresh session (nothing yet) -> turn 0."""
    head = _head(store, branch_id)
    ck = -1
    try:
        row = store.db.execute("SELECT MAX(turn_index) AS m FROM checkpoints WHERE branch_id=?",
                               (branch_id,)).fetchone()
        if row and row["m"] is not None:
            ck = int(row["m"])
    except Exception:
        ck = -1
    base = max(head, ck)
    return base + 1 if base >= 0 else 0


async def _list_models(get_client, base_url: str, api_key: str = "") -> list[str]:
    """GET {base}/models -> sorted model ids, [] on ANY failure (fail-open; creation-time
    cold path only — never called from the relay's hot path)."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        if get_client is not None:
            r = await get_client().get(base + "/models", headers=headers, timeout=15.0)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(base + "/models", headers=headers)
        if r.status_code >= 400:
            return []
        return sorted([m.get("id") for m in (r.json().get("data") or []) if m.get("id")])
    except Exception as exc:
        log.debug("model list failed open: %s", type(exc).__name__)
        return []


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
        up["model"] = cfg.upstream.model
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
        ge = getattr(cfg.assist, "group_endpoints", None)   # per-group endpoint overrides (Q8):
        gedict = {k: getattr(ge, k) for k in assist["groups"]                # write only when set,
                  if ge is not None and getattr(ge, k, "")} if ge else {}    # so an empty table
        if gedict:                                                           # stays out of the file
            assist["group_endpoints"] = gedict
        else:
            assist.pop("group_endpoints", None)

        # [extraction] (user-tunable subset)
        x = cfg.extraction
        _sec("extraction").update({
            "cadence_turns": int(x.cadence_turns), "intake_chars": int(x.intake_chars),
            "debounce_s": float(x.debounce_s), "thinking": x.thinking})

        _sec("manual_override")["enabled"] = bool(cfg.manual_override.enabled)
        _sec("user_guard")["name"] = cfg.user_guard.name
        _sec("consent")["safewords"] = list(cfg.consent.safewords)
        _sec("specialization")["name"] = cfg.specialization.name

        base.pop("source", None)           # runtime-only load marker; never persist
        header = ("# AetherState config -- the [server]/[upstream]/[assist]/[extraction] keys "
                  "below are\n# managed by the Console. Other sections are preserved on save; "
                  "comments are not.\n")
        # atomic write (2026-07-06): write a sibling temp file then os.replace, so a crash
        # mid-save can never leave a truncated config.toml behind (load would fall back to
        # .bak/defaults and the Console would look mysteriously wiped).
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(header + _toml_dumps(base) + "\n", encoding="utf-8")
        _os.replace(tmp, target)
        if _os.name == "posix":            # POSIX-only; NTFS ACLs differ and chmod is a no-op there
            try:
                _os.chmod(target, 0o600)
            except OSError:
                pass
        elif _os.name == "nt":             # 2026-07-07: NTFS equivalent — strip inherited ACLs,
            try:                            # grant the current user only (the upstream key must
                import subprocess as _sp    # not be world/other-user readable even locally)
                user = _os.environ.get("USERNAME", "")
                if user:
                    _sp.run(["icacls", str(target), "/inheritance:r",
                             "/grant:r", f"{user}:F"],
                            capture_output=True, timeout=10, check=False)
            except Exception:               # fail-open: hardening never blocks a config save
                pass
        return True
    except Exception:
        return False


def make_control_router(cfg, store, jobs=None, pipeline=None) -> APIRouter:
    router = APIRouter(prefix="/aether")

    @router.get("/console")
    async def console():
        """The AetherState Console (Q11 addendum 2; P5 UI base) — same-origin, no CORS."""
        return FileResponse(Path(__file__).parent / "static" / "console.html",
                            media_type="text/html")

    @router.get("/creator")
    async def creator_page():
        """The World Generator + Character Creator window (doc 09). Same-origin, no CORS."""
        return FileResponse(Path(__file__).parent / "static" / "creator.html",
                            media_type="text/html")

    @router.get("/registry")
    async def registry_view():
        """The curated stats/skills/abilities for the creator sheet (cached load, cold-path)."""
        return _creator.registry_export(cfg)

    @router.get("/creator/models")
    async def creator_models():
        """Models detected at the configured endpoints — feeds the Creator's model menu.
        Server-side (no CORS), fail-open: a dead endpoint just contributes an empty list."""
        get_client = jobs.ladder.get_client if jobs is not None else None
        eps = []
        if cfg.upstream.base_url:
            eps.append({"target": "main", "base_url": cfg.upstream.base_url, "default": "",
                        "models": await _list_models(get_client, cfg.upstream.base_url,
                                                     cfg.upstream.api_key)})
        for e in cfg.assist.endpoints:
            eps.append({"target": f"assist:{e.name}", "base_url": e.base_url, "default": e.model,
                        "models": await _list_models(get_client, e.base_url,
                                                     e.api_key or cfg.upstream.api_key)})
        return {"endpoints": eps}

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
    async def genesis_seed(sid: str, request: Request, force: int = 0, ifearly: int = 0):
        """Turn-0 genesis at chat-open (handoff 2026-07-04, REQUIRED). The extension
        posts card/greeting/speaker the moment a chat opens — state is seeded BEFORE
        the first message. Idempotent via the genesis marker; the first-request
        pipeline path stays as fallback for no-extension setups.

        ?force=1 clears the marker first so a session that once seeded empty (the
        pre-fix thinking-model bug marked those 'done' forever) can re-seed without
        starting a new chat. The slash command sends force — explicit user intent.

        ?ifearly=1 (2026-07-06, greeting swipes): only act if the session has no real
        exchange yet (head_turn < 1). The extension sends force=1&ifearly=1 when the
        FIRST message is swiped so the seed reflects the greeting actually chosen —
        but an established chat is never re-seeded by a stray swipe event."""
        from . import genesis as _genesis
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        row = _session(store, sid)
        if not row:
            session_id, _branch = store.create_session(external_id=sid)
            row = _session(store, session_id)
        if ifearly and _head(store, row["active_branch"]) >= 1:
            return {"session_id": row["session_id"], "applied": 0, "scheduled": False,
                    "skipped": "session already has real turns (ifearly)"}
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
        _genesis.seed_player(store, cfg, row["session_id"], row["active_branch"], doc)
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
        payload = {}
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
        try:  # Phase 0a stretch: chat-open prewarm (opt-in, cooldown-limited, fail-open) —
            # the extension already fires this hint at every chat open, so a returning
            # player's first real message hits a warm provider cache with zero UI change.
            if (str(payload.get("event", "")) == "chat_changed" and pipeline is not None
                    and jobs is not None and cfg.upstream.base_url
                    and getattr(cfg.upstream, "prewarm", False)
                    and getattr(cfg.upstream, "cache_key", True)):
                row = _session(store, str(payload.get("session", ""))[:80])
                doc = pipeline.prewarm_doc(row["session_id"]) if row else None
                if doc is not None:
                    import asyncio

                    from . import promptcache as _pc
                    t = asyncio.get_running_loop().create_task(
                        _pc.prewarm(jobs.ladder.get_client, cfg, doc, pipeline.cache))
                    jobs._tasks.add(t)
                    t.add_done_callback(jobs._tasks.discard)
        except Exception:
            pass                              # prewarm is pure bonus — never an error
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
        """05 SS7: assist feature-group mirrors (Q8, live-toggleable) + optional per-group endpoint
        overrides. Payload: {<group>: <mode>, ..., "group_endpoints": {<group>: <endpoint name>}}.
        A blank/unknown endpoint name clears the override (falls open to endpoints[0]). Persisted so
        the routing survives a restart; mode-only callers keep working unchanged."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        valid_modes = {"off", "rules", "main", "assist"}
        names = {e.name for e in cfg.assist.endpoints}
        out, eps_out = {}, {}
        for group, mode in payload.items():
            if group == "group_endpoints":
                continue
            if hasattr(cfg.assist.groups, group) and mode in valid_modes:
                setattr(cfg.assist.groups, group, mode)
                out[group] = mode
        ge = getattr(cfg.assist, "group_endpoints", None)
        for group, name in (payload.get("group_endpoints") or {}).items():
            if ge is not None and hasattr(ge, group):
                name = str(name or "").strip()
                setattr(ge, group, name if name in names else "")   # blank/unknown -> clear
                eps_out[group] = getattr(ge, group)
        persisted = _persist_config(cfg)
        return {"applied": out, "endpoints": eps_out, "persisted": persisted}

    @router.post("/assist/endpoints")
    async def set_assist_endpoints(request: Request):
        """Replace the [[assist.endpoints]] list (the Console's multi-endpoint editor). Each item:
        {name, base_url, model?, tier?, api_key?, max_concurrent?}. A blank api_key on an endpoint
        whose name already exists KEEPS the stored key (never echoed back). Persisted."""
        from .config import AssistEndpointConfig
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        incoming = payload.get("endpoints")
        if not isinstance(incoming, list):
            return JSONResponse({"error": "endpoints list required"}, status_code=400)
        old = {e.name: e for e in cfg.assist.endpoints}
        new_eps, seen = [], set()
        for item in incoming:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            base = str(item.get("base_url", "")).strip()
            if not name or name in seen or not base:
                continue                       # a usable endpoint needs a unique name + a url
            seen.add(name)
            key = str(item.get("api_key", "") or "")
            if not key and name in old:
                key = old[name].api_key         # blank keeps the stored key (never wiped by edits)
            try:
                maxc = max(1, int(item.get("max_concurrent", 1)))
            except (TypeError, ValueError):
                maxc = 1
            new_eps.append(AssistEndpointConfig(
                name=name, base_url=base, api_key=key,
                model=str(item.get("model", "")).strip(),
                tier=str(item.get("tier", "small")).strip() or "small", max_concurrent=maxc))
        cfg.assist.endpoints = new_eps
        # drop any group override that now points at a deleted endpoint (fail-open to default)
        ge = getattr(cfg.assist, "group_endpoints", None)
        if ge is not None:
            for grp in cfg.assist.groups.model_dump():
                if getattr(ge, grp, "") and getattr(ge, grp) not in seen:
                    setattr(ge, grp, "")
        persisted = _persist_config(cfg)
        return {"endpoints": [{"name": e.name, "base_url": e.base_url, "model": e.model,
                               "tier": e.tier, "has_key": bool(e.api_key),
                               "max_concurrent": e.max_concurrent} for e in new_eps],
                "persisted": persisted}

    # the RPG knobs a human flips live from the Console / ST panel (bools + the contract enum).
    _SPEC_BOOL_KNOBS = ("intent_floor", "war_room", "enemy_rolls", "auto_dm_checks", "foe_floor",
                        "stealth_kills", "living_world", "hardcore", "dm_guard",
                        "auto_compact_contract", "large_battle")

    def _spec_view() -> dict:
        s = cfg.specialization
        view = {"name": s.name, "blocks": list(s.blocks), "dice": s.dice, "tiers": s.tiers,
                "contract": getattr(s, "contract", "full")}
        for k in _SPEC_BOOL_KNOBS:
            view[k] = bool(getattr(s, k, k != "hardcore"))
        return view

    @router.get("/specialization")
    async def specialization_get():
        return _spec_view()

    @router.post("/specialization")
    async def specialization_set(request: Request):
        """Q27 / doc 05: switch narrative specialization (none|rpg) AND its knobs at runtime. Live
        for rendering + the DM guard immediately (compose/tier0 read cfg per request); persisted so
        the profile overlay fully re-applies on next load. `name` is optional now — a knob-only
        POST (e.g. {"intent_floor": false}) flips just that switch and leaves the mode alone."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if "name" in payload:
            name = str(payload.get("name", "")).strip().lower()
            if name not in ("none", "rpg"):
                return JSONResponse({"error": "name must be none|rpg"}, status_code=422)
            cfg.specialization.name = name
            log.info("specialization set: %s", name)
        for k in _SPEC_BOOL_KNOBS:                       # bool knobs — the visible opt-in switches
            if k in payload:
                setattr(cfg.specialization, k, bool(payload[k]))
                log.info("specialization %s = %s", k, bool(payload[k]))
        if "contract" in payload:
            c = str(payload.get("contract", "")).strip().lower()
            if c in ("full", "compact"):
                cfg.specialization.contract = c
        return {**_spec_view(), "persisted": _persist_config(cfg)}

    def _connection_view() -> dict:
        return {"upstream": {"base_url": cfg.upstream.base_url,
                             "has_key": bool(cfg.upstream.api_key),
                             "model": cfg.upstream.model},
                "assist": [{"name": e.name, "base_url": e.base_url, "has_key": bool(e.api_key),
                            "model": e.model, "tier": e.tier} for e in cfg.assist.endpoints]}

    @router.get("/connection")
    async def connection_get():
        return _connection_view()

    @router.post("/connection/models")
    async def connection_models(request: Request):
        """List models from an endpoint, server-side (no CORS, uses the truststore/Avast fix).

        2026-07-06: when the key field is blank the SAVED key for `target` is used — the
        Console leaves saved keys out of the DOM, so 'Connect / test' with a blank field
        used to run the auth check keyless and toast a false 'KEY REJECTED'."""
        try:
            p = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        base = str(p.get("base_url", "")).strip().rstrip("/")
        key = str(p.get("api_key", ""))
        if not key:                          # fall back to the saved key for this target
            target = str(p.get("target", ""))
            if target == "upstream":
                key = cfg.upstream.api_key
            elif target == "assist" and cfg.assist.endpoints:
                key = cfg.assist.endpoints[0].api_key or cfg.upstream.api_key
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
            if "model" in p:                 # default for engine-initiated calls (creator/genesis)
                cfg.upstream.model = str(p["model"]).strip()
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
        return {"ok": True, "persisted": _persist_config(cfg), **_connection_view()}

    @router.get("/sessions")
    async def sessions():
        rows = store.db.execute(
            "SELECT s.session_id, s.external_id, s.label, s.frontend, s.frozen, s.last_seen,"
            " s.active_branch, b.head_turn FROM sessions s"
            " LEFT JOIN branches b ON b.branch_id=s.active_branch"
            " ORDER BY s.last_seen DESC").fetchall()
        out = []
        for r in rows:                              # enrich with the committed world + player
            d = dict(r)                             # names so the Creator's session picker is
            d["world_name"], d["player_name"] = "", ""   # LEGIBLE (2026-07-08: cryptic st-ids
            try:                                    # gave no clue which world a session was)
                st = current_state(store, r["active_branch"])
                players = st.get("player") or {}
                pkey = next(iter(players), None)
                if pkey:
                    d["player_name"] = ((st.get("entities", {}).get(pkey, {}) or {})
                                        .get("name") or pkey or "")
                if _world_seeded(st):
                    d["world_name"] = _creator.world_from_state(st).get("name") or ""
            except Exception:
                pass                                # fail-open: a legible-name miss is cosmetic
            out.append(d)
        return {"sessions": out}

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

    @router.get("/session/{sid}/hud")
    async def hud_now(sid: str):
        """The resolved player-facing HUD payload (registry math done server-side): scene,
        player card(s) with effective skill mods + resolved abilities, statuses/conditions,
        drives, gear (worn) + inventory (carried), quests, dice rolls/checks, relations/
        factions. The ONE source both the SillyTavern HUD and the Console render, so they
        never diverge. Read-only, fail-open."""
        row = _session(store, sid)
        if not row:
            return {"session_id": None, "spec": getattr(getattr(cfg, "specialization", None),
                                                        "name", "none"),
                    "players": [], "scene": {}, "quests": [], "rolls": [],
                    "relations": [], "factions": [], "world_flags": {}}
        state = current_state(store, row["active_branch"])
        view = _hud.hud_view(state, cfg)
        view["session_id"] = row["session_id"]
        view["external_id"] = row["external_id"]
        view["head_turn"] = _head(store, row["active_branch"])
        try:    # 2026-07-10 (Eranmor, pillar 17): tier0 notices — "recharging", non-moves,
            #   unknown skills, re-served rolls — used to die in the proxy log; the HUD
            #   rolls lane now shows them. Transient ring (a restart clears it), fail-open.
            view["notices"] = (pipeline.recent_notices(row["session_id"])
                               if pipeline is not None else [])
        except Exception:
            view["notices"] = []
        return view

    @router.post("/session/{sid}/freeze")
    async def freeze(sid: str):
        return _user_ops(sid, [{"op": "freeze", "reason": "user"}])

    @router.post("/session/{sid}/unfreeze")
    async def unfreeze(sid: str):   # unfreeze is user-only by design (02 SS6)
        return _user_ops(sid, [{"op": "unfreeze"}])

    @router.get("/session/{sid}/creator")
    async def creator_prefill(sid: str):
        """Prefill the creator window: current Player Card + the committed world doc (best
        effort), whether a world was seeded, the active specialization, and the user persona
        name (the default player name). 2026-07-06: `world` + `player_name` added so the
        window can SHOW what is actually set (review tab / load-into-form)."""
        spec = getattr(getattr(cfg, "specialization", None), "name", "none")
        persona = getattr(getattr(cfg, "user_guard", None), "name", "") or ""
        row = _session(store, sid)
        if not row:
            return {"session_id": None, "specialization": spec, "persona": persona,
                    "player": None, "player_name": "", "world": None, "world_seeded": False}
        state = current_state(store, row["active_branch"])
        players = state.get("player") or {}
        pkey = next(iter(players), None)
        seeded = _world_seeded(state)
        pcard = players.get(pkey) if pkey else None
        if pcard is not None:                    # appearance lives in attributes, not the card —
            attrs = (state.get("attributes", {}) or {}).get(pkey, {}) or {}
            if attrs.get("appearance") and "appearance" not in pcard:
                pcard = {**pcard, "appearance": attrs.get("appearance")}
        return {"session_id": row["session_id"], "specialization": spec, "persona": persona,
                "player": pcard,
                "player_name": (state.get("entities", {}).get(pkey, {}) or {}).get("name")
                               or (pkey or ""),
                "world": _creator.world_from_state(state) if seeded else None,
                "world_seeded": seeded}

    # ---- creator presets (2026-07-06): named world/player docs, reusable across sessions
    @router.get("/presets")
    async def presets_list():
        return {"presets": store.preset_list()}

    @router.post("/presets")
    async def presets_save(request: Request):
        try:
            p = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        kind = str(p.get("kind", "")).lower()
        name = str(p.get("name", "")).strip()
        doc = p.get("doc") if isinstance(p.get("doc"), dict) else None
        if kind not in ("world", "player") or not name or doc is None:
            return JSONResponse({"error": "need kind=world|player, name, doc"},
                                status_code=422)
        pid = store.preset_save(kind, name, doc)
        return {"preset_id": pid, "kind": kind, "name": name}

    @router.get("/presets/{pid}")
    async def presets_get(pid: int):
        p = store.preset_get(pid)
        if not p:
            return JSONResponse({"error": "unknown preset"}, status_code=404)
        return p

    @router.delete("/presets/{pid}")
    async def presets_delete(pid: int):
        store.preset_delete(pid)
        return {"deleted": pid}

    @router.post("/session/{sid}/author")
    async def creator_author(sid: str, request: Request):
        """Assist-LLM 'fill the blanks' authoring (doc 09 §1). Cold-path, creation-time; returns
        the completed doc for the window to review (NOT yet persisted). Fail-open: with no assist
        endpoint it returns the deterministic fill, so the creator always works."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        mode = str(payload.get("mode", "world")).lower()
        seed = payload.get("doc") if isinstance(payload.get("doc"), dict) else {}
        world = payload.get("world") if isinstance(payload.get("world"), dict) else None
        if payload.get("offline"):             # explicit template fill — never an LLM call
            if mode != "world":                # ranks on genre-pack ids must freeze into defs
                seed = _creator._inject_pack_defs(seed, _creator._pack_for(world))
            doc = (_creator.deterministic_world(seed) if mode == "world"
                   else _creator.deterministic_player(seed, cfg))
            return {"source": "deterministic", "mode": mode, "doc": doc,
                    "detail": "template fill (offline, by request)"}
        row = _session(store, sid)
        ep = None
        if jobs is not None and row is not None:
            try:
                ep, _, _ = jobs.endpoint_for(row["session_id"])
            except Exception:
                ep = None
        if ep is None and jobs is not None:
            # creator-first flow: no session yet — build from config so the AI fill works
            # before the first chat message ever flows through the proxy
            if cfg.assist.endpoints:
                e = cfg.assist.endpoints[0]
                ep = Endpoint(base_url=e.base_url, model=e.model, api_key=e.api_key,
                              assist_tier=e.tier in ("nano", "small"))
            elif cfg.upstream.base_url:
                ep = Endpoint(base_url=cfg.upstream.base_url, model="")
        want = str(payload.get("model") or "").strip()
        if ep is not None and want:
            ep.model = want                    # explicit pick from the Creator's model menu
        if ep is not None and jobs is not None and not ep.model:
            # nothing proxied yet on this session -> resolve a real model (assist pick >
            # upstream.model default > GET /models detection), same ladder genesis uses
            from .assist import resolve_endpoint
            ep = await resolve_endpoint(jobs.ladder.get_client, cfg, ep)
        if ep is None or jobs is None or not ep.model:
            # No model to call: honest error — the window offers templates as an explicit
            # button instead of silently swapping them in (2026-07-06).
            return {"source": "error", "mode": mode,
                    "detail": "no model reachable — pick one in the Creator menu or "
                              "configure an endpoint in Console → Connection"}
        try:
            if mode in ("player", "character"):
                out = await _creator.author_player(jobs.ladder.get_client, cfg, ep, seed, world)
            else:
                out = await _creator.author_world(jobs.ladder.get_client, cfg, ep, seed)
        except Exception as exc:
            out = {"source": "error", "detail": f"authoring failed: {type(exc).__name__}"}
        out["mode"] = mode
        out["model"] = ep.model
        return out

    @router.post("/session/{sid}/world")
    async def creator_world(sid: str, request: Request):
        """Persist a finalized world doc as shipped ops (entities / memory-lore / scene). The
        world is authored FIRST; ops apply with source='user' (privileged entity creation)."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        world = payload.get("world") if isinstance(payload.get("world"), dict) else payload
        ops = _creator.world_to_ops(world or {})
        res = _creator_apply(sid, ops)
        if isinstance(res, dict):
            res["ops"] = len(ops)
        return res

    @router.post("/session/{sid}/player")
    async def creator_player(sid: str, request: Request):
        """Persist a finalized Player Card as [entity_add, player_seed, set_attribute...]. Mirrors
        genesis seed_player; ops apply with source='user' (privileged player card)."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        player = payload.get("player") if isinstance(payload.get("player"), dict) else payload
        ops = _creator.player_to_ops(player or {}, cfg)
        res = _creator_apply(sid, ops)
        if isinstance(res, dict):
            res["ops"] = len(ops)
        return res

    @router.post("/session/{sid}/seed")
    async def creator_seed(sid: str, request: Request):
        """Auto-seed a session from a Narrator card's embedded seed (the ST extension calls this
        on chat-open when the card carries extensions.aetherstate.seed). IDEMPOTENT by design:
        commits the world only when none is seeded yet, and the Player Card only when none
        exists — so re-opening an established chat never clobbers progress (player XP, gained
        items, moved scenes). Deterministic, no LLM (weak-model floor); privileged source='user'
        through the same validated world_to_ops/player_to_ops apply path. Fail-open."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        seed = payload.get("seed") if isinstance(payload.get("seed"), dict) else payload
        world = seed.get("world") if isinstance(seed.get("world"), dict) else None
        player = seed.get("player") if isinstance(seed.get("player"), dict) else None
        row = _session(store, sid)
        if not row:
            try:                                    # creator-first: mint the row the chat's first
                row = store.get_or_create_session(sid)   # stamped message will adopt (converges)
            except Exception:
                return JSONResponse({"error": "unknown session"}, status_code=404)
        state = current_state(store, row["active_branch"])
        did_world = bool(world and world.get("name")) and not _world_seeded(state)
        did_player = bool(player) and not (state.get("player") or {})
        ops: list = []
        if did_world:
            ops += _creator.world_to_ops(world)
        if did_player:
            ops += _creator.player_to_ops(player, cfg)
        applied = 0
        if ops:
            branch = row["active_branch"]
            turn = _next_turn(store, branch)
            res = apply_delta(store, row["session_id"], branch, turn, ops, "user", cfg)
            applied = len(res.applied)
        return {"session_id": row["session_id"], "world_seeded": did_world,
                "player_seeded": did_player, "applied": applied}

    @router.patch("/session/{sid}/state")
    async def patch_state(sid: str, request: Request):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        ops = []
        rejected = []
        if "path" in payload:
            rpg = getattr(cfg, "specialization", None) is not None \
                and cfg.specialization.name == "rpg"     # RPG-3b paths ride only under rpg
            op = translate_path(str(payload["path"]), str(payload.get("value", "")), rpg=rpg)
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

    @router.get("/session/{sid}/briefing")
    async def session_briefing(sid: str):
        """EXACTLY what the engine would inject into the next request (2026-07-09, Bean:
        'the console shouldn't hide anything raw'): the composed state header, the DM
        rules-contract when rpg, and per-component token counts after the budget governor.
        Read-only; the per-request extras (guard line, memories, director note) are noted
        but not fabricated here."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        from . import compose as _compose
        from . import prompts as _prompts
        try:
            state = current_state(store, row["active_branch"])
            header = _compose.render_header(state, cfg)
            comps = [_compose.Component("state_header", header,
                                        cfg.injection.priorities.get("state_header", 100))
                     ] if header else []
            rpg = getattr(cfg, "specialization", None) is not None \
                and cfg.specialization.name == "rpg"
            # the next request composes at last-committed-turn + 1. Derive it from the REPLAYED
            # state (meta.turn is authoritative), not the branches.head_turn cache — they agree
            # live, but only meta.turn is set when state is reconstructed from the journal.
            upcoming = int((state.get("meta") or {}).get("turn", -1)) + 1
            contract_variant = "full"
            if rpg:
                # A1: mirror compose.compose's per-turn contract choice so /briefing reports the
                # REAL size the NEXT request will carry. The next request composes at head+1, so
                # evaluate the calm/established + combat decision at that upcoming turn (not the
                # committed head) — otherwise the inspector under-reports a compact flip.
                dstate = dict(state)
                dstate["meta"] = {**(state.get("meta") or {}), "turn": upcoming}
                auto = _compose._auto_compact_contract(dstate, cfg)
                contract_variant = "compact" if auto else "full"
                comps.append(_compose.Component(
                    "rules_contract", _prompts.rules_contract(cfg, force_compact=auto),
                    cfg.injection.priorities.get("rules_contract", 30)))
            kept = _compose.govern(list(comps), cfg)
            return {"session_id": row["session_id"], "turn": _head(store, row["active_branch"]),
                    "upcoming_turn": upcoming, "contract_variant": contract_variant,
                    "spec": cfg.specialization.name if rpg else "none",
                    "components": [{"cls": c.cls, "tokens": c.tokens, "text": c.text}
                                   for c in comps],
                    "kept_after_budget": [{"cls": c.cls, "tokens": c.tokens} for c in kept],
                    "budget_tokens": cfg.injection.max_tokens,
                    "note": "per-request extras (guard line, recalled memories, director "
                            "notes) ride on top of this at send time"}
        except Exception as exc:
            return JSONResponse({"error": f"briefing render failed: {type(exc).__name__}"},
                                status_code=500)

    @router.get("/session/{sid}/journal")
    async def session_journal(sid: str, limit: int = 40):
        """RPG-4 inspector feed (doc 05 §9: visible state-change feedback): the tail of the
        ops journal, newest first, one row per applied op — turn, source, op kind and its
        salient fields. Read-only; never rewrites anything."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        import json as _json
        lim = max(1, min(int(limit), 200))
        rows = store.db.execute(
            "SELECT turn_hi, source, ops, ts FROM ops_journal WHERE branch_id=?"
            " ORDER BY rowid DESC LIMIT ?", (row["active_branch"], lim)).fetchall()
        entries = []
        for r in rows:
            try:
                ops = _json.loads(r["ops"])
            except Exception:
                continue
            for op in reversed(ops if isinstance(ops, list) else []):
                if not isinstance(op, dict):
                    continue
                brief = {k: op[k] for k in (
                    "name", "entity", "char", "kind", "skill", "tier", "effect", "location",
                    "to_location", "front", "template", "item", "slot", "target", "key",
                    "value", "delta", "present", "text", "statement", "reason", "phase",
                    "ability", "valence", "quest", "status", "note", "outcome", "amount",
                    "qty") if k in op}
                for tk in ("text", "statement", "reason"):   # reveal_fact rows were empty {}
                    if isinstance(brief.get(tk), str) and len(brief[tk]) > 80:
                        brief[tk] = brief[tk][:77] + "..."
                entries.append({"turn": r["turn_hi"], "source": r["source"],
                                "op": op.get("op"), "brief": brief})
                if len(entries) >= lim:
                    break
            if len(entries) >= lim:
                break
        return {"entries": entries, "rolls": (current_state(store, row["active_branch"])
                                              .get("rolls") or [])[-10:]}

    @router.get("/session/{sid}/search")
    async def session_search(sid: str, q: str = "", limit: int = 8):
        """RPG-5: search over the session's memory/summary ledger (the deferred
        AI-search-over-summaries hook, doc 10). Uses the same composite scorer recall
        uses (lexical BM25-ish + importance + recency; embeddings when the assist tier
        has them staged). Read-only, cold-path, fail-open to []."""
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        if not q.strip():
            return {"query": q, "hits": []}
        try:
            from . import memory as _memory
            branch = row["active_branch"]
            state = current_state(store, branch)
            now = state.get("meta", {}).get("turn", -1)
            rows = _memory.retrieve(store, cfg, branch, state, q.strip(), max(0, now))
            hits = [{"text": r["text"], "turn": r["created_turn"],
                     "importance": r["importance"],
                     "when": _memory.when_phrase(max(0, now - r["created_turn"]))}
                    for r in rows[:max(1, min(int(limit), 25))]]
            return {"query": q, "hits": hits}
        except Exception as exc:
            log.warning("session search failed open: %s", type(exc).__name__)
            return {"query": q, "hits": []}

    # ---- world-specific Narrator card (2026-07-07): make the built world VISIBLE in the
    # frontend. Projects committed world (creator.world_from_state) + Player Card into a V2
    # SillyTavern card. Read-only; never touches the stream; a `none` session's wire is
    # unaffected (control routes are off the relay).
    def _narrator_sources(sid: str):
        """(world_doc, player_card) from committed state — fail-open to ({}, None)."""
        row = _session(store, sid)
        if not row:
            return {}, None
        try:
            state = current_state(store, row["active_branch"])
        except Exception:
            return {}, None
        try:
            world = _creator.world_from_state(state)
        except Exception:
            world = {}
        players = state.get("player") or {}
        pkey = next(iter(players), None)
        player = None
        if pkey:
            card = dict(players.get(pkey) or {})
            nm = (state.get("entities", {}).get(pkey, {}) or {}).get("name") or pkey
            card.setdefault("name", nm)
            attrs = (state.get("attributes", {}) or {}).get(pkey, {}) or {}
            if attrs.get("appearance"):
                card.setdefault("appearance", attrs.get("appearance"))
            player = card
        return world, player

    def _card_filename(name: str) -> str:
        keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in (name or "Narrator"))
        return (keep.strip() or "Narrator")[:48] + ".png"

    def _install_card(png: bytes, fname: str):
        """Best-effort install into [specialization].narrator_card_dir (fail-open). Returns
        (installed_path, error) — empty dir config = download-only, never writes out."""
        target = str(getattr(getattr(cfg, "specialization", None),
                             "narrator_card_dir", "") or "").strip()
        if not target:
            return "", ""
        try:
            d = Path(target).expanduser()
            if not d.is_dir():
                return "", "narrator_card_dir is not a directory"
            (d / fname).write_bytes(png)
            return str(d / fname), ""
        except Exception as exc:
            return "", f"install failed: {type(exc).__name__}"

    @router.post("/narrator-card")
    async def narrator_card_build(request: Request):
        """Session-free Narrator card: build straight from POSTed world+player docs (the Creator
        form) — NO committed session required (2026-07-08: card creation was coupled to first
        applying the world to a blank session). The card carries a structured seed so a fresh
        chat auto-commits the ledger (extension -> /session/{sid}/seed). Installs into
        [specialization].narrator_card_dir when set; always returns the PNG (base64) to download."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        world = payload.get("world") if isinstance(payload.get("world"), dict) else {}
        player = payload.get("player") if isinstance(payload.get("player"), dict) else None
        card = _narrator.build_card(world, player)
        png = _narrator.card_png(card, world)
        name = card["data"]["name"]
        fname = _card_filename(name)
        installed, err = _install_card(png, fname)
        seed = card["data"]["extensions"]["aetherstate"].get("seed", {})
        return {"name": name, "world": str((world or {}).get("name") or ""),
                "bytes": len(png), "installed": installed, "error": err,
                "filename": fname, "tags": card["data"]["tags"],
                "seeded_world": bool(seed.get("world", {}).get("name")),
                "seeded_player": bool(seed.get("player")),
                "png_b64": base64.b64encode(png).decode("ascii")}

    @router.get("/session/{sid}/narrator-card.png")
    async def narrator_card_png(sid: str):
        """Downloadable world Narrator card (PNG with the V2 chara card embedded in a tEXt
        chunk — SillyTavern's import format), built from this session's committed world."""
        world, player = _narrator_sources(sid)
        card = _narrator.build_card(world, player)
        png = _narrator.card_png(card, world)
        fname = _card_filename(card["data"]["name"])
        return Response(content=png, media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @router.get("/session/{sid}/narrator-card.json")
    async def narrator_card_json(sid: str):
        """The V2 chara card JSON for this session's world (inspect / manual import)."""
        world, player = _narrator_sources(sid)
        return _narrator.build_card(world, player)

    @router.post("/session/{sid}/narrator-card")
    async def narrator_card_install(sid: str):
        """Build the world Narrator card and, when [specialization].narrator_card_dir is a real
        directory, install the PNG there so it appears in SillyTavern's character list. Always
        returns metadata + a download URL; the install is best-effort and fail-open."""
        world, player = _narrator_sources(sid)
        card = _narrator.build_card(world, player)
        png = _narrator.card_png(card, world)
        name = card["data"]["name"]
        fname = _card_filename(name)
        installed, err = _install_card(png, fname)
        return {"name": name, "world": str((world or {}).get("name") or ""),
                "bytes": len(png), "installed": installed, "error": err,
                "filename": fname, "tags": card["data"]["tags"],
                "png_b64": base64.b64encode(png).decode("ascii"),
                "download": f"/aether/session/{sid}/narrator-card.png"}

    def _creator_apply(sid: str, ops: list):
        """Apply creator (world/player) ops at the next free turn so they survive the genesis
        checkpoint shadow (see _next_turn). Privileged source='user'.

        2026-07-06 creator-first flow (live repro: 'Save failed: HTTP 404'): a brand-new chat
        has no session row until its FIRST message flows through the relay, so saving a world
        or character built creator-first bounced with 404. On a miss we now mint the session
        by external id — the exact row session_engine._session_for_external adopts when the
        chat's first stamped message arrives, so the creator save and the chat converge on
        one session. Authoring got this fix in the QoL sweep; this is the save-side half."""
        row = _session(store, sid)
        if not row:
            try:
                row = store.get_or_create_session(sid)
                log.info("creator save minted session %s for creator-first external id %r",
                         row["session_id"][:8], sid)
            except Exception:
                return JSONResponse({"error": "unknown session"}, status_code=404)
        branch = row["active_branch"]
        turn = _next_turn(store, branch)
        res = apply_delta(store, row["session_id"], branch, turn, ops, "user", cfg)
        rejected = [{"op": q["op"].get("op"), "reason": q["reason"]} for q in res.quarantined]
        return {"applied": len(res.applied), "rejected": rejected,
                "frozen": bool(res.state.get("frozen")), "turn": turn,
                "session_id": row["session_id"]}

    def _user_ops(sid: str, ops: list, extra_rejected: list | None = None):
        row = _session(store, sid)
        if not row:
            return JSONResponse({"error": "unknown session"}, status_code=404)
        turn = _head(store, row["active_branch"])
        try:
            ckrow = store.db.execute(
                "SELECT MAX(turn_index) AS m FROM checkpoints WHERE branch_id=?",
                (row["active_branch"],)).fetchone()
            ck = int(ckrow["m"]) if ckrow and ckrow["m"] is not None else -1
        except Exception:
            ck = -1
        if ck > turn:
            # Pre-first-message session: genesis/creator checkpointed AHEAD of head, so an op
            # journaled at head would be shadowed by state_at's `turn_hi > checkpoint` replay
            # (2026-07-06 live repro: a Console/PATCH edit reported applied=1 yet was invisible).
            # Land it one turn past the horizon — the same rule as _next_turn.
            turn = ck + 1
        res = apply_delta(store, row["session_id"], row["active_branch"],
                          turn, ops, "user", cfg)
        rejected = (extra_rejected or []) + [
            {"op": q["op"].get("op"), "reason": q["reason"]} for q in res.quarantined]
        return {"applied": len(res.applied), "rejected": rejected,
                "frozen": bool(res.state.get("frozen"))}

    return router
