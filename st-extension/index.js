/* AetherState Companion — thin SillyTavern extension (deliverable 05).
 * Responsibilities (05 §1): stamp identity (header L1 + sentinel L2), capture gen type,
 * quick panel (status chip, freeze/resume, override toggle, console link), slash cmds.
 * Everything is fail-open: if the proxy is down or this file errors, ST works untouched.
 * The proxy strips every <<AETHER:...>> sentinel — it can never reach the model (stamps.py).
 */
(() => {
  "use strict";
  const MODULE = "aetherstate";
  let ctx = null;
  try { ctx = SillyTavern.getContext(); } catch (e) { console.warn("[AetherState] no ST context", e); return; }
  console.log("[AetherState] Companion loaded — cadence + sentinel-priority build (2026-07-04e)");
  // ST reassigns chatMetadata/characterId on chat/char switch, so a context captured once
  // goes stale. C() always returns the CURRENT context for per-chat/character reads.
  const C = () => { try { return SillyTavern.getContext() || ctx; } catch (e) { return ctx; } };

  const defaults = {
    enabled: true,
    proxy_url: "http://127.0.0.1:9130",
    stamp: { header: true, sentinel: true },
    guard: { override_name: null },
    panel: { show_status_chip: true },
  };
  const settings = Object.assign({}, defaults, ctx.extensionSettings[MODULE]);
  ctx.extensionSettings[MODULE] = settings;
  const save = () => { try { ctx.saveSettingsDebounced(); } catch (e) {} };

  // ---- identity (05 §4.1): stable per-chat id + volatile per-request fields
  let lastGenType = "normal";
  let turnCounter = 0;
  const sid = () => {
    try {
      const c = C();
      const meta = c.chatMetadata || {};
      if (!meta.aetherstate_sid) {
        meta.aetherstate_sid = "st-" + Math.random().toString(36).slice(2, 12);
        try { c.saveMetadataDebounced(); } catch (e) {}
      }
      return meta.aetherstate_sid;
    } catch (e) { return "st-unknown"; }
  };
  const guardName = () => {
    try { return settings.guard.override_name || C().substituteParams("{{user}}"); }
    catch (e) { return ""; }
  };
  const speaker = () => {
    try { const c = C(); return c.characters?.[c.characterId]?.name || ""; } catch (e) { return ""; }
  };
  const sentinel = () =>
    `<<AETHER:v=1;session=${sid()};turn=${turnCounter};type=${lastGenType};` +
    `speaker=${speaker()};user=${guardName()}>>`;

  // ---- header write (05 §4.2): stable id only, Custom source only, on CHAT_CHANGED.
  // oai_settings is stable-but-undocumented — everything in try/catch; sentinel (L2)
  // carries identity whenever this fails or the source isn't Custom.
  function stampHeader() {
    try {
      if (!settings.enabled || !settings.stamp.header) return;
      // 2026-07-04: try every known accessor — if none works the header line saved in
      // ST's settings can go STALE; the proxy now lets the per-request sentinel win on
      // mismatch, so a stale header can no longer steal turns into an old session.
      const oai = C().chatCompletionSettings || ctx.oaiSettings || globalThis.oai_settings;
      if (!oai || oai.chat_completion_source !== "custom") return;
      const lines = String(oai.custom_include_headers || "").split("\n")
        .filter((l) => l.trim() && !/^x-aetherstate-session\s*:/i.test(l));
      lines.push(`x-aetherstate-session: ${sid()}`);
      oai.custom_include_headers = lines.join("\n");
      save();
    } catch (e) { /* L2 covers L1 (05 §4.2) */ }
  }

  // ---- turn-0 genesis (handoff 2026-07-04, REQUIRED): the greeting renders with NO
  // request, so at chat-open we hand the proxy the card ourselves. Fire-and-forget;
  // the proxy's genesis marker makes re-opens no-ops. First-request path = fallback.
  async function doGenesis(reason, force = false) {
    if (!settings.enabled) return { error: "extension disabled" };
    const sub = (t) => { try { return C().substituteParams(t || ""); } catch (e) { return t || ""; } };
    let ch = null, cx = C();                                // CHAT_CHANGED can fire before the
    for (let i = 0; i < 8 && !ch; i++) {                    // character is loaded — retry briefly
      cx = C(); ch = cx.characters?.[cx.characterId];
      if (!ch) await new Promise((r) => setTimeout(r, 250));
    }
    if (!ch) { console.warn("[AetherState] genesis: no active character"); return { error: "no character" }; }
    const card = [sub(ch.description), sub(ch.personality), sub(ch.scenario), sub(ch.mes_example)]
      .filter(Boolean).join("\n").trim();
    let greeting = sub(ch.first_mes || "");
    if (!greeting) {                                        // fall back to the greeting shown in chat
      try { const m = (cx.chat || []).find((x) => !x.is_user && x.mes); if (m) greeting = sub(m.mes); } catch (e) {}
    }
    if (!card && !greeting) { console.warn("[AetherState] genesis: empty card+greeting"); return { error: "empty card" }; }
    const S = sid();
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 8000);        // seeding is fast (Stage B is async)
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/genesis${force ? "?force=1" : ""}`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ card, greeting, speaker: ch.name || "", user: guardName(), opening: "" }),
      });
      clearTimeout(t);
      const d = await r.json().catch(() => ({}));
      console.log(`[AetherState] genesis (${reason}) sid=${S} ->`, d);
      try { refreshChip(); } catch (e) {}
      return d;
    } catch (e) { console.warn("[AetherState] genesis fetch failed", e); return { error: String(e) }; }
  }
  function genesisAtChatOpen() { doGenesis("chat_open").catch(() => {}); }

  // ---- fire-and-forget hints (05 §5): 2 s timeout, silent — proxy never depends on them
  function hint(event, messageIndex = -1) {
    try {
      const ac = new AbortController();
      setTimeout(() => ac.abort(), 2000);
      fetch(settings.proxy_url.replace(/\/$/, "") + "/aether/hint", {
        method: "POST", headers: { "content-type": "application/json" },
        signal: ac.signal,
        body: JSON.stringify({ event, session: sid(), messageIndex }),
      }).catch(() => {});
    } catch (e) {}
  }

  // ---- gen-type capture (05 §4.4): interceptor runs before prompt build, skips dry runs
  globalThis.aetherstateInterceptor = async (chat, contextSize, abort, type) => {
    lastGenType = type || "normal";        // "swipe" | "regenerate" | "impersonate" | ...
  };

  // ---- sentinel injection (05 §4.3): CHAT_COMPLETION_PROMPT_READY, never on dry runs
  try {
    const ev = ctx.eventTypes || ctx.event_types;
    const on = (name, fn) => { if (ev?.[name]) ctx.eventSource.on(ev[name], fn); };
    on("CHAT_COMPLETION_PROMPT_READY", (data) => {          // 05 §4.3
      try {
        if (data?.dryRun) return;
        if (!settings.enabled || !settings.stamp.sentinel) return;
        data.chat.unshift({ role: "system", content: sentinel() });
      } catch (e) { /* fail-open: header or LCP fallback still identifies (03 §2) */ }
    });
    on("CHAT_CHANGED", () => {                              // 05 §5
      turnCounter = 0; stampHeader(); hint("chat_changed"); refreshChip();
      genesisAtChatOpen();                                  // turn-0 seed (proxy idempotent)
    });
    on("GENERATION_STARTED", (type, opts, dryRun) => {
      try {
        if (dryRun) return;
        if (type) lastGenType = type;                       // fallback capture
        if (!type || type === "normal" || type === "continue") turnCounter++;
      } catch (e) {}
    });
    on("MESSAGE_SWIPED", (i) => hint("swipe", Number(i)));
    on("MESSAGE_EDITED", (i) => hint("edit", Number(i)));
    on("MESSAGE_DELETED", (i) => hint("delete", Number(i)));
    on("MESSAGE_RECEIVED", () => { refreshChip(); lastGen = Date.now(); });
  } catch (e) { console.warn("[AetherState] event wiring unavailable", e); }

  // ---- quick panel (05 §7)
  let lastGen = 0;
  const api = async (path, opts = {}) => {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 2000);           // 05 §9: 2 s, silent degrade
    try {
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + path,
                            { ...opts, signal: ac.signal });
      return await r.json();
    } finally { clearTimeout(t); }
  };

  // ---- native writeback loop (05 §6): v1 applies the chat-metadata patch; WI/AN
  // arrive empty until the proxy route-split lands (no double injection by design).
  let wbCursor = 0;
  async function writebackTick() {
    try {
      const drawerOpen = !!document.querySelector("#aetherstate_panel .inline-drawer-content:not([style*='display: none'])");
      if (!drawerOpen && Date.now() - lastGen > 60000) return;
      const d = await api(`/aether/session/${sid()}/writeback?cursor=${wbCursor}`);
      if (!d || d.error) return;
      wbCursor = d.cursor || wbCursor;
      if (d.chat_metadata_patch?.aetherstate) {
        ctx.chatMetadata.aetherstate =
          Object.assign(ctx.chatMetadata.aetherstate || {}, d.chat_metadata_patch.aetherstate);
        try { ctx.saveMetadataDebounced(); } catch (e) {}
      }
    } catch (e) {}
  }
  setInterval(writebackTick, 4000);
  async function refreshChip() {
    const el = document.getElementById("aes_chip");
    if (!el) return;
    try {
      const d = await api("/aether/status");
      el.className = "aes-chip";
      el.textContent = `AetherState ${d.version} · ${d.mode} · ${d.extraction.mode}`;
    } catch (e) { el.className = "aes-chip bad"; el.textContent = "AetherState: offline"; }
  }
  async function drawPanel() {
    try {
      const holder = document.getElementById("extensions_settings2") ||
                     document.getElementById("extensions_settings");
      if (!holder || document.getElementById("aetherstate_panel")) return;
      const div = document.createElement("div");
      div.id = "aetherstate_panel";
      div.innerHTML = `
        <div class="inline-drawer">
          <div class="inline-drawer-toggle inline-drawer-header">
            <b>AetherState</b><div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
          </div>
          <div class="inline-drawer-content">
            <div class="aes-row"><span id="aes_chip" class="aes-chip">…</span></div>
            <div class="aes-row">
              <button class="aes-freeze" id="aes_freeze">FREEZE</button>
              <button class="aes-resume" id="aes_resume">RESUME</button>
              <label><input type="checkbox" id="aes_override"> manual override</label>
            </div>
            <div class="aes-row">
              <label><input type="checkbox" id="aes_enabled"> enabled</label>
              <label><input type="checkbox" id="aes_mode" checked> enrichment</label>
              <input id="aes_proxy" placeholder="proxy url" style="flex:1" />
              <a id="aes_console" target="_blank">open Console</a>
            </div>
            <div class="aes-row">
              <input id="aes_guard" placeholder="your character name (blank = {{user}})" style="flex:1" />
              <span id="aes_groups"></span>
            </div>
            <div class="aes-row">
              <label style="font-size:12px">update state every
                <input id="aes_cadence" type="number" min="1" max="50" style="width:3.5em" /> turn(s)</label>
              <label style="font-size:12px">story context intake
                <input id="aes_intake" type="number" min="0" max="200000" step="1000" style="width:6.5em" /> chars</label>
            </div>
            <div class="aes-row" style="opacity:.75;font-size:12px">
              Headroom: keep ~1200 tokens free in ST's context size so the state
              briefing never crowds the chat (Settings → Context Size).
            </div>
          </div>
        </div>`;
      holder.appendChild(div);
      const $ = (id) => document.getElementById(id);
      $("aes_enabled").checked = settings.enabled;
      $("aes_proxy").value = settings.proxy_url;
      $("aes_console").href = settings.proxy_url + "/aether/console";
      $("aes_enabled").onchange = (e) => { settings.enabled = e.target.checked; save(); };
      $("aes_proxy").onchange = (e) => {
        settings.proxy_url = e.target.value; save();
        $("aes_console").href = settings.proxy_url + "/aether/console";
      };
      $("aes_mode").onchange = (e) => api(`/aether/session/${sid()}/mode`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ mode: e.target.checked ? "enriched" : "passthrough" }),
      }).catch(() => {});
      $("aes_guard").value = settings.guard.override_name || "";
      $("aes_guard").onchange = (e) => {
        settings.guard.override_name = e.target.value.trim() || null; save();
      };
      try {                                    // cadence + intake (2026-07-04)
        const ex = await api("/aether/extraction");
        if (ex && ex.cadence_turns) $("aes_cadence").value = ex.cadence_turns;
        if (ex && ex.intake_chars != null) $("aes_intake").value = ex.intake_chars;
      } catch (e) {}
      const postExtraction = (body) => api("/aether/extraction", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }).catch(() => {});
      $("aes_cadence").onchange = (e) => postExtraction({ cadence_turns: parseInt(e.target.value, 10) || 1 });
      $("aes_intake").onchange = (e) => postExtraction({ intake_chars: parseInt(e.target.value, 10) || 0 });
      try {                                    // assist group mirrors (05 §7, Q8)
        const st = await api("/aether/status");
        const groups = st.extraction?.groups || {};
        const sel = (g, v) => `<label style="font-size:12px">${g}
          <select data-g="${g}">${["off", "rules", "main", "assist"].map(
            (m) => `<option ${m === v ? "selected" : ""}>${m}</option>`).join("")}
          </select></label>`;
        $("aes_groups").innerHTML = ["memory_reflection", "embeddings", "linter_nli"]
          .filter((g) => g in groups).map((g) => sel(g, groups[g])).join(" ");
        $("aes_groups").querySelectorAll("select").forEach((el) => {
          el.onchange = () => api("/aether/groups", {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ [el.dataset.g]: el.value }),
          }).catch(() => {});
        });
      } catch (e) {}
      $("aes_freeze").onclick = () => api(`/aether/session/${sid()}/freeze`, { method: "POST" }).catch(() => {});
      $("aes_resume").onclick = () => api(`/aether/session/${sid()}/unfreeze`, { method: "POST" }).catch(() => {});
      try {
        const o = await api("/aether/override");
        $("aes_override").checked = !!o.enabled;
      } catch (e) {}
      $("aes_override").onchange = (e) => api("/aether/override", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled: e.target.checked }),
      }).catch(() => {});
      refreshChip(); setInterval(refreshChip, 15000);
    } catch (e) { console.warn("[AetherState] panel failed open", e); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", drawPanel);
  else drawPanel();

  // ---- slash commands (05 §8). Registered via SlashCommandParser.addCommandObject —
  // called ON the class (a detached reference loses `this` and threw silently before:
  // that is why the commands never appeared in autocomplete). With helpString +
  // argument lists they now show up in ST's / autofill like native commands.
  try {
    const P = ctx.SlashCommandParser, C = ctx.SlashCommand,
          A = ctx.SlashCommandArgument, T = ctx.ARGUMENT_TYPE;
    if (P?.addCommandObject && C?.fromProps) {
      const arg = (desc, required) => {
        try {
          return A?.fromProps
            ? [A.fromProps({ description: desc, typeList: T ? [T.STRING] : [],
                             isRequired: !!required })]
            : [];
        } catch (e) { return []; }
      };
      const cmd = (name, cb, help, unnamedDesc, requiredArg) =>
        P.addCommandObject(C.fromProps({
          name, callback: cb, helpString: help, returns: "status text",
          unnamedArgumentList: unnamedDesc ? arg(unnamedDesc, requiredArg) : [],
        }));
      cmd("aether-status", async () => {
        try { const d = await api("/aether/status");
              return `${d.name} ${d.version} · ${d.mode} · extraction ${d.extraction.mode}`; }
        catch (e) { return "AetherState: offline"; }
      }, "Show AetherState proxy status.");
      cmd("aether-freeze", async () => {
        await api(`/aether/session/${sid()}/freeze`, { method: "POST" });
        return "scene frozen";
      }, "Pause the scene (safeword-equivalent). Aftercare register takes over.");
      cmd("aether-resume", async () => {
        await api(`/aether/session/${sid()}/unfreeze`, { method: "POST" });
        return "scene resumed";
      }, "Resume a frozen scene. Human-only by design.");
      cmd("aether-set", async (_n, value) => {
        const [path, ...rest] = String(value || "").trim().split(/\s+/);
        if (!path) return "usage: /aether-set <path> <value>";
        const d = await api(`/aether/session/${sid()}/state`, {
          method: "PATCH", headers: { "content-type": "application/json" },
          body: JSON.stringify({ path, value: rest.join(" ") }),
        });
        return d.applied ? "applied" : "rejected: " + JSON.stringify(d.rejected);
      }, "Set a state value, e.g. /aether-set scene.location tavern. Authority rules apply.",
         "path value", true);
      cmd("aether-mode", async (_n, value) => {
        const mode = String(value || "").trim();
        const d = await api(`/aether/session/${sid()}/mode`, {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        return d.mode ? `mode: ${d.mode}` : "usage: /aether-mode enriched|passthrough";
      }, "Turn AetherState enrichment on (enriched) or off (passthrough) for this chat.",
         "enriched|passthrough", true);
      cmd("aether-genesis", async () => {
        // explicit user intent -> force=1: re-seeds even if an earlier (pre-fix)
        // attempt marked this session done/skipped with an empty result.
        const d = await doGenesis("command", true);
        if (d && d.session_id)
          return `genesis: card ${d.card_len ?? "?"} ch, greeting ${d.greeting_len ?? "?"} ch, `
               + `speaker ${d.speaker || "?"} — seeded ${d.applied || 0} op(s) into `
               + `${String(d.session_id).slice(0, 8)}`
               + (d.prior_state ? ` (was '${d.prior_state}', re-run)` : " (first run)")
               + (d.scheduled ? "; full LLM pass running — check the panel in ~15s" : "");
        return "genesis failed: " + (d && d.error ? d.error : "unknown");
      }, "Seed state from the character card now (turn-0 genesis; re-runs even if already seeded).");
      cmd("aether-cadence", async (_n, value) => {
        const n = parseInt(String(value || "").trim(), 10);
        if (!n || n < 1 || n > 50)
          return "usage: /aether-cadence <1-50> — update state every N turns (1 = every turn)";
        const d = await api("/aether/extraction", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ cadence_turns: n }),
        });
        return d && d.cadence_turns
          ? `state now updates every ${d.cadence_turns} turn(s)` : "failed to set cadence";
      }, "Set how often the state updates: every N turns (1 = every turn).",
         "turns (1-50)", true);
      console.log("[AetherState] slash commands registered");
    }
  } catch (e) { console.warn("[AetherState] slash registration failed", e); }
})();
