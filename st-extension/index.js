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
  console.log("[AetherState] Companion loaded — hud-clarity build (2026-07-09)");
  // ST reassigns chatMetadata/characterId on chat/char switch, so a context captured once
  // goes stale. C() always returns the CURRENT context for per-chat/character reads.
  const C = () => { try { return SillyTavern.getContext() || ctx; } catch (e) { return ctx; } };

  const defaults = {
    enabled: true,
    proxy_url: "http://127.0.0.1:9130",
    stamp: { header: true, sentinel: true },
    guard: { override_name: null },
    panel: { show_status_chip: true },
    hud: { open: false, theme: "neutral", top: 70, left: null, right: 18, width: 360,
           edit: false, compact: false, tab: "char", hideTags: true },
  };
  const settings = Object.assign({}, defaults, ctx.extensionSettings[MODULE]);
  // per-key hud merge (2026-07-09): a saved hud from an older build REPLACES the default
  // object wholesale, so any newly-added key came up undefined forever. Merge key-by-key.
  settings.hud = Object.assign({}, defaults.hud, settings.hud || {});
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
  async function doGenesis(reason, force = false, ifearly = false) {
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
    // 2026-07-06: prefer the greeting actually SHOWN in chat — message.mes reflects the
    // current swipe, so alternative greetings seed correctly. first_mes is the fallback.
    let greeting = "";
    try { const m = (cx.chat || []).find((x) => !x.is_user && x.mes); if (m) greeting = sub(m.mes); } catch (e) {}
    if (!greeting) greeting = sub(ch.first_mes || "");
    if (!card && !greeting) { console.warn("[AetherState] genesis: empty card+greeting"); return { error: "empty card" }; }
    const S = sid();
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 8000);        // seeding is fast (Stage B is async)
      const q = [force ? "force=1" : "", ifearly ? "ifearly=1" : ""].filter(Boolean).join("&");
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/genesis${q ? "?" + q : ""}`, {
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
  // 2026-07-06: swiping the FIRST message picks a different opening — re-seed so state
  // reflects the greeting the player actually chose. ifearly=1 makes the proxy refuse
  // once real turns exist, so an established chat is never disturbed.
  function genesisAtGreetingSwipe(i) {
    try {
      const chat = C().chat || [];
      const first = chat.findIndex((x) => !x.is_user);
      if (Number(i) !== first || first < 0) return;
      setTimeout(() => doGenesis("greeting_swipe", true, true).catch(() => {}), 400);
    } catch (e) {}
  }

  // ---- card-seed auto-apply (2026-07-08, REQUIRED for a smooth Creator flow): a Narrator card
  // built by the Creator carries the whole world + Player Card in extensions.aetherstate.seed.
  // On chat-open we hand that seed to the proxy, which commits it to THIS session's ledger —
  // deterministically, no LLM. This is what removes "you have to re-apply the world to every
  // new chat": import the card, open a chat, and the world + your character are already there.
  // The /seed route is idempotent (skips a world/player already present), so an established
  // chat is never disturbed. Fire-and-forget, fail-open; runs BEFORE genesis so presence/mood
  // seeding sees the committed world.
  function cardSeed() {
    try {
      const cx = C();
      const ch = cx.characters?.[cx.characterId];
      const ext = ch?.data?.extensions?.aetherstate || ch?.extensions?.aetherstate;
      const seed = ext && ext.seed;
      if (!seed || !(seed.world || seed.player)) return null;
      return seed;
    } catch (e) { return null; }
  }
  async function seedFromCard() {
    if (!settings.enabled) return;
    let seed = null, cx = C();
    for (let i = 0; i < 8 && !seed; i++) {                   // CHAT_CHANGED can fire before the
      cx = C(); if (cx.characters?.[cx.characterId]) { seed = cardSeed(); break; }
      await new Promise((r) => setTimeout(r, 250));          // character loads a beat later
    }
    if (!seed) seed = cardSeed();
    if (!seed) return;
    const S = sid();
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 6000);
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/seed`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ seed }),
      });
      clearTimeout(t);
      const d = await r.json().catch(() => ({}));
      console.log(`[AetherState] card-seed sid=${S} ->`, d);
      try { refreshChip(); } catch (e) {}
    } catch (e) { /* fail-open: genesis + the card prose still seed the session */ }
  }

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
      seedFromCard().catch(() => {}).finally(() => genesisAtChatOpen());   // card seed FIRST,
      //                                             then turn-0 genesis (both proxy-idempotent)
      try { setTimeout(scrubTags, 400); setTimeout(scrubTags, 1400); } catch (e) {}   // hide ledger tags
      try {                                                 // 2026-07-07 live repro: the panel's
        const a = document.getElementById("aes_creator");   // Creator link kept the PREVIOUS
        if (a) a.href = settings.proxy_url                  // chat's session — copy-link or
          + "/aether/creator?session=" + encodeURIComponent(sid());   // middle-click then saved
      } catch (e) { /* fail-open */ }                       // the world to the WRONG session
    });
    on("GENERATION_STARTED", (type, opts, dryRun) => {
      try {
        if (dryRun) return;
        if (type) lastGenType = type;                       // fallback capture
        if (!type || type === "normal" || type === "continue") turnCounter++;
      } catch (e) {}
    });
    on("MESSAGE_SWIPED", (i) => { hint("swipe", Number(i)); genesisAtGreetingSwipe(i);
      try { setTimeout(scrubTags, 120); setTimeout(scrubTags, 900); } catch (e) {} });
    on("MESSAGE_EDITED", (i) => { hint("edit", Number(i)); try { setTimeout(scrubTags, 120); } catch (e) {} });
    on("MESSAGE_DELETED", (i) => hint("delete", Number(i)));
    on("MESSAGE_RECEIVED", () => { refreshChip(); lastGen = Date.now();
      try { setTimeout(scrubTags, 80); setTimeout(scrubTags, 500); setTimeout(scrubTags, 1600); } catch (e) {}
      try { if (hudVisible()) { setTimeout(hudRefresh, 1500); setTimeout(hudRefresh, 6000); } } catch (e) {} });
  } catch (e) { console.warn("[AetherState] event wiring unavailable", e); }

  // ---- quick panel (05 §7)
  let lastGen = 0;
  // Circuit breaker (2026-07-07): the browser logs a failed request for EVERY poll while the
  // proxy is down — with the HUD/writeback/chip loops that is a console flood. When calls start
  // failing we mark the proxy offline and the periodic POLLERS back off to one probe / 20 s
  // (user-initiated calls always go through); the first success resumes normal cadence.
  let _offlineSince = 0, _lastProbe = 0;
  const _mark = (ok) => { if (ok) _offlineSince = 0; else if (!_offlineSince) _offlineSince = Date.now(); };
  const pollSkip = () => {                 // a poller calls this and returns early when true
    if (!_offlineSince) return false;
    const now = Date.now();
    if (now - _lastProbe < 20000) return true;
    _lastProbe = now; return false;        // let ONE probe through every 20 s while offline
  };
  const api = async (path, opts = {}) => {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 2000);           // 05 §9: 2 s, silent degrade
    try {
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + path,
                            { ...opts, signal: ac.signal });
      const j = await r.json();
      _mark(true);
      return j;
    } catch (e) { _mark(false); throw e; } finally { clearTimeout(t); }
  };

  // ---- native writeback loop (05 §6): v1 applies the chat-metadata patch; WI/AN
  // arrive empty until the proxy route-split lands (no double injection by design).
  let wbCursor = 0;
  async function writebackTick() {
    try {
      if (pollSkip()) return;              // proxy offline → don't hammer it every 4 s
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
  setInterval(scrubTags, 2500);          // safety net: catch tags on messages the events missed
  async function refreshChip() {
    const el = document.getElementById("aes_chip");
    if (!el) return;
    if (pollSkip()) return;                 // offline → the shared 20 s probe covers recovery
    try {
      const d = await api("/aether/status");
      let spec = "";
      try { const sp = await api("/aether/specialization"); spec = sp && sp.name ? ` · ${sp.name.toUpperCase()}` : ""; } catch (e) {}
      el.className = "aes-chip";
      el.textContent = `AetherState ${d.version} · ${d.mode} · ${d.extraction.mode}${spec}`;
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
              <label class="aes-check"><input type="checkbox" id="aes_override"> manual override</label>
            </div>
            <div class="aes-row">
              <label class="aes-check"><input type="checkbox" id="aes_enabled"> enabled</label>
              <label class="aes-check"><input type="checkbox" id="aes_mode" checked> enrichment</label>
              <a id="aes_console" class="aes-link" target="_blank">open Console</a>
              <a id="aes_creator" class="aes-link" target="_blank">open Creator</a>
              <a id="aes_hud_open" class="aes-link" href="#">🎛 player HUD</a>
            </div>
            <div class="aes-row">
              <label class="aes-inline">narrative mode
                <select id="aes_spec"><option value="none">none (chat RP)</option><option value="rpg">rpg (DM mode)</option></select></label>
              <span id="aes_spec_state" class="aes-chip">spec: …</span>
            </div>
            <div class="aes-help" id="aes_spec_help"></div>
            <div class="aes-field">
              <label class="aes-label" for="aes_proxy">Proxy URL</label>
              <input id="aes_proxy" class="text_pole aes-input" placeholder="http://127.0.0.1:9130" />
            </div>
            <div class="aes-field">
              <label class="aes-label" for="aes_guard">Your name <span class="aes-opt">— optional</span></label>
              <input id="aes_guard" class="text_pole aes-input" placeholder="blank = your ST persona ({{user}})" />
              <div class="aes-help">Tells AetherState which character is <b>you</b>, so the AI never writes
                in your voice and never tracks you as an NPC. Leave blank to use your SillyTavern persona
                name automatically — only type a name here to override it.</div>
            </div>
            <div class="aes-row"><span id="aes_groups"></span></div>
            <div class="aes-row">
              <label class="aes-inline">update state every
                <input id="aes_cadence" class="text_pole aes-num" type="number" min="1" max="50" /> turn(s)</label>
              <label class="aes-inline">story context intake
                <input id="aes_intake" class="text_pole aes-num" type="number" min="0" max="200000" step="1000" /> chars</label>
            </div>
            <div class="aes-row aes-note">
              Headroom: keep context free for the state briefing so it never crowds the chat
              (Settings → Context Size) — ~1200 tokens for chat RP, ~2400 for RPG mode (it
              injects the full sheet plus the DM rules).
            </div>
          </div>
        </div>`;
      holder.appendChild(div);
      const $ = (id) => document.getElementById(id);
      $("aes_enabled").checked = settings.enabled;
      $("aes_proxy").value = settings.proxy_url;
      $("aes_console").href = settings.proxy_url + "/aether/console";
      const creatorHref = () => settings.proxy_url + "/aether/creator?session=" + encodeURIComponent(sid());
      $("aes_creator").href = creatorHref();
      $("aes_creator").onclick = () => { $("aes_creator").href = creatorHref(); };
      $("aes_hud_open").onclick = (e) => { e.preventDefault(); openHud(); };
      const setSpecHelp = (name) => { const el = $("aes_spec_help"); if (!el) return;
        el.innerHTML = name === "rpg"
          ? "<b>RPG (DM mode):</b> the card runs the world as your Dungeon Master. Full engine — dice &amp; skill checks, a Player sheet, gear &amp; inventory, statuses, quests, XP &amp; mastery. Use the 🎛 player HUD and the Creator."
          : "<b>Chat RP:</b> casual roleplay with silent state-tracking only — no dice, no DM framing, no Player sheet. Byte-identical to plain AetherState 1.0."; };
      try {                                    // narrative mode: show it + let the user switch it
        const sp = await api("/aether/specialization");
        if (sp && sp.name) { $("aes_spec").value = sp.name; $("aes_spec_state").textContent = "spec: " + sp.name; setSpecHelp(sp.name); }
      } catch (e) {}
      $("aes_spec").onchange = async (e) => {
        setSpecHelp(e.target.value);
        const d = await api("/aether/specialization", { method: "POST",
          headers: { "content-type": "application/json" }, body: JSON.stringify({ name: e.target.value }) }).catch(() => null);
        if (d && d.name) { $("aes_spec_state").textContent = "spec: " + d.name; setSpecHelp(d.name); refreshChip();
          try { if (hudVisible()) hudRefresh(); } catch (err) {} }
      };
      $("aes_enabled").onchange = (e) => { settings.enabled = e.target.checked; save(); };
      $("aes_proxy").onchange = (e) => {
        settings.proxy_url = e.target.value; save();
        $("aes_console").href = settings.proxy_url + "/aether/console";
        $("aes_creator").href = creatorHref();
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
      try {                                    // assist group mirrors + per-group endpoint (05 §7, Q8)
        const st = await api("/aether/status");
        const groups = st.extraction?.groups || {};
        const gep = st.extraction?.group_endpoints || {};
        const eps = (st.extraction?.assist_endpoints || []).map((e) => e.name);
        const epOpts = (cur) => `<option value="">(default: first)</option>` +
          eps.map((n) => `<option ${n === cur ? "selected" : ""}>${n}</option>`).join("");
        const sel = (g, v) => `<label style="font-size:12px">${g}
          <select data-g="${g}" data-kind="mode">${["off", "rules", "main", "assist"].map(
            (m) => `<option ${m === v ? "selected" : ""}>${m}</option>`).join("")}</select>
          <select data-g="${g}" data-kind="ep" title="which assist endpoint"
            style="${v === "assist" && eps.length ? "" : "display:none"}">${epOpts(gep[g] || "")}</select></label>`;
        $("aes_groups").innerHTML = ["memory_reflection", "embeddings", "linter_nli"]
          .filter((g) => g in groups).map((g) => sel(g, groups[g])).join(" ");
        const postGroup = (body) => api("/aether/groups", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify(body) }).catch(() => {});
        $("aes_groups").querySelectorAll('select[data-kind="mode"]').forEach((el) => {
          el.onchange = () => {
            postGroup({ [el.dataset.g]: el.value });
            const ep = $("aes_groups").querySelector(`select[data-kind="ep"][data-g="${el.dataset.g}"]`);
            if (ep) ep.style.display = (el.value === "assist" && eps.length) ? "" : "none";
          };
        });
        $("aes_groups").querySelectorAll('select[data-kind="ep"]').forEach((el) => {
          el.onchange = () => postGroup({ group_endpoints: { [el.dataset.g]: el.value } });
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
  // =============================== player HUD (2026-07-07) =====================
  // A movable, themeable window surfacing the ledger's player-facing truth, fetched
  // from GET /aether/session/{sid}/hud — the SAME payload the Console renders, so the
  // two never diverge. Fail-open: proxy down -> a quiet "offline" line, ST untouched.
  const HUD_THEMES = { neutral: "Neutral", fantasy: "Fantasy", scifi: "Sci-Fi", modern: "Modern" };
  const HUD_TABS = [
    ["char", "◈ Char"], ["skills", "✦ Skills"], ["abilities", "❋ Abilities"], ["rolls", "🎲 Rolls"],
    ["gear", "⚔ Gear"], ["inventory", "🎒 Items"], ["status", "☤ Status"], ["world", "🌍 World"],
  ];
  let hudTimer = null;
  let lastHudView = null;             // cache the last /hud payload so tab-switching never refetches
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---- display-only ledger-tag hider (2026-07-07): the DM emits bracketed protocol tags
  // ([hp | ...], [scene | ...], and often invented ones) that the ENGINE parses from the raw
  // message — but they are engine plumbing, not prose. This hides them from the READER only:
  // it rewrites the RENDERED .mes_text, never message.mes, so the proxy still gets every tag
  // and the ledger keeps updating. Fail-open; toggle with settings.hud.hideTags.
  const _TAG_RE = () => /\[\s*[A-Za-z][^\]\n|]*\|[^\]\n]*\]/g;
  function scrubTags() {
    try {
      if (settings.hud && settings.hud.hideTags === false) return;
      document.querySelectorAll("#chat .mes_text").forEach((t) => {
        if (!t || t.dataset.aesScrubbed === "1") return;
        const html = t.innerHTML;
        if (!_TAG_RE().test(html)) return;
        t.innerHTML = html.replace(_TAG_RE(),
          (m) => `<span class="aes-hidden-tag" title="AetherState ledger tag (hidden)">${m}</span>`);
        t.dataset.aesScrubbed = "1";
      });
    } catch (e) { /* fail-open: never touch the chat if this errors */ }
  }

  function buildHud() {
    if (document.getElementById("aes_hud_launch")) return;
    const launch = document.createElement("button");
    launch.id = "aes_hud_launch"; launch.className = "aes-hud-launch";
    launch.title = "AetherState player HUD"; launch.textContent = "◈";
    launch.onclick = toggleHud; document.body.appendChild(launch);
    const hud = document.createElement("div");
    hud.id = "aes_hud"; hud.className = "aes-hud hidden t-" + (settings.hud.theme || "neutral");
    hud.innerHTML = `
      <div class="aes-hud-bar" id="aes_hud_bar">
        <span class="aes-hud-title">◈ AetherState</span>
        <span class="aes-hud-spec none" id="aes_hud_spec">…</span>
        <span class="aes-hud-grow"></span>
        <select id="aes_hud_theme" title="theme">${Object.entries(HUD_THEMES).map(
          ([k, n]) => `<option value="${k}">${n}</option>`).join("")}</select>
        <button id="aes_hud_edit" title="edit mode — spend points, equip, use, adjust">✎</button>
        <button id="aes_hud_min" title="minimize / expand">▁</button>
        <button id="aes_hud_ref" title="refresh">⟳</button>
        <button id="aes_hud_close" title="close">✕</button>
      </div>
      <div class="aes-hud-body" id="aes_hud_body"><div class="aes-hud-empty">Loading…</div></div>`;
    document.body.appendChild(hud);
    const p = settings.hud, $h = (id) => document.getElementById(id);
    hud.style.top = (p.top || 70) + "px";
    if (p.left != null) { hud.style.left = p.left + "px"; hud.style.right = "auto"; }
    else hud.style.right = (p.right != null ? p.right : 18) + "px";
    if (p.width) hud.style.width = p.width + "px";
    $h("aes_hud_theme").value = settings.hud.theme || "neutral";
    $h("aes_hud_theme").onchange = (e) => applyTheme(e.target.value);
    $h("aes_hud_ref").onclick = hudRefresh;
    $h("aes_hud_close").onclick = closeHud;
    if (settings.hud.edit) hud.classList.add("editing");
    if (settings.hud.compact) hud.classList.add("compact");
    $h("aes_hud_edit").onclick = () => { settings.hud.edit = hud.classList.toggle("editing"); save(); };
    $h("aes_hud_min").onclick = () => { settings.hud.compact = hud.classList.toggle("compact"); save(); syncMinBtn(); hudRefresh(); };
    syncMinBtn();
    makeDraggable(hud, $h("aes_hud_bar"));
    if (settings.hud.open) openHud();
  }
  // apply a state op (or ops) via the same privileged PATCH the Console uses, then refresh
  async function hudOp(ops) {
    try {
      await api(`/aether/session/${sid()}/state`, { method: "PATCH",
        headers: { "content-type": "application/json" }, body: JSON.stringify({ ops }) });
    } catch (e) {}
    setTimeout(hudRefresh, 120);
  }
  window.aetherHudOp = hudOp;        // inline row buttons call this (delegated onclick handlers)
  function applyTheme(t) { const h = document.getElementById("aes_hud"); if (!h) return;
    h.className = h.className.replace(/t-\w+/, "t-" + t); settings.hud.theme = t; save(); }
  function hudVisible() { const h = document.getElementById("aes_hud");
    return h && !h.classList.contains("hidden"); }
  function openHud() { const h = document.getElementById("aes_hud"); if (!h) return;
    h.classList.remove("hidden"); settings.hud.open = true; save(); hudRefresh();
    if (!hudTimer) hudTimer = setInterval(() => { if (hudVisible() && !pollSkip()) hudRefresh(); }, 5000); }
  function closeHud() { const h = document.getElementById("aes_hud"); if (!h) return;
    h.classList.add("hidden"); settings.hud.open = false; save(); }
  function toggleHud() { hudVisible() ? closeHud() : openHud(); }
  // the minimize button must SHOW which state you're in (2026-07-09: Bean lost days to a
  // silently-minimized HUD that "displayed nothing beyond hp/stamina/mana") — never again.
  function syncMinBtn() {
    const h = document.getElementById("aes_hud"), b = document.getElementById("aes_hud_min");
    if (!h || !b) return;
    const c = h.classList.contains("compact");
    b.textContent = c ? "▣" : "▁";
    b.title = c ? "EXPAND — the HUD is minimized to a vitals strip" : "minimize to a compact vitals strip";
  }
  window.aetherHudExpand = () => {
    const h = document.getElementById("aes_hud"); if (!h) return;
    h.classList.remove("compact"); settings.hud.compact = false; save(); syncMinBtn(); hudRefresh();
  };
  function makeDraggable(box, handle) {
    let sx, sy, ox, oy, drag = false;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.tagName === "SELECT" || e.target.tagName === "BUTTON") return;
      drag = true; sx = e.clientX; sy = e.clientY;
      const r = box.getBoundingClientRect(); ox = r.left; oy = r.top;
      box.style.right = "auto"; e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => { if (!drag) return;
      box.style.left = Math.max(0, ox + e.clientX - sx) + "px";
      box.style.top = Math.max(0, oy + e.clientY - sy) + "px"; });
    window.addEventListener("mouseup", () => { if (!drag) return; drag = false;
      const r = box.getBoundingClientRect();
      settings.hud.left = Math.round(r.left); settings.hud.top = Math.round(r.top);
      settings.hud.right = null; save(); });
  }
  async function hudRefresh() {
    const body = document.getElementById("aes_hud_body"); if (!body) return;
    let v = null; try { v = await api(`/aether/session/${sid()}/hud`); } catch (e) {}
    const spec = document.getElementById("aes_hud_spec");
    const hud = document.getElementById("aes_hud");
    if (!v) { body.innerHTML = `<div class="aes-hud-empty aes-hud-off">AetherState proxy offline.</div>`; return; }
    if (spec) { spec.textContent = v.spec || "none"; spec.className = "aes-hud-spec" + (v.spec === "rpg" ? "" : " none"); }
    lastHudView = v;
    const _ae = document.activeElement;
    if (_ae && _ae.id === "aes_roll_custom") return;   // don't clobber a custom roll being typed
    // VISIBLE fail-open (2026-07-09): a throw inside a renderer used to leave the PREVIOUS
    // innerHTML on screen forever — an invisible break that looks like "the HUD lost my data".
    // The ledger is never at risk; the reader must SEE that the view (not the truth) failed.
    try {
      body.innerHTML = (hud && hud.classList.contains("compact")) ? renderCompact(v) : renderHud(v);
    } catch (e) {
      console.error("[AetherState] HUD render failed", e);
      body.innerHTML = `<div class="aes-hud-empty aes-hud-off">⚠ HUD render error — ${esc(e && e.message ? e.message : String(e))}.<br>The ledger is intact; refresh (⟳) or report this.</div>`;
    }
  }
  // tab switch: re-render the cached payload in place (no network round-trip)
  window.aetherHudTab = (t) => {
    settings.hud.tab = t; save();
    const body = document.getElementById("aes_hud_body");
    if (!body || !lastHudView) return;
    try { body.innerHTML = renderHud(lastHudView); }
    catch (e) {
      console.error("[AetherState] HUD tab render failed", e);
      body.innerHTML = `<div class="aes-hud-empty aes-hud-off">⚠ HUD render error — ${esc(e && e.message ? e.message : String(e))}.<br>The ledger is intact; refresh (⟳) or report this.</div>`;
    }
  };
  // an inline op button (edit-mode only via CSS). JSON is single-quote-safe for the attribute.
  function actBtn(label, ops, title, cls) {
    const j = JSON.stringify(ops).replace(/'/g, "&#39;");
    return `<button class="aes-act ${cls || ""}" title="${esc(title || "")}" onclick='window.aetherHudOp(${j})'>${esc(label)}</button>`;
  }
  function renderCompact(v) {
    const p = (v.players || [])[0], s = v.scene || {};
    const loc = s.location ? esc(String(s.location).replace(/_/g, " ")) : "—";
    let h = `<div class="aes-kv" style="margin:2px 0">📍 ${loc}${s.time_of_day ? " · " + esc(s.time_of_day) : ""}</div>`;
    if (!p) return h + `<div class="aes-hud-empty">no player</div>`;
    h += `<div class="aes-sub" style="margin:2px 0">${esc(p.name)} · Lv${p.level}</div>`;
    const bars = []; if (p.hp && p.hp.max) bars.push(bar("hp", p.hp.cur, p.hp.max));
    for (const k in (p.resources || {})) { const r = p.resources[k]; bars.push(bar(k, r.cur, r.max)); }
    if (bars.length) h += `<div class="aes-bars">${bars.join("")}</div>`;
    if ((p.effects || []).length) h += `<div class="aes-rows" style="margin-top:4px">${p.effects.map((e) => { const cls = e.valence === "positive" ? "pos good" : e.valence === "negative" ? "neg bad" : "neu"; return `<span class="aes-pill ${cls}"><span class="g">${esc(e.glyph)}</span> ${esc(e.name)}</span>`; }).join("")}</div>`;
    if (v.war_room && v.war_room.active) {           // Phase 1: minimized combat strip
      const foes = (v.war_room.combatants || []).filter((c) => c.side === "enemy" && !c.defeated);
      if (foes.length) h += `<div class="aes-kv" style="margin-top:4px">⚔ ${foes.map((c) => `${esc(c.name)} ${esc(c.hp.cur)}/${esc(c.hp.max)}`).join(" · ")}</div>`;
    }
    const lr = (v.rolls || []).slice(-1)[0];
    if (lr && lr.tier_label) {
      const tc = (lr.tier === "success" || lr.tier === "crit_success") ? "success" : (lr.tier === "partial" ? "partial" : "fail");
      h += `<div class="aes-lastroll ${tc}">\uD83C\uDFB2 ${esc(lr.skill || "roll")} \u2192 <b>${esc(lr.tier_label)}</b> <span class="m">(${esc(lr.result)})</span></div>`;
    }
    // the strip must SAY it is a strip (2026-07-09) — a minimized HUD with no label reads
    // as "everything is gone". One tap restores the full sheet.
    h += `<button class="aes-expand" onclick="window.aetherHudExpand()" title="this compact strip shows vitals only — the full sheet (skills, gear, status, world…) is one tap away">▣ expand — full sheet</button>`;
    return h;
  }
  function bar(kind, cur, max) { const pct = max ? Math.max(0, Math.min(100, Math.round(100 * cur / max))) : 0;
    return `<div class="aes-bar ${kind}"><i style="width:${pct}%"></i><span>${esc(kind.toUpperCase())} ${esc(cur)}/${esc(max)}</span></div>`; }
  function sec(title, ic, html) { return `<div class="aes-sec"><div class="aes-sec-h"><span class="ic">${ic}</span>${esc(title)}</div>${html}</div>`; }
  function sechdr(t) { return `<div class="aes-sec-h" style="margin-top:8px">${esc(t)}</div>`; }
  // Tabbed HUD (2026-07-07): a persistent vitals strip + a tab bar so the whole tracked sheet
  // is organized, not dumped in one scroll. Char · Skills · Abilities · Gear (paper-doll) ·
  // Status · World. The player always sees vitals; the detail lives one tap away.
  function renderHud(v) {
    const p = (v.players || [])[0];
    let head = renderVitals(v, p);
    if (v.frozen) head += `<div class="aes-hud-off">⏸ scene frozen${v.frozen_reason ? " (" + esc(v.frozen_reason) + ")" : ""}</div>`;
    if (v.war_room && v.war_room.active) head += renderWarRoom(v.war_room);   // Phase 1:
    // the combat lane rides ABOVE the tabs, combat-phase-only — it vanishes with the fight
    const tab = HUD_TABS.some((t) => t[0] === settings.hud.tab) ? settings.hud.tab : "char";
    head += `<div class="aes-tabs">${HUD_TABS.map(([k, label]) =>
      `<button class="aes-tab ${k === tab ? "on" : ""}" onclick="window.aetherHudTab('${k}')">${esc(label)}</button>`).join("")}</div>`;
    let body;
    if (!p) body = `<div class="aes-hud-empty">No player character yet.${v.spec !== "rpg"
      ? " Narrative mode is “" + esc(v.spec) + "” — switch to RPG to track a player." : " Build one in the Creator."}</div>`;
    else if (tab === "skills") body = tabSkills(v, p);
    else if (tab === "abilities") body = tabAbilities(v, p);
    else if (tab === "rolls") body = tabRolls(v, p);
    else if (tab === "gear") body = tabGear(v, p);
    else if (tab === "inventory") body = tabInventory(v, p);
    else if (tab === "status") body = tabStatus(v, p);
    else if (tab === "world") body = tabWorld(v, p);
    else body = tabChar(v, p);
    return head + `<div class="aes-tabbody">${body}</div>`;
  }
  function renderVitals(v, p) {
    const s = v.scene || {};
    const loc = s.location ? esc(String(s.location).replace(/_/g, " ")) : "—";
    let h = `<div class="aes-vitals"><div class="aes-vit-scene">📍 ${loc}${s.time_of_day ? " · " + esc(s.time_of_day) : ""}${s.phase ? " · " + esc(s.phase) : ""}</div>`;
    if (p) {
      h += `<div class="aes-vit-name">${esc(p.name)} <span class="m">Lv${esc(p.level)}${p.xp ? " · XP " + esc(p.xp) : ""}</span>${p.stat_points ? ` <span class="aes-tag warn">${esc(p.stat_points)} pt</span>` : ""}${p.mood ? ` <span class="aes-dim">${esc(p.mood)}</span>` : ""}</div>`;
      const bars = []; if (p.hp && p.hp.max) bars.push(bar("hp", p.hp.cur, p.hp.max));
      for (const k in (p.resources || {})) { const r = p.resources[k]; bars.push(bar(k, r.cur, r.max)); }
      if (bars.length) h += `<div class="aes-bars">${bars.join("")}</div>`;
      h += `<div class="aes-act-row">${actBtn("HP −5", [{ op: "hp_adj", char: p.eid, delta: -5 }], "lose 5 HP")}${actBtn("−1", [{ op: "hp_adj", char: p.eid, delta: -1 }], "lose 1 HP")}${actBtn("+1", [{ op: "hp_adj", char: p.eid, delta: 1 }], "heal 1 HP")}${actBtn("+5", [{ op: "hp_adj", char: p.eid, delta: 5 }], "heal 5 HP")}</div>`;
      const lr = (v.rolls || []).slice(-1)[0];
      if (lr && lr.tier_label) {
        const tc = (lr.tier === "success" || lr.tier === "crit_success") ? "success" : (lr.tier === "partial" ? "partial" : "fail");
        h += `<div class="aes-lastroll ${tc}">\uD83C\uDFB2 ${esc(lr.skill || "roll")} \u2192 <b>${esc(lr.tier_label)}</b> <span class="m">(${esc(lr.result)})</span></div>`;
      }
    }
    return h + `</div>`;
  }
  // ---- Phase 1: the War Room lane (plan doc 13, ratified) — combatant cards with EXACT HP
  // numbers (pillar-17 rawness), tier/armament, the pre-rolled enemy + ally dice (visible,
  // ratified), defeat marks and fresh loot chips. Rendered from committed rows only.
  function renderWarRoom(w) {
    const die = (d) => d ? `<span class="aes-die ${d.tier === "MISSES" ? "miss" : d.tier === "GRAZES" ? "graze" : "hit"}" title="pre-rolled action die (deterministic this turn)">🎲${esc(d.total)} ${esc(d.tier.toLowerCase())}</span>` : "";
    const card = (c) => {
      const pct = c.hp.max ? Math.max(0, Math.min(100, Math.round(100 * c.hp.cur / c.hp.max))) : 0;
      let h = `<div class="aes-com ${c.side}${c.defeated ? " down" : ""}">`;
      h += `<div class="aes-com-h">${c.side === "enemy" ? "⚔" : "🛡"} <b>${esc(c.name)}</b>`;
      if (c.tier && c.tier !== "standard") h += ` <span class="aes-tag${c.tier === "boss" ? " warn" : ""}">${esc(c.tier)}</span>`;
      if (c.kind === "tracked") h += ` <span class="aes-tag ok" title="a tracked character — wounds persist after the fight">tracked</span>`;
      h += c.defeated ? ` <span class="aes-tag bad">☠ down</span>` : ` ${die(c.die)}`;
      h += `</div>`;
      if (!c.defeated) h += `<div class="aes-bar hp com"><i style="width:${pct}%"></i><span>HP ${esc(c.hp.cur)}/${esc(c.hp.max)}</span></div>`;
      const bits = [];
      if (c.armament) bits.push(`<span class="aes-dim">⚒ ${esc(c.armament)}</span>`);
      if ((c.dropped || []).length) bits.push(`<span class="aes-pill warn" title="dropped loot on the field">💰 ${c.dropped.map(esc).join(", ")}</span>`);
      if (bits.length) h += `<div class="aes-kv">${bits.join(" ")}</div>`;
      return h + `</div>`;
    };
    const foes = (w.combatants || []).filter((c) => c.side === "enemy");
    const allies = (w.combatants || []).filter((c) => c.side !== "enemy");
    let h = `<div class="aes-war"><div class="aes-war-h">⚔ WAR ROOM <span class="m">round ${esc(w.round)}</span></div>`;
    if (foes.length) h += `<div class="aes-war-side">${foes.map(card).join("")}</div>`;
    if (allies.length) h += `<div class="aes-war-side">${allies.map(card).join("")}</div>`;
    return h + `</div>`;
  }
  function renderRules(rules) {
    if (!rules || !rules.dice) return "";
    let h = `<div class="aes-rules"><div class="aes-rules-h">🎲 How checks work — roll ${esc(rules.dice)}, keep best ${esc(rules.keep)}, add your modifier</div>`;
    h += `<div class="aes-rules-rows">${(rules.thresholds || []).map((t) =>
      `<div><b>${esc(t.range)}</b> <span class="t ${t.tier === "Success" ? "success" : t.tier === "Partial" ? "partial" : "fail"}">${esc(t.tier)}</span> — ${esc(t.desc)}</div>`).join("")}</div>`;
    if (rules.crits) h += `<div class="aes-rules-crit">${esc(rules.crits)}</div>`;
    if (rules.check_syntax) h += `<div class="aes-rules-syntax"><code>${esc(rules.check_syntax)}</code></div>`;
    if (rules.note) h += `<div class="aes-rules-note">${esc(rules.note)}</div>`;
    return h + `</div>`;
  }
  function renderCast(cast) {
    const rows = cast.map((c) => {
      let r = `<div class="aes-cast"><div class="aes-cast-h"><b>${esc(c.name)}</b>${c.present ? `<span class="aes-tag ok">here</span>` : `<span class="aes-tag">away${c.location ? " · " + esc(String(c.location).replace(/_/g, " ")) : ""}</span>`}${c.rel_tier ? `<span class="m">${esc(c.rel_tier)}</span>` : ""}${c.mood ? `<span class="aes-dim">${esc(c.mood)}</span>` : ""}${c.arousal > 0 ? `<span class="aes-dim">arousal ${esc(c.arousal)}</span>` : ""}</div>`;
      if ((c.effects || []).length) r += `<div class="aes-rows">${c.effects.map((e) => { const cls = e.valence === "positive" ? "pos good" : e.valence === "negative" ? "neg bad" : "neu"; return `<span class="aes-pill ${cls}" title="${esc(e.kind_label + (e.note ? " · " + e.note : ""))}"><span class="g">${esc(e.glyph)}</span> ${esc(e.name)} <span class="aes-tag">${esc(e.kind_label)}</span>${e.remaining != null ? ` <span class="m">${e.remaining}t</span>` : ""}${actBtn("×", [{ op: "effect_remove", char: c.eid, effect: e.key }], "remove", "x")}</span>`; }).join("")}</div>`;
      const dr = c.drives || {}, db = [...(dr.obsessions || []).map((o) => `<span class="aes-pill warn">☄ ${esc(o.target)} ${esc(o.intensity)}</span>`), ...(dr.cravings || []).map((cr) => `<span class="aes-pill ${cr.withdrawal ? "bad" : ""}">♦ ${esc(cr.substance)} ${esc(cr.level)}</span>`)];
      if (db.length) r += `<div class="aes-rows">${db.join("")}</div>`;
      if ((c.rel_dims || []).length) r += `<div class="aes-kv">${c.rel_dims.map((d) => `<span class="aes-dim">${esc(d.dim)} ${d.val >= 0 ? "+" : ""}${esc(d.val)}</span>`).join(" · ")}</div>`;
      if ((c.worn || []).length) r += `<div class="aes-kv"><span class="aes-dim">wearing</span> ${c.worn.map(esc).join(", ")}</div>`;
      if ((c.exposed || []).length) r += `<div class="aes-kv"><span class="aes-dim">exposed</span> ${c.exposed.map(esc).join(", ")}</div>`;
      if ((dr.goals || []).length) r += `<div class="aes-kv"><span class="aes-dim">goals</span> ${dr.goals.map(esc).join(" · ")}</div>`;
      return r + `</div>`;
    }).join("");
    return sec("Cast", "👥", rows);
  }
  // ---- Rolls tab (2026-07-08, Bean): one-tap check inserters. A button per rollable skill
  // drops ((aether.check <slug>)) into ST's message box — NON-destructive, stackable; you add
  // your prose and send yourself. The ENGINE still rolls the dice and injects the [DIRECTIVE];
  // this only writes the CALL, never the outcome (code resolves, the model narrates — pillar 3).
  let rollDraft = "";                                  // survives the 5s HUD re-render
  function aetherInsertText(text) {
    const ta = document.querySelector("#send_textarea");
    if (!ta) { console.warn("[AetherState] no #send_textarea to insert into"); return; }
    const cur = ta.value || "";
    ta.value = cur + (cur && !/\s$/.test(cur) ? " " : "") + text;
    ta.dispatchEvent(new Event("input", { bubbles: true }));   // ST reads value on send + auto-resizes
    ta.focus();
  }
  window.aetherInsertText = aetherInsertText;
  window.aetherInsertRoll = (slug, use) => {
    const s = String(slug || "").trim(); if (!s) return;
    const u = String(use || "").trim();
    aetherInsertText(`((aether.check ${s}${u ? " use " + u : ""}))`);
  };
  window.aetherRollDraft = (el) => { rollDraft = el.value; };
  window.aetherInsertCustom = () => {
    const inp = document.getElementById("aes_roll_custom");
    const val = ((inp ? inp.value : rollDraft) || "").trim();
    if (!val) return;
    aetherInsertText(/^\(\(/.test(val) ? val : `((aether.check ${val}))`);
    rollDraft = ""; if (inp) inp.value = "";
  };
  function tabRolls(v, p) {
    const sk = p.skills || [], abils = p.abilities || [];
    let h = `<div class="aes-roll-help"><b>Skills</b> are what you roll; <b>abilities</b> bend or unlock a skill roll — they never roll on their own. Tap to drop a check into your message, <b>or just write it</b>: name a skill or ability in your prose (e.g. \u201cI use Fire-Slash\u201d) and the engine rolls it for you.</div>`;
    if (!sk.length) h += `<div class="aes-hud-empty">No skills yet. Build a character in the Creator, or earn skills in-world.</div>`;
    else {
      const { groups, order } = groupByCategory(sk, "Skills");
      const solo = order.length === 1 && order[0] === "Skills";
      for (const g of order) {
        h += sechdr(solo ? "Skills \u2014 tap to roll" : g);
        h += `<div class="aes-rollbtns">${groups[g].map((s) => {
          const gated = s.gated && !s.basis_met;
          const t = gated ? "needs " + esc(s.basis_name || "a basis") + " \u2014 this would be a non-move"
                          : "roll ((aether.check " + esc(s.id) + "))";
          return `<button class="aes-rollbtn${gated ? " gated" : ""}" title="${t}" onclick="window.aetherInsertRoll('${esc(s.id)}')">${esc(s.label)} <span class="m">${s.mod >= 0 ? "+" : ""}${esc(s.mod)}</span></button>`;
        }).join("")}</div>`;
      }
    }
    const acts = abils.filter((a) => a.active);
    if (acts.length) {
      h += sechdr("Active abilities \u2014 invoke on a check");
      h += `<div class="aes-rollbtns">${acts.map((a) => {
        if (a.applies_id) {
          const t = "roll " + esc(a.applies_id) + " and spend " + esc(a.name) + (a.on_cd ? " (recharging " + esc(a.on_cd) + "t)" : "");
          return `<button class="aes-rollbtn act${a.on_cd ? " gated" : ""}" title="${t}" onclick="window.aetherInsertRoll('${esc(a.applies_id)}','${esc(a.id)}')">\u2726 ${esc(a.name)} <span class="m">on ${esc(a.applies_to)}</span></button>`;
        }
        return `<span class="aes-pill">\u2726 ${esc(a.name)} <span class="aes-tag">any check \u2014 type \u2018use ${esc(a.id)}\u2019</span></span>`;
      }).join("")}</div>`;
    }
    const pass = abils.filter((a) => !a.active);
    if (pass.length) {
      h += sechdr("Passive abilities \u2014 always on");
      h += `<div class="aes-rows">${pass.map((a) => `<span class="aes-pill">${esc(a.name)} <span class="aes-tag">${a.applies_to === "all checks" ? "all checks" : "on " + esc(a.applies_to)}</span></span>`).join("")}</div>`;
    }
    h += sechdr("Custom roll");
    h += `<div class="aes-roll-custom"><input id="aes_roll_custom" type="text" spellcheck="false" placeholder="skill name or slug" value="${esc(rollDraft)}" oninput="window.aetherRollDraft(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();window.aetherInsertCustom();}"><button class="aes-rollbtn" onclick="window.aetherInsertCustom()">Insert</button></div>`;
    h += `<div class="aes-roll-note">Must be a skill you actually have (or one you built in the Creator). An unknown name comes back as a visible \u201cno basis\u201d non-move \u2014 that's by design. Type <code>skill use ability</code> to invoke an active on a check.</div>`;
    return h;
  }
  function tabChar(v, p) {
    let h = "";
    if (p.appearance) h += `<div class="aes-appear">${esc(p.appearance)}</div>`;
    const sub = [p.concept, p.species, p.pronouns].filter(Boolean).join(" · ");
    if (sub) h += `<div class="aes-sub">${esc(sub)}</div>`;
    if ((p.stats || []).length) h += sechdr("Attributes") + `<div class="aes-stats">${p.stats.map((s) => `<div class="aes-stat"><small>${esc(s.key)}</small><b>${esc(s.val)}</b><em>${s.mod >= 0 ? "+" : ""}${esc(s.mod)}</em>${p.stat_points ? actBtn("+1", [{ op: "stat_spend", char: p.eid, stat: s.key }], "spend a banked point on " + s.key, "mini") : ""}</div>`).join("")}</div>`;
    const dr = p.drives || {}, dbits = [];
    (dr.obsessions || []).forEach((o) => dbits.push(`<span class="aes-pill warn">obsession: ${esc(o.target)} <span class="m">${esc(o.intensity)}</span>${actBtn("−", [{ op: "obsession", char: p.eid, target_kind: o.target_kind, target: o.target, delta: -10 }], "ease", "mini")}${actBtn("+", [{ op: "obsession", char: p.eid, target_kind: o.target_kind, target: o.target, delta: 10 }], "deepen", "mini")}</span>`));
    (dr.cravings || []).forEach((c) => dbits.push(`<span class="aes-pill ${c.withdrawal ? "bad" : ""}">craving: ${esc(c.substance)} <span class="m">${esc(c.level)}</span>${c.withdrawal ? " ⚠" : ""}${actBtn("sate", [{ op: "craving", char: p.eid, substance: c.substance, action: "consume" }], "sate the craving", "mini")}</span>`));
    if (dbits.length) h += sechdr("Drives") + `<div class="aes-rows">${dbits.join("")}</div>`;
    if ((dr.goals || []).length) h += sechdr("Goals") + `<div class="aes-kv">${dr.goals.map(esc).join(" · ")}</div>`;
    return h || `<div class="aes-hud-empty">No character detail recorded yet.</div>`;
  }
  function skillRowHtml(s) {
    return `<div class="aes-skill"><span class="aes-skl"><b>${esc(s.label)}</b> <span class="m big">${s.mod >= 0 ? "+" : ""}${esc(s.mod)}</span></span><span class="aes-skmeta">${s.keyed_stat ? esc(s.keyed_stat) : ""}${s.bracket ? " · " + esc(s.bracket) : ""}${s.mastery ? " · m" + esc(s.mastery) : ""}${s.cost ? " · costs " + esc(s.cost) : ""}${s.gated ? (s.basis_met ? ` · <span class="aes-tag ok">✦ ${esc(s.basis_name)}</span>` : ` · <span class="aes-tag bad">needs ${esc(s.basis_name || "a basis")}</span>`) : ""}</span></div>`;
  }
  // group skills by their free-form category (Bean 2026-07-07): "Spells", "Cyber-Ware", etc.
  // Ungrouped skills fall under the default "Skills" heading.
  function groupByCategory(items, dflt) {
    const groups = {}, order = [];
    (items || []).forEach((it) => { const g = it.group || dflt; if (!(g in groups)) { groups[g] = []; order.push(g); } groups[g].push(it); });
    return { groups, order };
  }
  function tabSkills(v, p) {
    let h = renderRules(v.rules || {});
    const sk = p.skills || [];
    if (!sk.length) h += `<div class="aes-hud-empty">No skills yet.</div>`;
    else {
      const { groups, order } = groupByCategory(sk, "Skills");
      const solo = order.length === 1 && order[0] === "Skills";
      for (const g of order) {
        h += sechdr(solo ? "Skills — your competencies (the modifier you roll)" : g);
        h += `<div class="aes-skills">${groups[g].map(skillRowHtml).join("")}</div>`;
      }
    }
    if ((v.rolls || []).length) h += sechdr("Recent checks") + `<div class="aes-rows2">${v.rolls.slice().reverse().map((r) => `<div class="aes-roll"><span>${esc(r.skill || r.spec || "roll")} = <b>${esc(r.result)}</b>${r.mod != null ? ` <span class="aes-dim">(mod ${r.mod >= 0 ? "+" : ""}${esc(r.mod)})</span>` : ""}</span>${r.tier_label ? `<span class="t ${esc(r.tier)}">${esc(r.tier_label)}</span>` : ""}${r.note ? `<div class="aes-roll-note">↳ ${esc(r.note)}</div>` : ""}</div>`).join("")}</div>`;
    return h;
  }
  function tabAbilities(v, p) {
    const abils = p.abilities || [];
    if (!abils.length) return `<div class="aes-hud-empty">No abilities yet. Abilities bend the dice — earn them in-world or author them in the Creator.</div>`;
    // free-form categories (Bean 2026-07-07): the built-in three sort first, any custom
    // category the player authored ("Cyber-Ware", "Spells", …) follows with its own header.
    const { groups, order } = groupByCategory(abils, "talent");
    const KNOWN = { spell: 0, technique: 1, talent: 2 };
    order.sort((a, b) => (KNOWN[a] != null ? KNOWN[a] : 3) - (KNOWN[b] != null ? KNOWN[b] : 3));
    const GH = { spell: "✨ Spells", technique: "⚡ Techniques — active, you invoke them", talent: "🜂 Talents — passive, always on" };
    let h = "";
    for (const g of order) {
      h += sechdr(GH[g] || ("❖ " + g.charAt(0).toUpperCase() + g.slice(1))) + groups[g].map((a) => renderAbility(a)).join("");
    }
    const ms = (v.rules || {}).mechanics || [];
    if (ms.length) h += `<div class="aes-rules"><div class="aes-rules-h">What the mechanics mean</div>${ms.map((m) => `<div class="aes-mech"><b>${esc(m.mechanic)}</b> — ${esc(m.label)}</div>`).join("")}<div class="aes-rules-note">Invoke an active in a check: <code>((aether.check &lt;skill&gt; use &lt;ability&gt;))</code></div></div>`;
    return h;
  }
  function renderAbility(a) {
    const badge = a.active ? `<span class="aes-tag act">ACTIVE</span>` : `<span class="aes-tag">passive</span>`;
    const bits = [];
    bits.push(a.applies_to && a.applies_to !== "all checks" ? "on " + esc(a.applies_to) : "any check");
    if (a.cost) bits.push("costs " + esc(a.cost));
    if (a.cooldown) bits.push(a.on_cd ? `recharging ${esc(a.on_cd)}t` : `cooldown ${esc(a.cooldown)}t`);
    return `<div class="aes-abil ${a.active ? "act" : ""} ${a.on_cd ? "cd" : ""}"><div class="aes-abil-h"><b>${esc(a.name)}</b> ${badge}</div>${a.mechanic_label ? `<div class="aes-abil-mech">${esc(a.mechanic_label)}</div>` : ""}${bits.length ? `<div class="aes-abil-meta">${bits.join(" · ")}</div>` : ""}${a.desc ? `<div class="aes-abil-desc">${esc(a.desc)}</div>` : ""}</div>`;
  }
  // a sensible free slot for stowed gear (2026-07-09): its native slot when free, else the
  // first empty of a type-appropriate preference list — so every stowed piece has an equip
  // button instead of only slot-tagged ones (the patch kit / dive light were dead rows).
  function freeSlotFor(p, i) {
    const slots = p.gear_slots || [];
    const empty = new Set(slots.filter((s) => !s.item).map((s) => s.slot));
    if (i.slot && empty.has(i.slot)) return i.slot;
    const pref = i.type === "weapon" ? ["mainhand", "offhand", "waist"]
      : i.type === "container" ? ["back", "waist"]
      : ["waist", "back", "neck", "offhand", "hands"];
    for (const s of pref) if (empty.has(s)) return s;
    return i.slot || "waist";
  }
  function stowedRows(p, hdr) {
    const stow = p.stowed_gear || [];
    if (!stow.length) return "";
    return sechdr(hdr) + stow.map((c) => `<div class="aes-invrow"><b>${esc(c.container)}:</b> ${c.items.map((i) => { const sl = freeSlotFor(p, i); return `<span class="aes-inv">${esc((i.qty > 1 ? i.qty + "\u00d7 " : "") + i.name)}${i.type ? ` <span class="aes-dim">${esc(i.type)}</span>` : ""}${actBtn("equip", [{ op: "item_equip", instance: i.iid, slot: sl }], "wear/wield \u2192 " + sl, "mini")}</span>`; }).join(" ")}</div>`).join("");
  }
  function tabGear(v, p) {
    // Gear = weapons, armor, tools, accessories, bags. Equipped on the paper-doll; the rest
    // stowed but still gear (a sheathed sword is not "inventory"). Consumables live in 🎒 Items.
    let h = renderPaperdoll(p);
    const stow = p.stowed_gear || [];
    h += stowedRows(p, "Stowed gear — carried, ready to equip");
    if (!(p.gear_slots || []).some((s) => s.item) && !stow.length)
      h += `<div class="aes-kv" style="opacity:.5;margin-top:6px">no gear yet — weapons, armor & tools show here</div>`;
    return h;
  }
  function tabInventory(v, p) {
    // Inventory = everything that isn't gear: consumables, materials, devices, keepsakes.
    const inv = p.inventory || [];
    const stowed = stowedRows(p, "⚔ Stowed gear — also carried (equips on the Gear tab too)");
    if (!inv.length && !stowed) return `<div class="aes-hud-empty">Nothing carried. Consumables, materials & odds-and-ends show here — worn gear lives under ⚔ Gear.</div>`;
    if (!inv.length) return stowed;
    return stowed + sechdr("🎒 Inventory — carried items") + inv.map((c) => `<div class="aes-invrow"><b>${esc(c.container)}:</b> ${c.items.map((i) => `<span class="aes-inv">${esc((i.qty > 1 ? i.qty + "× " : "") + i.name)}${i.type ? ` <span class="aes-dim">${esc(i.type)}</span>` : ""}${i.consumable ? actBtn("use", [{ op: "item_consume", instance: i.iid }], "consume one", "mini") : ""}${i.slot ? actBtn("equip", [{ op: "item_equip", instance: i.iid, slot: i.slot }], "wear/wield", "mini") : ""}</span>`).join(" ")}</div>`).join("");
  }
  function renderPaperdoll(p) {
    const slots = p.gear_slots || [];
    if (!slots.length) return `<div class="aes-hud-empty">No equip slots.</div>`;
    const groups = { weapon: [], armor: [], trinket: [] };
    slots.forEach((s) => (groups[s.kind] || groups.armor).push(s));
    const GH = { weapon: "⚔ Weapons", armor: "🛡 Armor", trinket: "💍 Trinkets" };
    let h = sechdr("Equipped — worn on the body") + `<div class="aes-doll">`;
    for (const g of ["weapon", "armor", "trinket"]) {
      if (!groups[g].length) continue;
      h += `<div class="aes-doll-gh">${GH[g]}</div>`;
      h += groups[g].filter((s) => s.item).map((s) => {
        const it = s.item;
        return `<div class="aes-slot filled ${g}"><span class="aes-slot-l">${esc(s.label)}</span><span class="aes-slot-i">${esc(it.name)}${it.mods ? ` <span class="m">${esc(it.mods)}</span>` : ""}</span>${actBtn("✕", [{ op: "item_unequip", instance: it.iid }], "take off", "x")}</div>`;
      }).join("");
      const empties = groups[g].filter((s) => !s.item);   // 2026-07-09: 12 "— empty —" rows
      if (empties.length) h += `<div class="aes-slot-empties">open: ${empties.map((s) => esc(s.label)).join(" · ")}</div>`;   // said nothing — one line says it all
    }
    return h + `</div>`;
  }
  function tabStatus(v, p) {
    let h = sechdr("Statuses · Conditions · Diseases");
    if ((p.effects || []).length) h += `<div class="aes-rows">${p.effects.map((e) => { const cls = e.valence === "positive" ? "pos good" : e.valence === "negative" ? "neg bad" : "neu"; return `<span class="aes-pill ${cls}" title="${esc(e.kind_label + (e.note ? " · " + e.note : "") + (e.mods ? " · " + e.mods : ""))}"><span class="g">${esc(e.glyph)}</span> ${esc(e.name)} <span class="aes-tag">${esc(e.kind_label)}</span>${e.stacks > 1 ? " ×" + e.stacks : ""}${e.remaining != null ? ` <span class="m">${e.remaining}t</span>` : ""}${actBtn("×", [{ op: "effect_remove", char: p.eid, effect: e.key }], "remove", "x")}</span>`; }).join("")}</div>`;
    else h += `<div class="aes-kv" style="opacity:.55">none active — you're unharmed and unafflicted</div>`;
    return h;
  }
  function tabWorld(v, p) {
    const parts = [], s = v.scene || {};
    const sceneBits = [];
    if (s.location) sceneBits.push("📍 " + esc(String(s.location).replace(/_/g, " ")));
    const tod = [s.time_of_day, s.day ? ("day " + s.day) : ""].filter(Boolean).join(", ");
    if (tod) sceneBits.push("🕓 " + esc(tod));
    if (s.phase) sceneBits.push("phase " + esc(s.phase));
    if ((s.present || []).length) sceneBits.push("👥 " + s.present.map(esc).join(", "));
    if (sceneBits.length) parts.push(sec("Scene", "🗺", `<div class="aes-kv">${sceneBits.join(" · ")}</div>`));
    if ((v.cast || []).length) parts.push(renderCast(v.cast));
    if ((v.quests || []).length) parts.push(sec("Quests", "🎯", v.quests.map((q) => `<div class="aes-quest ${q.status !== "active" ? "done" : ""}"><b>${esc(q.name)}</b>${q.stakes ? " (" + esc(q.stakes) + ")" : ""}${q.status !== "active" ? " — " + esc(q.status.toUpperCase()) : (q.note ? " — " + esc(q.note) : "")}</div>`).join("")));
    const rel = [...(v.relations || []).map((r) => `<span class="aes-pill">${esc(r.name)} <span class="m">${esc(r.tier)}</span></span>`), ...(v.factions || []).map((f) => `<span class="aes-pill">⚑ ${esc(f.name)} <span class="m">${esc(f.tier)}</span></span>`)];
    if (rel.length) parts.push(sec("Relations & Factions", "♥", `<div class="aes-rows">${rel.join("")}</div>`));
    if ((v.relationships || []).length) parts.push(sec("Relationships", "🔗", v.relationships.map((r) => `<div class="aes-kv"><b>${esc(r.a)} → ${esc(r.b)}</b> ${r.dims.map((d) => `<span class="aes-dim">${esc(d.dim)} ${d.val >= 0 ? "+" : ""}${esc(d.val)}</span>`).join(" ")}</div>`).join("")));
    if (Object.keys(v.world_flags || {}).length) parts.push(sec("World", "🌍", `<div class="aes-rows">${Object.entries(v.world_flags).map(([k, val]) => `<span class="aes-pill">${esc(k)}=${esc(String(val))}</span>`).join("")}</div>`));
    if ((v.memories || []).length) parts.push(sec("Recent events", "📜", v.memories.map((m) => `<div class="aes-kv"><span class="m">t${esc(m.turn)}</span> ${esc(m.text)}</div>`).join("")));
    if ((v.consent || []).length) parts.push(sec("Consent", "✔", v.consent.map((c) => `<div class="aes-kv"><b>${esc(c.pair)}</b> · ${esc(c.category)} <span class="m">${esc(c.level)}</span>${c.cap != null ? " ≤" + esc(c.cap) : ""}</div>`).join("")));
    return parts.join("") || `<div class="aes-hud-empty">The world is quiet.</div>`;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => { drawPanel(); buildHud(); });
  else { drawPanel(); buildHud(); }

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
      cmd("aether-spec", async (_n, value) => {
        const name = String(value || "").trim().toLowerCase();
        if (!name) {                                   // no arg -> report current
          try { const d = await api("/aether/specialization");
                return `specialization: ${d.name}`; }
          catch (e) { return "AetherState: offline"; }
        }
        if (name !== "none" && name !== "rpg") return "usage: /aether-spec none|rpg";
        const d = await api("/aether/specialization", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ name }),
        });
        return d && d.name ? `specialization: ${d.name}` : "failed to set specialization";
      }, "Switch narrative mode: none (default chat-RP) or rpg (Dungeon-Master mode).",
         "none|rpg", false);
      cmd("aether-creator", async () => {
        const url = settings.proxy_url + "/aether/creator?session=" + encodeURIComponent(sid());
        try { window.open(url, "_blank"); } catch (e) { return "open " + url; }
        return "opening the World & Character creator\u2026";
      }, "Open the AetherState World Generator & Character Creator window.");
      cmd("aether-hud", async () => {
        try { toggleHud(); return hudVisible() ? "player HUD opened" : "player HUD closed"; }
        catch (e) { return "HUD unavailable"; }
      }, "Toggle the movable player HUD (stats, statuses, drives, gear, dice, scene).");
      console.log("[AetherState] slash commands registered");
    }
  } catch (e) { console.warn("[AetherState] slash registration failed", e); }
})();
