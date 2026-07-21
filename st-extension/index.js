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
  console.log("[AetherState] Companion loaded — combat-reference/composer build + world-overlay (2026-07-17) + HUD clarity (2026-07-18) + card-seed reliability (2026-07-21)");
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
  const mintSid = () => "st-" + Math.random().toString(36).slice(2, 12);
  const safeLineageSid = (value) => {
    const v = String(value || "").trim();
    return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v) ? v : "";
  };
  function ensureChatIdentity() {
    try {
      const c = C();
      const meta = c.chatMetadata || {};
      try { if (!c.chatMetadata) c.chatMetadata = meta; } catch (e) {}
      const chatId = c.chatId == null ? "" : String(c.chatId).trim();
      const boundChatId = String(meta.aetherstate_chat_id || "").trim();
      const derived = Boolean(meta.main_chat);
      let changed = false;

      // SillyTavern branches copy the whole parent metadata object. main_chat identifies that
      // copied snapshot; a different chatId proves this is a new branch, not a reopen. Rotate
      // once, retain the copied session as the explicit parent, and freeze the exact snapshot
      // length. A nested branch naturally repeats this with its immediate child as the parent.
      if (!meta.aetherstate_sid) {
        // A pre-extension branch has no parent AetherState identity to inherit. Give it one
        // stable standalone identity and bind it immediately rather than minting twice.
        meta.aetherstate_sid = mintSid();
        if (chatId) meta.aetherstate_chat_id = chatId;
        delete meta.aetherstate_parent_sid;
        delete meta.aetherstate_fork_pos;
        changed = true;
      } else if (derived && chatId && boundChatId !== chatId) {
        const parentSid = safeLineageSid(meta.aetherstate_sid);
        meta.aetherstate_sid = mintSid();
        if (parentSid) meta.aetherstate_parent_sid = parentSid;
        else delete meta.aetherstate_parent_sid;
        meta.aetherstate_fork_pos = Array.isArray(c.chat) ? c.chat.length : 0;
        meta.aetherstate_chat_id = chatId;
        changed = true;
      } else {
        if (!derived && chatId && boundChatId !== chatId) {
          meta.aetherstate_chat_id = chatId;
          changed = true;
        }
        if (!derived) {
          if (Object.prototype.hasOwnProperty.call(meta, "aetherstate_parent_sid")) {
            delete meta.aetherstate_parent_sid;
            changed = true;
          }
          if (Object.prototype.hasOwnProperty.call(meta, "aetherstate_fork_pos")) {
            delete meta.aetherstate_fork_pos;
            changed = true;
          }
        }
      }
      if (changed) { try { c.saveMetadataDebounced(); } catch (e) {} }

      const parent = safeLineageSid(meta.aetherstate_parent_sid);
      const rawFork = meta.aetherstate_fork_pos;
      const forkPos = Number.isInteger(rawFork) && rawFork >= 0 ? rawFork
        : (/^\d+$/.test(String(rawFork ?? "")) ? Number(rawFork) : null);
      return { session: String(meta.aetherstate_sid || "st-unknown"), derived,
               parent, forkPos, chatId };
    } catch (e) {
      return { session: "st-unknown", derived: false, parent: "", forkPos: null, chatId: "" };
    }
  }
  const sid = () => ensureChatIdentity().session;
  const isDerivedChat = () => ensureChatIdentity().derived;
  const cardLifecycleSource = (character) => String(
    character?.avatar || character?.data?.avatar || character?.avatar_url
      || character?.data?.avatar_url || "",
  ).trim();
  const cardLifecycleFingerprint = (character) => {
    const ext = character?.data?.extensions?.aetherstate || character?.extensions?.aetherstate;
    return String(ext?.seed_fingerprint || "").trim();
  };
  function captureCardLifecycleContext(identity = null) {
    const cx = C();
    const exactIdentity = identity || ensureChatIdentity();
    const characterId = cx.characterId;
    return {
      session: exactIdentity.session,
      derived: Boolean(exactIdentity.derived),
      chatId: String(exactIdentity.chatId || cx.chatId || ""),
      characterId,
      character: cx.characters?.[characterId] || null,
      cardName: String(cx.characters?.[characterId]?.name || ""),
      cardSource: cardLifecycleSource(cx.characters?.[characterId]),
      seedFingerprint: cardLifecycleFingerprint(cx.characters?.[characterId]),
    };
  }
  function cardLifecycleIsCurrent(origin) {
    if (!origin) return true;
    const cx = C(), identity = ensureChatIdentity();
    if (identity.session !== origin.session
        || String(identity.chatId || cx.chatId || "") !== origin.chatId) return false;
    const active = cx.characters?.[cx.characterId] || null;
    if (origin.characterId !== undefined && origin.characterId !== null
        && cx.characterId !== origin.characterId) return false;
    if (!active) return true;
    const activeSource = cardLifecycleSource(active);
    const activeFingerprint = cardLifecycleFingerprint(active);
    const sourceComparable = Boolean(origin.cardSource && activeSource);
    const fingerprintComparable = Boolean(origin.seedFingerprint && activeFingerprint);
    if (sourceComparable && activeSource !== origin.cardSource) return false;
    if (fingerprintComparable && activeFingerprint !== origin.seedFingerprint) return false;
    // ST can replace a character object while hydrating it, so object identity is not stable.
    // When neither durable card field is comparable yet, the visible name is the remaining shell
    // identity: a different name means a card switch, while the same name permits hydration.
    if (!sourceComparable && !fingerprintComparable && origin.cardName
        && String(active.name || "") && String(active.name || "") !== origin.cardName) return false;
    return true;
  }
  function bindCardLifecycleCharacter(origin) {
    if (!origin || !cardLifecycleIsCurrent(origin)) return null;
    const cx = C(), active = cx.characters?.[cx.characterId] || null;
    if (active) {
      origin.characterId = cx.characterId;
      origin.character = active;
      if (!origin.cardName) origin.cardName = String(active.name || "");
      if (!origin.cardSource) origin.cardSource = cardLifecycleSource(active);
      if (!origin.seedFingerprint) origin.seedFingerprint = cardLifecycleFingerprint(active);
    }
    return active;
  }
  const staleCardLifecycle = () => ({
    ok: true, requested: false, structuredSeed: false, stale: true, skipped: "active chat changed",
  });
  const guardName = () => {
    try { return settings.guard.override_name || C().substituteParams("{{user}}"); }
    catch (e) { return ""; }
  };
  const speaker = () => {
    try { const c = C(); return c.characters?.[c.characterId]?.name || ""; } catch (e) { return ""; }
  };
  const cardRole = () => {
    try {
      const c = C(), ch = c.characters?.[c.characterId];
      const ext = ch?.data?.extensions?.aetherstate || ch?.extensions?.aetherstate;
      return String(ext?.role || "").trim().toLowerCase().replace(/[^a-z_-]/g, "").slice(0, 32);
    } catch (e) { return ""; }
  };
  const sentinel = () => {
    const identity = ensureChatIdentity();
    const lineage = identity.parent && identity.forkPos !== null
      ? `parent=${identity.parent};fork=${identity.forkPos};` : "";
    return `<<AETHER:v=1;session=${identity.session};${lineage}` +
      `turn=${turnCounter};type=${lastGenType};speaker=${speaker()};` +
      `card_role=${cardRole()};user=${guardName()}>>`;
  };

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
  async function doGenesis(reason, force = false, ifearly = false,
                           structuredSeed = false, seedFingerprint = "", lifecycleContext = null) {
    if (!settings.enabled) return { error: "extension disabled" };
    const origin = lifecycleContext || captureCardLifecycleContext();
    if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    const sub = (t) => { try { return C().substituteParams(t || ""); } catch (e) { return t || ""; } };
    let ch = bindCardLifecycleCharacter(origin), cx = C();  // CHAT_CHANGED can fire before the
    for (let i = 0; i < 8 && !ch; i++) {                    // character is loaded — retry briefly
      if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
      cx = C(); ch = cx.characters?.[cx.characterId];
      if (ch) bindCardLifecycleCharacter(origin);
      if (!ch) await new Promise((r) => setTimeout(r, 250));
    }
    if (!ch) { console.warn("[AetherState] genesis: no active character"); return { error: "no character" }; }
    // Dialogue examples demonstrate style; they are not card/world facts. Feeding them to
    // genesis once caused a generic example NPC to be committed as real cast.
    const card = [sub(ch.description), sub(ch.personality), sub(ch.scenario)]
      .filter(Boolean).join("\n").trim();
    // 2026-07-06: prefer the greeting actually SHOWN in chat — message.mes reflects the
    // current swipe, so alternative greetings seed correctly. first_mes is the fallback.
    let greeting = "";
    try { const m = (cx.chat || []).find((x) => !x.is_user && x.mes); if (m) greeting = sub(m.mes); } catch (e) {}
    if (!greeting) greeting = sub(ch.first_mes || "");
    if (!card && !greeting) { console.warn("[AetherState] genesis: empty card+greeting"); return { error: "empty card" }; }
    if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    const S = origin.session;
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 8000);        // seeding is fast (Stage B is async)
      const q = [force ? "force=1" : "", ifearly ? "ifearly=1" : ""].filter(Boolean).join("&");
      const verifiedFingerprint = structuredSeed
        && /^sha256:[0-9a-f]{64}$/.test(seedFingerprint) ? seedFingerprint : "";
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/genesis${q ? "?" + q : ""}`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ card, greeting, speaker: ch.name || "", card_role: cardRole(),
                               user: guardName(), opening: "",
                               structured_seed: Boolean(verifiedFingerprint),
                               seed_fingerprint: verifiedFingerprint }),
      });
      clearTimeout(t);
      const d = await r.json().catch(() => ({}));
      console.log(`[AetherState] genesis (${reason}) sid=${S} ->`, d);
      if (cardLifecycleIsCurrent(origin)) { try { refreshChip(); } catch (e) {} }
      return d;
    } catch (e) {
      if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
      console.warn("[AetherState] genesis fetch failed", e); return { error: String(e) };
    }
  }
  function genesisAtChatOpen(identity = null) {
    const origin = captureCardLifecycleContext(identity);
    if (origin.derived) return;
    seedThenGenesis("chat_open", false, false, origin).catch(() => {});
  }
  // 2026-07-06: swiping the FIRST message picks a different opening — re-seed so state
  // reflects the greeting the player actually chose. ifearly=1 makes the proxy refuse
  // once real turns exist, so an established chat is never disturbed.
  function genesisAtGreetingSwipe(i) {
    try {
      if (isDerivedChat()) return;
      const origin = captureCardLifecycleContext();
      const chat = C().chat || [];
      const first = chat.findIndex((x) => !x.is_user);
      if (Number(i) !== first || first < 0) return;
      setTimeout(() => {
        if (!cardLifecycleIsCurrent(origin)) return;
        seedThenGenesis("greeting_swipe", true, true, origin).catch(() => {});
      }, 400);
    } catch (e) {}
  }

  // ---- card-seed auto-apply (2026-07-08, REQUIRED for a smooth Creator flow): a Narrator card
  // built by the Creator carries the whole world + Player Card in extensions.aetherstate.seed.
  // On chat-open we hand that seed to the proxy, which commits it to THIS session's ledger —
  // deterministically, no LLM. This is what removes "you have to re-apply the world to every
  // new chat": import the card, open a chat, and the world + your character are already there.
  // The /seed route is receipt-idempotent for one exact card fingerprint, so retries/reopens can
  // confirm that exact durable source without accepting a same-name world/player. It runs BEFORE
  // genesis. A rejected portable seed must not be disguised as successful structured genesis.
  function cardSeedEnvelope(lifecycleContext = null) {
    try {
      const cx = C();
      if (lifecycleContext && !cardLifecycleIsCurrent(lifecycleContext)) return null;
      const ch = lifecycleContext
        ? (bindCardLifecycleCharacter(lifecycleContext) || lifecycleContext.character)
        : cx.characters?.[cx.characterId];
      const ext = ch?.data?.extensions?.aetherstate || ch?.extensions?.aetherstate;
      const seed = ext && ext.seed;
      if (!seed || !(seed.world || seed.player)) return null;
      return { seed, seedFingerprint: String(ext?.seed_fingerprint || "").trim() };
    } catch (e) { return null; }
  }
  const reportedCardSeedFailures = new Map();
  function cardSeedFailureReason(data, fallback = "seed admission was not confirmed") {
    const rejected = Array.isArray(data?.rejected) ? data.rejected : [];
    const raw = data?.error || data?.detail || data?.message || rejected[0]?.reason || fallback;
    return String(raw || fallback).replace(/\s+/g, " ").trim().slice(0, 180);
  }
  function reportCardSeedFailure(S, reason,
                                 recovery = "Re-export this Narrator from Creator, then run /aether-genesis.") {
    const concise = cardSeedFailureReason(null, reason);
    if (reportedCardSeedFailures.get(S) === concise) return;
    reportedCardSeedFailures.set(S, concise);
    const message = `Card seed failed: ${concise}. ${recovery}`;
    console.warn("[AetherState] " + message);
    try {
      if (globalThis.toastr) toastr.error(message, "AetherState card seed needs recovery");
    } catch (e) {}
  }
  const reportedCardSeedConfirmations = new Set();
  function reportCardSeedConfirming(S) {
    if (reportedCardSeedConfirmations.has(S)) return;
    reportedCardSeedConfirmations.add(S);
    try {
      if (globalThis.toastr?.info) {
        toastr.info(
          "The seed is still being confirmed; your new chat is safe to leave open.",
          "AetherState is finishing setup",
        );
      }
    } catch (e) {}
  }
  function cardSeedPostconditions(data, requestedWorld, requestedPlayer, seedFingerprint) {
    const rejected = Array.isArray(data?.rejected) ? data.rejected : [];
    const quarantined = Array.isArray(data?.quarantined)
      ? data.quarantined.length > 0 : Boolean(data?.quarantined);
    return data?.complete === true && rejected.length === 0 && !quarantined
      && data?.seed_fingerprint === seedFingerprint
      && (!requestedWorld || data?.world_seeded === true)
      && (!requestedPlayer || data?.player_seeded === true);
  }
  function cardSeedExplicitlyRejected(data) {
    const rejected = Array.isArray(data?.rejected) ? data.rejected : [];
    const quarantined = Array.isArray(data?.quarantined)
      ? data.quarantined.length > 0 : Boolean(data?.quarantined);
    return rejected.length > 0 || quarantined
      || Boolean(String(data?.error || data?.detail || "").trim());
  }
  function cardSeedStatusUrl(S, seedFingerprint) {
    return settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/seed-status`
      + `?seed_fingerprint=${encodeURIComponent(seedFingerprint)}`;
  }
  function cardSeedStatusConfirmed(data, requestedWorld, requestedPlayer, seedFingerprint) {
    return data?.world_requested === requestedWorld
      && data?.player_requested === requestedPlayer
      && cardSeedPostconditions(data, requestedWorld, requestedPlayer, seedFingerprint);
  }
  async function reconcileCardSeed(S, seedFingerprint, requestedWorld, requestedPlayer,
                                   lifecycleContext = null) {
    // Losing the browser response does not prove that the deterministic server commit failed.
    // Poll a dedicated NO-WRITE postcondition instead of sending the mutating seed a second time.
    // The server owns World/Player identity normalization, so the extension never guesses it.
    const deadline = Date.now() + 90000;
    let lastData = null;
    let lastReason = "the committed seed could not be confirmed";
    while (Date.now() < deadline) {
      if (lifecycleContext && !cardLifecycleIsCurrent(lifecycleContext))
        return { ...staleCardLifecycle(), requested: true };
      let timer = null;
      try {
        const ac = new AbortController();
        const remaining = Math.max(1, deadline - Date.now());
        timer = setTimeout(() => ac.abort(), Math.min(45000, remaining));
        const r = await fetch(cardSeedStatusUrl(S, seedFingerprint),
          { method: "GET", signal: ac.signal });
        let d = null;
        let parsed = true;
        try {
          d = await r.json();
        } catch (e) {
          // A status body can itself be truncated while the original commit continues. Keep
          // polling this exact immutable receipt until the bounded deadline instead of treating
          // a transport-level parse failure as a negative admission result.
          lastReason = "the seed receipt status response was incomplete";
          parsed = false;
        }
        if (parsed) {
          lastData = d;
          if (r.ok && cardSeedStatusConfirmed(
            d, requestedWorld, requestedPlayer, seedFingerprint,
          )) {
            console.log(`[AetherState] card-seed sid=${S} confirmed from committed state ->`, d);
            if (!lifecycleContext || cardLifecycleIsCurrent(lifecycleContext)) {
              try { refreshChip(); } catch (e) {}
            }
            reportedCardSeedFailures.delete(S);
            return { ok: true, requested: true, structuredSeed: true,
                     applied: Number.isFinite(Number(d?.applied)) ? Number(d.applied) : 0, data: d,
                     reconciled: true, seed_fingerprint: seedFingerprint };
          }
          lastReason = cardSeedFailureReason(d,
            !r.ok ? `status returned HTTP ${r.status || "error"}`
              : "the committed seed did not satisfy its postconditions");
          // Invalid criteria cannot become valid by waiting. A missing receipt can: the original
          // POST may still be ahead of this status request in the server queue.
          if ([400, 401, 403, 409, 422].includes(Number(r.status))
              || (r.ok && d?.seed_fingerprint
                && d.seed_fingerprint !== seedFingerprint)
              || (r.ok && d?.seed_fingerprint === seedFingerprint
                && d?.complete === false && d?.pending !== true)) break;
        }
      } catch (e) {
        lastReason = e?.name === "AbortError"
          ? "the committed seed check timed out" : String(e?.message || e);
      } finally {
        if (timer !== null) clearTimeout(timer);
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000, remaining)));
    }
    return { ok: false, requested: true, structuredSeed: false,
             error: lastReason, data: lastData, reconciled: false };
  }
  async function seedFromCard(lifecycleContext = null) {
    if (!settings.enabled)
      return { ok: false, requested: false, structuredSeed: false, error: "extension disabled" };
    const origin = lifecycleContext || captureCardLifecycleContext();
    if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    if (origin.derived)
      return { ok: true, requested: false, structuredSeed: false, skipped: "derived chat" };

    let envelope = null, metadataReady = false, cx = C();
    for (let i = 0; i < 8 && !metadataReady; i++) {          // CHAT_CHANGED can fire after the
      if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
      cx = C();                                              // character shell but before its
      const ch = bindCardLifecycleCharacter(origin);         // extension metadata is attached.
      const aes = ch?.data?.extensions?.aetherstate || ch?.extensions?.aetherstate;
      if (aes) { metadataReady = true; envelope = cardSeedEnvelope(origin); break; }
      await new Promise((r) => setTimeout(r, 250));
    }
    if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    if (!envelope) envelope = cardSeedEnvelope(origin);
    if (!envelope)
      return { ok: true, requested: false, structuredSeed: false, skipped: "no card seed" };

    const { seed, seedFingerprint } = envelope;
    const requestedWorld = Boolean(seed.world), requestedPlayer = Boolean(seed.player);
    const requested = requestedWorld || requestedPlayer;
    const S = origin.session;
    if (!/^sha256:[0-9a-f]{64}$/.test(seedFingerprint)) {
      const reason = "the Narrator card is missing a valid seed fingerprint";
      reportCardSeedFailure(S, reason);
      return { ok: false, requested, structuredSeed: false, error: reason };
    }
    let t = null;
    try {
      const ac = new AbortController();
      t = setTimeout(() => ac.abort(), 6000);
      const r = await fetch(settings.proxy_url.replace(/\/$/, "") + `/aether/session/${S}/seed`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({ seed, seed_fingerprint: seedFingerprint }),
      });
      let d = {};
      try {
        d = await r.json();
      } catch (e) {
        // A successful HTTP status with a lost/truncated body is still commit-ambiguous.
        if (r.ok || Number(r.status) >= 500) throw e;
      }
      if (Number(r.status) >= 500) {
        const ambiguous = new Error(`seed endpoint returned HTTP ${r.status}`);
        ambiguous.name = "SeedCommitAmbiguous";
        throw ambiguous;
      }
      console.log(`[AetherState] card-seed sid=${S} ->`, d);
      if (r.ok && d?.seed_fingerprint !== seedFingerprint) {
        const ambiguous = new Error("seed response did not echo the requested fingerprint");
        ambiguous.name = "SeedCommitAmbiguous";
        throw ambiguous;
      }
      const applied = Number(d?.applied);
      const verified = r.ok && cardSeedPostconditions(
        d, requestedWorld, requestedPlayer, seedFingerprint,
      )
        && Number.isFinite(applied) && applied >= 0;
      if (r.ok && !verified && !cardSeedExplicitlyRejected(d)) {
        // Valid JSON can still be a clipped success response. Only an explicit rejection is
        // terminal; otherwise confirm the immutable receipt through the read-only status route.
        const ambiguous = new Error("seed response did not prove every required postcondition");
        ambiguous.name = "SeedCommitAmbiguous";
        throw ambiguous;
      }
      if (!verified) {
        const reason = cardSeedFailureReason(d,
          !r.ok ? `server returned HTTP ${r.status || "error"}` : "seed admission was not confirmed");
        reportCardSeedFailure(S, reason);
        return { ok: false, requested, structuredSeed: false, error: reason, data: d };
      }
      reportedCardSeedFailures.delete(S);
      if (cardLifecycleIsCurrent(origin)) { try { refreshChip(); } catch (e) {} }
      return { ok: true, requested, structuredSeed: true, applied, data: d,
               seed_fingerprint: seedFingerprint };
    } catch (e) {
      if (t !== null) { clearTimeout(t); t = null; }
      if (!cardLifecycleIsCurrent(origin)) return { ...staleCardLifecycle(), requested };
      reportCardSeedConfirming(S);
      const reconciled = await reconcileCardSeed(
        S, seedFingerprint, requestedWorld, requestedPlayer, origin,
      );
      if (reconciled.ok) return reconciled;
      if (reconciled.stale) return reconciled;
      const transport = e?.name === "AbortError"
        ? "the seed request timed out" : String(e?.message || e);
      const reason = `${transport}; ${reconciled.error || "the committed seed could not be confirmed"}`;
      reportCardSeedFailure(
        S,
        reason,
        "Keep this chat open, restore the AetherState connection, then run /aether-genesis.",
      );
      return { ok: false, requested, structuredSeed: false, error: reason,
               data: reconciled.data };
    } finally {
      if (t !== null) clearTimeout(t);
    }
  }
  async function seedThenGenesis(reason, force = false, ifearly = false, lifecycleContext = null) {
    const origin = lifecycleContext || captureCardLifecycleContext();
    if (!cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    const seeded = await seedFromCard(origin);
    if (seeded.stale || !cardLifecycleIsCurrent(origin)) return staleCardLifecycle();
    if (!seeded.ok && seeded.requested)
      return { error: `card seed failed: ${seeded.error || "admission was not confirmed"}`,
               card_seed: seeded };
    return doGenesis(
      reason, force, ifearly, Boolean(seeded.structuredSeed),
      seeded.structuredSeed ? String(seeded.seed_fingerprint || "") : "",
      origin,
    );
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

  function promptMessageText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    return content.map((part) => {
      if (typeof part === "string") return part;
      if (part && typeof part.text === "string") return part.text;
      return "";
    }).join("\n");
  }

  const normalizedPromptText = (text) => String(text || "").replace(/\s+/g, " ").trim();

  const playerContextGenerationTypes = new Set(["normal", "swipe", "regenerate"]);
  // Only these SillyTavern generation types can produce a visible Narrator reply. Quiet prompts
  // and impersonation are separate client jobs; treating them as Narrator work can leave this
  // HUD-only timer active even though the visible reply already arrived.
  const narratorPulseGenerationTypes = new Set(["normal", "swipe", "regenerate", "continue"]);
  const normalizedGenerationType = (type) => String(type == null ? "" : type).trim().toLowerCase();
  const isNarratorPulseGeneration = (type) =>
    narratorPulseGenerationTypes.has(normalizedGenerationType(type));

  // ---- visible Narrator lifecycle
  // SillyTavern emits GENERATION_STARTED before command handling, connection checks, and prompt
  // assembly. It is only a proposal; GENERATE_AFTER_DATA is the first public event proving that a
  // request survived those gates. ST does not publish a generation ID, so we add a local epoch and
  // require both the expected coordinate and a reply mutation visible in the current chat. Coordinate
  // equality alone is insufficient when A and B activate before either one changes chat length.
  const narratorLifecycleKey = "__aetherstateNarratorPulseLifecycle";
  try { globalThis[narratorLifecycleKey]?.teardown?.("module_replaced"); } catch (e) {}

  let narratorEpoch = 0;
  let narratorPending = null;
  let narratorActive = null;
  let narratorPulseInterval = 0;
  const narratorEventDetachers = [];
  let narratorLifecycleDisposed = false;
  let narratorLifecycleApi = null;
  const narratorPulseMaxMs = 30 * 60 * 1000;

  const narratorChatKey = () => {
    try {
      const cx = C();
      const chatId = cx.chatId == null ? "" : String(cx.chatId);
      const sessionId = cx.chatMetadata?.aetherstate_sid == null
        ? "" : String(cx.chatMetadata.aetherstate_sid);
      return chatId + "\u0000" + sessionId;
    } catch (e) { return ""; }
  };

  const narratorExpectedReply = (type) => {
    try {
      const chat = C().chat;
      if (!Array.isArray(chat)) return { messageIndex: null, terminalLength: null };
      const length = chat.length;
      // By this boundary ST has already deleted the old assistant for regenerate, so its reply is
      // appended like normal. Swipe and continue retain the existing assistant slot.
      const replaces = type === "swipe" || type === "continue";
      const messageIndex = replaces ? Math.max(0, length - 1) : length;
      return { messageIndex, terminalLength: replaces ? length : length + 1 };
    } catch (e) { return { messageIndex: null, terminalLength: null }; }
  };

  const narratorReplySnapshot = (row) => {
    if (!row || typeof row !== "object" || row.is_user === true || row.is_system === true) {
      return null;
    }
    // Keep a fixed set of exact fields without cloning the row or hashing prose. A hash collision
    // must never turn a real completion into a thirty-minute orphan.
    return Object.freeze({
      mes: row.mes,
      swipeId: row.swipe_id,
      swipeCount: Array.isArray(row.swipes) ? row.swipes.length : null,
      swipeInfoCount: Array.isArray(row.swipe_info) ? row.swipe_info.length : null,
      sendDate: row.send_date,
      genStarted: row.gen_started,
      genFinished: row.gen_finished,
    });
  };

  const narratorReplySnapshotChanged = (before, after) => !before || !after
    || before.mes !== after.mes
    || before.swipeId !== after.swipeId
    || before.swipeCount !== after.swipeCount
    || before.swipeInfoCount !== after.swipeInfoCount
    || before.sendDate !== after.sendDate
    || before.genStarted !== after.genStarted
    || before.genFinished !== after.genFinished;

  const narratorReplyAt = (messageIndex) => {
    try {
      const chat = C().chat;
      if (!Array.isArray(chat) || !Number.isInteger(messageIndex)
          || messageIndex < 0 || messageIndex >= chat.length) return null;
      const row = chat[messageIndex];
      const snapshot = narratorReplySnapshot(row);
      return snapshot == null ? null : { row, snapshot };
    } catch (e) { return null; }
  };

  const narratorReplyChangedSinceActivation = (active) => {
    if (!active || active.expectedMessageIndex == null) return false;
    const current = narratorReplyAt(active.expectedMessageIndex);
    if (!current) return false;
    if (!active.activationReply) return true;
    return current.row !== active.activationReply.row
      || narratorReplySnapshotChanged(active.activationReply.snapshot, current.snapshot);
  };

  const hideNarratorPulse = () => {
    try {
      const el = document.getElementById("aes_pulse");
      if (el) el.style.display = "none";
    } catch (e) {}
  };

  const renderNarratorPulse = () => {
    try {
      // A timer callback may already be queued when an old module instance is replaced. A disposed
      // instance must never write the shared DOM owned by the new instance.
      if (narratorLifecycleDisposed) return;
      const active = narratorActive;
      let elapsed = 0;
      if (active) {
        // Re-check the captured epoch before writing so a superseded delayed tick cannot repaint.
        if (!narratorActive || narratorActive.epoch !== active.epoch) return;
        elapsed = Math.max(0, Date.now() - active.startedAt);
        // ST exposes neither a generation-error event nor liveness through getContext(). All local
        // post-request errors normally emit ENDED, but this generous final bound prevents an orphaned
        // browser event from claiming that narration is alive forever. Evaluate it even when the
        // compact/closed HUD has no pulse element, because lifecycle state must still be bounded.
        if (elapsed >= narratorPulseMaxMs) {
          console.warn("[AetherState] retired an orphaned Narrating indicator after 30 minutes");
          retireNarratorPulse();
          return;
        }
      }
      const el = document.getElementById("aes_pulse");
      if (!el) return;
      if (!active) { el.style.display = "none"; return; }
      el.style.display = "";
      el.textContent = "✦ narrating… " +
        Math.max(1, Math.round(elapsed / 1000)) + "s";
      el.title = "the model is generating — reasoning models can think silently for a " +
        "minute or more before the first visible word; the turn is alive";
    } catch (e) {}
  };

  const retireNarratorPulse = () => {
    const hadWork = Boolean(narratorPending || narratorActive);
    narratorPending = null;
    narratorActive = null;
    hideNarratorPulse();
    return hadWork;
  };

  function proposeNarratorPulse(type, opts, dryRun) {
    // Every START supersedes an older pending proposal, including ignored background/dry-run work.
    // Only a proven foreground type supersedes an already active visible reply.
    narratorPending = null;
    if (narratorLifecycleDisposed || !settings.enabled || dryRun) return false;
    const quietPrompt = Boolean(opts?.quiet_prompt);
    const quietToLoud = opts?.quietToLoud === true;
    let normalizedType = normalizedGenerationType(type);
    // Tool recursion can re-enter as `normal` while carrying the original quiet prompt. Preserve
    // that background authority. Conversely, older callers that omit type may explicitly promote
    // a quiet prompt to visible work with quietToLoud.
    if (!normalizedType && quietPrompt && quietToLoud) normalizedType = "normal";
    if (quietPrompt && !quietToLoud) return false;
    if (!narratorPulseGenerationTypes.has(normalizedType)) return false;

    narratorPending = {
      epoch: ++narratorEpoch,
      type: normalizedType,
      chatKey: narratorChatKey(),
    };
    return true;
  }

  function activateNarratorPulse(data, dryRun) {
    if (narratorLifecycleDisposed || dryRun || !narratorPending) return false;
    const pending = narratorPending;
    narratorPending = null;
    if (pending.chatKey !== narratorChatKey()) return false;
    const expected = narratorExpectedReply(pending.type);
    narratorActive = {
      epoch: pending.epoch,
      type: pending.type,
      chatKey: pending.chatKey,
      expectedMessageIndex: expected.messageIndex,
      expectedTerminalLength: expected.terminalLength,
      activationReply: narratorReplyAt(expected.messageIndex),
      startedAt: Date.now(),
    };
    renderNarratorPulse();
    return true;
  }

  function finishNarratorMessage(messageIndex, type) {
    const active = narratorActive;
    if (narratorLifecycleDisposed || !active || !isNarratorPulseGeneration(type)) return false;
    const index = Number(messageIndex);
    // This corroborates that ST changed the predicted reply slot; it is not a request identity.
    // Once two truly concurrent requests touch the same slot, ST's public terminal payload cannot
    // distinguish a late A terminal from B. Ordinary visible sends are serialized by ST's UI lock.
    if (!Number.isInteger(index) || active.expectedMessageIndex == null
        || index !== active.expectedMessageIndex
        || !narratorReplyChangedSinceActivation(active)) return false;
    return retireNarratorPulse();
  }

  function finishNarratorWindow(chatLength) {
    const active = narratorActive;
    if (narratorLifecycleDisposed || !active) return false;
    const length = Number(chatLength);
    let currentLength = null;
    try { currentLength = Array.isArray(C().chat) ? C().chat.length : null; } catch (e) {}
    if (!Number.isInteger(length) || active.expectedTerminalLength == null
        || length !== active.expectedTerminalLength || length !== currentLength
        || !narratorReplyChangedSinceActivation(active)) return false;
    return retireNarratorPulse();
  }

  function teardownNarratorLifecycle() {
    if (narratorLifecycleDisposed) return;
    retireNarratorPulse();
    narratorLifecycleDisposed = true;
    if (narratorPulseInterval) {
      try { clearInterval(narratorPulseInterval); } catch (e) {}
      narratorPulseInterval = 0;
    }
    for (const detach of narratorEventDetachers.splice(0)) {
      try { detach(); } catch (e) {}
    }
    try { globalThis.removeEventListener?.("pagehide", teardownNarratorLifecycle); } catch (e) {}
    try { globalThis.removeEventListener?.("beforeunload", teardownNarratorLifecycle); } catch (e) {}
    try {
      if (globalThis[narratorLifecycleKey] === narratorLifecycleApi) {
        delete globalThis[narratorLifecycleKey];
      }
    } catch (e) {}
  }

  narratorPulseInterval = setInterval(renderNarratorPulse, 1000);
  try { globalThis.addEventListener?.("pagehide", teardownNarratorLifecycle); } catch (e) {}
  try { globalThis.addEventListener?.("beforeunload", teardownNarratorLifecycle); } catch (e) {}
  narratorLifecycleApi = Object.freeze({
    teardown: teardownNarratorLifecycle,
    snapshot: () => Object.freeze({
      epoch: narratorActive?.epoch ?? narratorPending?.epoch ?? null,
      phase: narratorActive ? "active" : narratorPending ? "pending" : "idle",
    }),
  });
  try { globalThis[narratorLifecycleKey] = narratorLifecycleApi; } catch (e) {}

  function recoverCurrentPlayerMessage(data) {
    if (!playerContextGenerationTypes.has(lastGenType) || !Array.isArray(data?.chat)) return false;
    const latest = [...(C().chat || [])].reverse().find((message) =>
      message?.is_user && !message?.is_system
      && typeof message?.mes === "string" && message.mes.trim());
    if (!latest) return false;

    const wanted = normalizedPromptText(latest.mes);
    const finalUser = [...data.chat].reverse().find((message) => message?.role === "user");
    const present = normalizedPromptText(promptMessageText(finalUser?.content));
    if (present && (present === wanted || present.endsWith(wanted) || present.includes(wanted))) {
      return false;
    }

    data.chat.push({ role: "user", content: latest.mes });
    console.warn("[AetherState] final prompt lost the current Player message; recovered it from SillyTavern chat.");
    return true;
  }

  // SillyTavern can serialize duplicate character-editor controls as arrays even though these
  // V2 card fields are scalars. A generated Narrator then fails before the request reaches
  // AetherState (for example, system_prompt.trim is not a function). Repair only the exact
  // duplicate-control shape on AetherState-generated Narrators, in memory, at the earliest
  // generation event. Native array fields such as tags and alternate_greetings are not listed.
  const narratorScalarFields = [
    "name", "description", "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "creator_notes", "creator",
    "character_version",
  ];
  const narratorLegacyScalarFields = new Set([
    "name", "description", "personality", "scenario", "first_mes", "mes_example",
  ]);
  const warnedMalformedNarrators = new WeakSet();

  function duplicatedScalarValue(value) {
    if (!Array.isArray(value)) return { changed: false, ambiguous: false, value };
    if (value.length >= 2 && typeof value[0] === "string"
        && value.slice(1).every((part) => typeof part === "string" && !part.trim())) {
      return { changed: true, ambiguous: false, value: value[0] };
    }
    return { changed: false, ambiguous: true, value };
  }

  function normalizeGeneratedNarratorCard() {
    try {
      const cx = C();
      const ch = cx.characters?.[cx.characterId];
      const aes = ch?.data?.extensions?.aetherstate || ch?.extensions?.aetherstate;
      if (!ch || aes?.generated !== true || String(aes?.role || "").trim().toLowerCase() !== "narrator") {
        return { repaired: [], ambiguous: [] };
      }

      const repaired = [], ambiguous = [];
      const repair = (owner, field, label) => {
        if (!owner || !Object.prototype.hasOwnProperty.call(owner, field)) return;
        const result = duplicatedScalarValue(owner[field]);
        if (result.changed) {
          owner[field] = result.value;
          repaired.push(label);
        } else if (result.ambiguous) ambiguous.push(label);
      };

      for (const field of narratorScalarFields) repair(ch.data, field, `data.${field}`);
      for (const field of narratorLegacyScalarFields) repair(ch, field, field);

      if (repaired.length) {
        console.warn("[AetherState] repaired duplicate SillyTavern scalar fields on generated Narrator card:", repaired);
      }
      if (ambiguous.length) {
        console.warn("[AetherState] generated Narrator has ambiguous array-valued scalar fields; left unchanged:", ambiguous);
      }
      if (globalThis.toastr && !warnedMalformedNarrators.has(ch)) {
        if (ambiguous.length) {
          warnedMalformedNarrators.add(ch);
          toastr.error(
            "This AetherState Narrator card has ambiguous malformed fields. Re-import a fresh card from Creator before generating.",
            "AetherState Narrator card needs recovery",
            { timeOut: 15000, preventDuplicates: true },
          );
        } else if (repaired.length) {
          warnedMalformedNarrators.add(ch);
          toastr.warning(
            "AetherState repaired malformed Narrator fields in memory. Generation can continue; re-import the Creator card to persist a clean copy.",
            "AetherState repaired Narrator card",
            { timeOut: 12000, preventDuplicates: true },
          );
        }
      }
      return { repaired, ambiguous };
    } catch (e) {
      console.warn("[AetherState] Narrator scalar recovery failed open", e);
      return { repaired: [], ambiguous: [] };
    }
  }

  // ---- sentinel injection (05 §4.3): CHAT_COMPLETION_PROMPT_READY, never on dry runs
  try {
    const ev = ctx.eventTypes || ctx.event_types;
    const on = (name, fn) => {
      const eventName = ev?.[name];
      if (!eventName) return () => {};
      // EventSource awaits handlers, but callbacks can already be queued while a module instance is
      // being replaced. Keep old callbacks inert so they cannot mutate the new instance's state/DOM.
      const guarded = (...args) => {
        if (narratorLifecycleDisposed) return;
        return fn(...args);
      };
      ctx.eventSource.on(eventName, guarded);
      const detach = () => {
        try {
          if (typeof ctx.eventSource.off === "function") ctx.eventSource.off(eventName, guarded);
          else ctx.eventSource.removeListener?.(eventName, guarded);
        } catch (e) {}
      };
      narratorEventDetachers.push(detach);
      return detach;
    };
    on("CHAT_COMPLETION_PROMPT_READY", (data) => {          // 05 §4.3
      try {
        if (data?.dryRun) return;
        recoverCurrentPlayerMessage(data);
        if (!settings.enabled || !settings.stamp.sentinel) return;
        data.chat.unshift({ role: "system", content: sentinel() });
      } catch (e) { /* fail-open: header or LCP fallback still identifies (03 §2) */ }
    });
    on("CHAT_CHANGED", () => {                              // 05 §5
      retireNarratorPulse();
      const identity = ensureChatIdentity();
      turnCounter = 0; stampHeader(); hint("chat_changed"); refreshChip();
      if (!identity.derived) {
        genesisAtChatOpen(identity);                       // verified card seed FIRST,
        //                                           then turn-0 genesis (both proxy-idempotent)
      }
      try { setTimeout(scrubTags, 400); setTimeout(scrubTags, 1400); } catch (e) {}   // hide ledger tags
      try {                                                 // 2026-07-07 live repro: the panel's
        const a = document.getElementById("aes_creator");   // Creator link kept the PREVIOUS
        if (a) a.href = settings.proxy_url                  // chat's session — copy-link or
          + "/aether/creator?session=" + encodeURIComponent(sid());   // middle-click then saved
      } catch (e) { /* fail-open */ }                       // the world to the WRONG session
    });
    on("GENERATION_STARTED", (type, opts, dryRun) => {
      try {
        normalizeGeneratedNarratorCard();
        if (dryRun) return;
        if (type) lastGenType = type;                       // fallback capture
        // 2026-07-10 (Arinvale): continues no longer tick the counter — a continue re-generates
        // the SAME server turn, and phantom ticks skipped indices (live session recorded turns
        // 1,3,4,5). The proxy now files every new turn at head+1 regardless; this keeps the
        // stamp honest as a dedup/debug hint.
        if (!type || type === "normal") turnCounter++;
        proposeNarratorPulse(type, opts, dryRun);
      } catch (e) {}
    });
    on("GENERATE_AFTER_DATA", (data, dryRun) => {
      try { activateNarratorPulse(data, dryRun); } catch (e) {}
    });
    on("GENERATION_ENDED", (chatLength) => { try { finishNarratorWindow(chatLength); } catch (e) {} });
    on("GENERATION_STOPPED", () => { try { retireNarratorPulse(); } catch (e) {} });
    on("MESSAGE_SWIPED", (i) => { retireNarratorPulse(); hint("swipe", Number(i)); genesisAtGreetingSwipe(i);
      try { setTimeout(scrubTags, 120); setTimeout(scrubTags, 900); } catch (e) {} });
    on("MESSAGE_EDITED", (i) => { hint("edit", Number(i)); try { setTimeout(scrubTags, 120); } catch (e) {} });
    on("MESSAGE_DELETED", (i) => hint("delete", Number(i)));
    on("MESSAGE_RECEIVED", (i, type) => {
      // MESSAGE_RECEIVED is the strongest proof that the visible reply completed. Current ST can
      // omit/coalesce the broader generation-ended signal on some paths, so it is also the stale
      // timer failsafe. Extension/system messages must not end a real foreground reply in flight.
      finishNarratorMessage(i, type);
      refreshChip(); lastGen = Date.now();
      try { setTimeout(scrubTags, 80); setTimeout(scrubTags, 500); setTimeout(scrubTags, 1600); } catch (e) {}
      try { if (hudVisible()) { setTimeout(hudRefresh, 1500); setTimeout(hudRefresh, 6000); } } catch (e) {} });
  } catch (e) { console.warn("[AetherState] event wiring unavailable", e); }

  // ---- quick panel (05 §7)
  let lastGen = 0;
  // 2026-07-10 (Arinvale): the thinking pulse. Reasoning models (GLM-5.2) can think silently
  // for a minute-plus while ST renders "..." — a healthy turn LOOKED dead in live play. While
  // a foreground narrator generation is in flight the HUD vitals show a live
  // "narrating… Ns" chip instead. It does not represent post-stream extraction or quiet jobs.
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
            <div class="aes-row" id="aes_intent_row" style="display:none">
              <label class="aes-check" title="Pure-code semantic reflex floor: natural phrasings map to the skill they mean (sweet-talk -> persuasion, sneaked -> stealth), and a strike targets a REAL on-scene character, not a stray word. No model, no network.">
                <input type="checkbox" id="aes_intent_floor"> semantic intent floor</label>
              <span class="aes-opt">grounds rolls &amp; targets by meaning</span>
            </div>
            <div class="aes-row" id="aes_compact_row" style="display:none">
              <label class="aes-check" title="On calm, established turns, inject the SHORTER DM rules-contract instead of the full one (the model has learned the rules by then) — saves ~800 tokens every calm turn. The full contract still rides the first few turns and every combat turn. Opt-in; off = the full contract every turn.">
                <input type="checkbox" id="aes_compact_contract"> auto-compact contract</label>
              <span class="aes-opt">terser rules on calm turns (saves tokens)</span>
            </div>
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
      const applyIntent = (sp) => {            // rpg-gated toggles: reflex floor + auto-compact contract
        if (!sp) return;
        const row = $("aes_intent_row"), box = $("aes_intent_floor");
        if (row && box) {
          row.style.display = (sp.name === "rpg") ? "" : "none";
          if (typeof sp.intent_floor === "boolean") box.checked = sp.intent_floor;
        }
        const crow = $("aes_compact_row"), cbox = $("aes_compact_contract");
        if (crow && cbox) {
          crow.style.display = (sp.name === "rpg") ? "" : "none";
          if (typeof sp.auto_compact_contract === "boolean") cbox.checked = sp.auto_compact_contract;
        }
      };
      try {                                    // narrative mode: show it + let the user switch it
        const sp = await api("/aether/specialization");
        if (sp && sp.name) { $("aes_spec").value = sp.name; $("aes_spec_state").textContent = "spec: " + sp.name; setSpecHelp(sp.name); }
        applyIntent(sp);                        // reflex-floor toggle: reflect + show under rpg
      } catch (e) {}
      $("aes_spec").onchange = async (e) => {
        setSpecHelp(e.target.value);
        const d = await api("/aether/specialization", { method: "POST",
          headers: { "content-type": "application/json" }, body: JSON.stringify({ name: e.target.value }) }).catch(() => null);
        if (d && d.name) { $("aes_spec_state").textContent = "spec: " + d.name; setSpecHelp(d.name); refreshChip();
          applyIntent(d); try { if (hudVisible()) hudRefresh(); } catch (err) {} }
      };
      $("aes_intent_floor").onchange = async (e) => {   // the semantic reflex floor, flipped live
        await api("/aether/specialization", { method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ intent_floor: e.target.checked }) }).catch(() => null);
      };
      $("aes_compact_contract").onchange = async (e) => {   // A1: auto-compact contract, flipped live
        await api("/aether/specialization", { method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ auto_compact_contract: e.target.checked }) }).catch(() => null);
      };
      $("aes_enabled").onchange = (e) => {
        settings.enabled = e.target.checked;
        if (!settings.enabled) retireNarratorPulse();
        save();
      };
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
    ["char", "◈ Char", "Character identity, attributes, drives, and goals."],
    ["skills", "✦ Skills", "Your learned competencies, roll modifiers, and check rules."],
    ["abilities", "❋ Abilities", "Active techniques and passive talents that change checks."],
    ["rolls", "🎲 Rolls", "Choose a check, see its full cost, and review settled results."],
    ["gear", "⚔ Gear", "Everything worn, wielded, or stowed as equipment."],
    ["inventory", "🎒 Items", "Consumables, materials, devices, and keepsakes—not gear."],
    ["status", "☤ Status", "Current conditions and explicit relationship consent boundaries."],
    ["world", "🌍 World", "Current world changes, quests, people, factions, agendas, and known history."],
  ];
  let hudTimer = null;
  let lastHudView = null;             // cache the last /hud payload so tab-switching never refetches
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const uniqueDisplayProse = (values) => {
    const out = [], seen = new Set();
    for (const raw of values || []) {
      if (typeof raw !== "string") continue;
      const text = raw.trim();
      const key = text.replace(/\s+/g, " ").toLowerCase();
      if (!key || seen.has(key)) continue;
      seen.add(key); out.push(text);
    }
    return out;
  };

  // ---- display-only ledger-tag hider (2026-07-07): the DM emits bracketed protocol tags
  // ([hp | ...], [scene | ...], and often invented ones) that the ENGINE parses from the raw
  // message — but they are engine plumbing, not prose. This hides them from the READER only:
  // it rewrites the RENDERED .mes_text, never message.mes, so the proxy still gets every tag
  // and the ledger keeps updating. Fail-open; toggle with settings.hud.hideTags.
  const _TAG_RE = () => /\[\s*[A-Za-z][^\]\n|]*\|[^\]\n]*\]/g;
  // These are authoritative request-only records, never valid narrator output. A model echo is
  // mechanically ignored and hidden from the reader even when ordinary ledger-tag hiding is off.
  // This scans rendered HTML, so markdown quote/list prefixes are already elements. Never consume
  // a raw `>` here: at the start of a paragraph it is the closing boundary of the `<p>` tag.
  const _ENGINE_HEADER_RE = () =>
    /[ \t]*\[\s*(?:DIRECTIVE|ENEMY\s+(?:INTENT|ACTION)|WAR|INIT|PLAYER|RULES|OPPOSITION|PROTOCOL|CONTEXT\s+PRIORITY|AETHER\s+P[0-3]|PRIVATE\s+COMBAT\s+NARRATION\s+PRIMER)(?:\s+[^\]\r\n<|]*)?\][^<\r\n]*/gi;
  const _HIDDEN_WRAPPER_RE = () =>
    /(<span\b(?=[^>]*\bclass=(?:"[^"]*\baes-hidden-tag\b[^"]*"|'[^']*\baes-hidden-tag\b[^']*'))[^>]*>[\s\S]*?<\/span>)/gi;
  // Defense-only: hide a narrator-hallucinated OOC macro. The engine never parses or arms a
  // narrator-authored check; all new rolls come from the Player's input through AetherState.
  const _OOC_RE = () => /\(\(+\s*aether\.[^)]*\)\)+/gi;
  const _scrubbedHtml = new WeakMap();
  const _hidden = (title, text) =>
    `<span class="aes-hidden-tag" title="${title}">${text}</span>`;
  function scrubTags() {
    try {
      const hideLedgerTags = !(settings.hud && settings.hud.hideTags === false);
      document.querySelectorAll("#chat .mes_text").forEach((t) => {
        if (!t) return;
        const html = t.innerHTML;
        if (_scrubbedHtml.get(t) === html) return;
        // Preserve wrappers from earlier streaming fragments, then scrub only newly rendered
        // chunks. This makes repeated message updates idempotent instead of permanently trusting
        // the first partial render.
        const parts = html.split(_HIDDEN_WRAPPER_RE());
        const next = parts.map((part, index) => {
          if (index % 2) return part;
          let chunk = part.replace(_ENGINE_HEADER_RE(),
            (m) => _hidden("AetherState input-only echo (ignored)", m));
          if (hideLedgerTags) {
            chunk = chunk.replace(_TAG_RE(),
              (m) => _hidden("AetherState ledger tag (hidden)", m));
          }
          return chunk.replace(_OOC_RE(),
            (m) => _hidden("AetherState macro (hidden)", m));
        }).join("");
        if (next !== html) t.innerHTML = next;
        _scrubbedHtml.set(t, t.innerHTML);
      });
    } catch (e) { /* fail-open: never touch the chat if this errors */ }
  }
  window.aetherScrubTags = scrubTags;

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
    hud.style.setProperty("--aes-hud-width", (p.width || 340) + "px");
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
    window.addEventListener("resize", () => clampHud(hud));
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
    h.classList.remove("hidden"); clampHud(h); settings.hud.open = true; save(); hudRefresh();
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
    h.classList.remove("compact"); clampHud(h); settings.hud.compact = false; save(); syncMinBtn(); hudRefresh();
  };
  function clampHud(box) {
    const vw = Number(window.innerWidth) || 0, vh = Number(window.innerHeight) || 0;
    if (!vw || !vh) return;
    const r = box.getBoundingClientRect();
    const left = Math.max(0, Math.min(r.left, Math.max(0, vw - r.width)));
    const top = Math.max(0, Math.min(r.top, Math.max(0, vh - r.height)));
    box.style.left = Math.round(left) + "px";
    box.style.top = Math.round(top) + "px";
    box.style.right = "auto";
  }
  function makeDraggable(box, handle) {
    let sx, sy, ox, oy, drag = false;
    handle.addEventListener("mousedown", (e) => {
      if (e.target.tagName === "SELECT" || e.target.tagName === "BUTTON") return;
      drag = true; sx = e.clientX; sy = e.clientY;
      const r = box.getBoundingClientRect(); ox = r.left; oy = r.top;
      box.style.right = "auto"; e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => { if (!drag) return;
      const r = box.getBoundingClientRect();
      const maxLeft = Math.max(0, (Number(window.innerWidth) || r.width) - r.width);
      const maxTop = Math.max(0, (Number(window.innerHeight) || r.height) - r.height);
      box.style.left = Math.max(0, Math.min(maxLeft, ox + e.clientX - sx)) + "px";
      box.style.top = Math.max(0, Math.min(maxTop, oy + e.clientY - sy)) + "px"; });
    window.addEventListener("mouseup", () => { if (!drag) return; drag = false;
      clampHud(box);
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
  function renderLastRoll(lr) {
    if (!lr || !lr.tier_label) return "";
    const tc = (lr.tier === "success" || lr.tier === "crit_success")
      ? "success" : (lr.tier === "partial" ? "partial" : "fail");
    const impact = lr.impact && typeof lr.impact === "object" ? lr.impact : null;
    const impactText = impact && String(impact.text || "").trim()
      ? String(impact.text).trim() : "Impact not recorded";
    return `<div class="aes-lastroll ${tc}">\uD83C\uDFB2 ${esc(lr.label || lr.skill || "roll")} \u2192 <b>${esc(lr.tier_label)}</b> <span class="m">(${esc(lr.result)})</span><span class="aes-lastroll-impact">${esc(impactText)}</span></div>`;
  }
  function renderCompact(v) {
    const p = (v.players || [])[0], s = v.scene || {};
    const loc = s.location ? esc(String(s.location).replace(/_/g, " ")) : "—";
    let h = `<div class="aes-kv" style="margin:2px 0">📍 ${loc}${s.time_of_day ? " · " + esc(s.time_of_day) : ""}</div>`;
    h += renderTransportError(v);
    if (!p) return h + `<div class="aes-hud-empty">no player</div>`;
    h += `<div class="aes-sub" style="margin:2px 0">${esc(p.name)} · Lv${p.level}</div>`;
    const bars = []; if (p.hp && p.hp.max) bars.push(bar("hp", p.hp.cur, p.hp.max, p.hp.name, p.hp.color));
    for (const k in (p.resources || {})) { const r = p.resources[k]; bars.push(bar(k, r.cur, r.max, r.name, r.color)); }
    if (bars.length) h += `<div class="aes-bars">${bars.join("")}</div>`;
    if ((p.effects || []).length) h += `<div class="aes-rows" style="margin-top:4px">${p.effects.map((e) => { const cls = e.valence === "positive" ? "pos good" : e.valence === "negative" ? "neg bad" : "neu"; return `<span class="aes-pill ${cls}"><span class="g">${esc(e.glyph)}</span> ${esc(e.name)}</span>`; }).join("")}</div>`;
    if (v.war_room && v.war_room.active) h += renderWarRoom(v.war_room, true);
    const lr = (v.rolls || []).slice(-1)[0];
    h += renderLastRoll(lr);
    // the strip must SAY it is a strip (2026-07-09) — a minimized HUD with no label reads
    // as "everything is gone". One tap restores the full sheet.
    h += `<button class="aes-expand" onclick="window.aetherHudExpand()" title="this compact strip shows vitals only — the full sheet (skills, gear, status, world…) is one tap away">▣ expand — full sheet</button>`;
    return h;
  }
  function bar(kind, cur, max, name, color) {
    const pct = max ? Math.max(0, Math.min(100, Math.round(100 * cur / max))) : 0;
    const id = String(kind || "").toLowerCase();
    const cls = id === "hp" || id === "stamina" || id === "mana" ? id : "custom";
    const label = String(name || (id === "hp" ? "HP" : kind) || "resource")
      .replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    const safeColor = /^#[0-9a-f]{6}$/i.test(String(color || "")) ? String(color).toLowerCase() : "";
    return `<div class="aes-bar ${cls}" title="${esc(label)}: ${esc(cur)} of ${esc(max)} remaining"><i style="width:${pct}%${safeColor ? ";background:" + safeColor : ""}"></i><span>${esc(label)} ${esc(cur)}/${esc(max)}</span></div>`;
  }
  function sec(title, ic, html, help = "") { return `<div class="aes-sec"><div class="aes-sec-h${help ? " aes-help" : ""}"${help ? ` title="${esc(help)}"` : ""}><span class="ic">${ic}</span>${esc(title)}</div>${html}</div>`; }
  function sechdr(t) { return `<div class="aes-sec-h" style="margin-top:8px">${esc(t)}</div>`; }
  function humanLabel(value) {
    return String(value || "").replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim()
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }
  function worldSignal(v) {
    const knowledge = v && v.knowledge && typeof v.knowledge === "object" ? v.knowledge : {};
    const events = knowledgeRows(knowledge, "events");
    const currentEvents = events.filter((row) =>
      ["active", "accepted", "current", "admission"].includes(String(row.status || "").toLowerCase()));
    const quests = Array.isArray(v && v.quests) ? v.quests : [];
    const activeQuests = quests.filter((row) => row && row.status === "active");
    const blockedQuests = activeQuests.filter((row) => row.available === false);
    const fronts = Array.isArray(v && v.fronts) ? v.fronts : [];
    const activeFronts = fronts.filter((row) => row && !row.done);
    const freshFronts = fronts.filter((row) => row && row.done && row.fresh);
    const scene = v && v.scene && typeof v.scene === "object" ? v.scene : {};
    const circumstances = [scene.world_circumstance, scene.location_circumstance].filter(Boolean).length;
    const worldFlags = Object.keys(v && v.world_flags && typeof v.world_flags === "object"
      ? v.world_flags : {}).length;
    const cast = Array.isArray(v && v.cast) ? v.cast.length : 0;
    const standings = (Array.isArray(v && v.relations) ? v.relations.length : 0) +
      (Array.isArray(v && v.factions) ? v.factions.length : 0);
    const count = currentEvents.length + activeQuests.length + activeFronts.length +
      freshFronts.length + circumstances + worldFlags + cast + standings;
    const short = [];
    if (freshFronts.length) short.push(`${freshFronts.length} ${freshFronts.length === 1 ? "agenda came" : "agendas came"} to a head`);
    if (currentEvents.length) short.push(`${currentEvents.length} active world ${currentEvents.length === 1 ? "change" : "changes"}`);
    if (blockedQuests.length) short.push(`${blockedQuests.length} blocked ${blockedQuests.length === 1 ? "quest" : "quests"}`);
    else if (activeQuests.length) short.push(`${activeQuests.length} active ${activeQuests.length === 1 ? "quest" : "quests"}`);
    if (activeFronts.length) short.push(`${activeFronts.length} moving ${activeFronts.length === 1 ? "agenda" : "agendas"}`);
    if (circumstances) short.push(`${circumstances} local ${circumstances === 1 ? "condition" : "conditions"}`);
    if (worldFlags) short.push(`${worldFlags} tracked world ${worldFlags === 1 ? "condition" : "conditions"}`);
    if (cast) short.push(`${cast} known ${cast === 1 ? "person" : "people"}`);
    if (standings) short.push(`${standings} social ${standings === 1 ? "standing" : "standings"}`);
    const detail = short.join(" · ");
    const text = short.slice(0, 3).join(" · ") + (short.length > 3 ? " · more" : "");
    return { count, urgent: !!(freshFronts.length || blockedQuests.length), text, detail };
  }
  function renderWorldPulse(v) {
    const signal = worldSignal(v);
    if (!signal.count) return "";
    return `<button class="aes-world-pulse${signal.urgent ? " urgent" : ""}" onclick="window.aetherHudTab('world')" title="${esc(signal.detail)}. Open the World tab for details."><span>🌍 <b>World</b></span><span>${esc(signal.text)}</span></button>`;
  }
  function signedWorldModifier(value) {
    if (value == null || typeof value === "boolean" || String(value).trim() === "") return "";
    const n = Number(value);
    return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n}` : "";
  }
  function worldEffectLine(label, value, cls = "") {
    if (value == null || String(value).trim() === "") return "";
    return `<div class="aes-world-effect${cls ? " " + cls : ""}"><b>${esc(label)}:</b> ${esc(value)}</div>`;
  }
  function capabilityAvailability(value) {
    if (value === false) {
      return `<div class="aes-world-effect unavailable"><b>Unavailable due to world change</b></div>`;
    }
    if (value === true) {
      return `<div class="aes-world-effect available"><b>Available under current world conditions</b></div>`;
    }
    return "";
  }
  // Tabbed HUD (2026-07-07): a persistent vitals strip + a tab bar so the whole tracked sheet
  // is organized, not dumped in one scroll. Char · Skills · Abilities · Gear (paper-doll) ·
  // Status · World. The player always sees vitals; the detail lives one tap away.
  function renderHud(v) {
    const p = (v.players || [])[0];
    const tab = HUD_TABS.some((t) => t[0] === settings.hud.tab) ? settings.hud.tab : "char";
    const playerImpacts = v.war_room && Array.isArray(v.war_room.player_impacts)
      ? v.war_room.player_impacts : [];
    let head = renderVitals(v, p, tab !== "rolls" && !playerImpacts.length);
    head += renderTransportError(v);
    if (v.frozen) head += `<div class="aes-hud-off">⏸ scene frozen${v.frozen_reason ? " (" + esc(v.frozen_reason) + ")" : ""}</div>`;
    if (v.war_room && (v.war_room.active || v.war_room.opposition)) head += renderWarRoom(v.war_room, false, tab === "rolls");
    if (tab !== "world") head += renderWorldPulse(v);
    // The current turn's resolved enemy action remains above the tabs after combat ends.
    const signal = worldSignal(v);
    head += `<div class="aes-tabs">${HUD_TABS.map(([k, label, help]) => {
      const badge = k === "world" && signal.count
        ? `<span class="aes-tab-badge${signal.urgent ? " urgent" : ""}">${esc(signal.count)}</span>` : "";
      return `<button class="aes-tab ${k === tab ? "on" : ""}" title="${esc(help)}" onclick="window.aetherHudTab('${k}')">${esc(label)}${badge}</button>`;
    }).join("")}</div>`;
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
  function renderTransportError(v) {
    const e = v && v.transport_error;
    if (!e) return "";
    return `<div class="aes-hud-off">⚠ Upstream request failed (HTTP ${esc(e.status || "error")})` +
      `${e.message ? " — " + esc(e.message) : ""}. No assistant reply was produced.</div>`;
  }
  function renderVitals(v, p, showLastRoll = true) {
    const s = v.scene || {};
    const loc = s.location ? esc(String(s.location).replace(/_/g, " ")) : "—";
    let h = `<div class="aes-vitals"><div class="aes-vit-scene">📍 ${loc}${s.time_of_day ? " · " + esc(s.time_of_day) : ""}${s.phase ? " · " + esc(s.phase) : ""} <span id="aes_pulse" class="aes-tag" style="display:none"></span></div>`;
    if (p) {
      h += `<div class="aes-vit-name">${esc(p.name)} <span class="m">Lv${esc(p.level)}${p.xp ? " · XP " + esc(p.xp) : ""}</span>${p.stat_points ? ` <span class="aes-tag warn">${esc(p.stat_points)} pt</span>` : ""}${p.mood ? ` <span class="aes-dim">${esc(p.mood)}</span>` : ""}</div>`;
      const bars = []; if (p.hp && p.hp.max) bars.push(bar("hp", p.hp.cur, p.hp.max, p.hp.name, p.hp.color));
      for (const k in (p.resources || {})) { const r = p.resources[k]; bars.push(bar(k, r.cur, r.max, r.name, r.color)); }
      if (bars.length) h += `<div class="aes-bars">${bars.join("")}</div>`;
      h += `<div class="aes-act-row">${actBtn("HP −5", [{ op: "hp_adj", char: p.eid, delta: -5 }], "lose 5 HP")}${actBtn("−1", [{ op: "hp_adj", char: p.eid, delta: -1 }], "lose 1 HP")}${actBtn("+1", [{ op: "hp_adj", char: p.eid, delta: 1 }], "heal 1 HP")}${actBtn("+5", [{ op: "hp_adj", char: p.eid, delta: 5 }], "heal 5 HP")}</div>`;
      const lr = (v.rolls || []).slice(-1)[0];
      if (showLastRoll) h += renderLastRoll(lr);
      const ln = (v.notices || []).slice(-1)[0];   // 2026-07-10 (pillar 17): latest engine
      if (ln) h += `<div class="aes-roll-note" title="engine notice — the Rolls tab keeps the recent ones">⚠ ${esc(ln.text)}</div>`;
    }
    return h + `</div>`;
  }
  // ---- Phase 1: the War Room lane (the mechanics contract, verified) — combatant cards with EXACT HP
  // numbers (pillar-17 rawness), tier/armament, one committed future enemy move, ally dice,
  // defeat marks and fresh loot chips. Rendered from committed rows only.
  function renderEnemyIntent(i, compact) {
    if (!i) return "";
    const actor = i.actor_name || i.actor || "Enemy";
    const target = i.target_name || i.target || "Player";
    const danger = i.danger || "moderate";
    const dangerTip = "Danger is the committed threat level, not guaranteed damage.";
    if (compact) return `<div class="aes-enemy-intent compact" title="This move is committed as the next enemy threat, but has not resolved yet.">` +
      `<b>${esc(i.move_name || i.move_id || "attack")}</b> · <span class="aes-tag warn aes-help" title="${dangerTip}">${esc(danger)}</span>` +
      `<div class="aes-kv" title="Committed attacker, target, and delivery.">${esc(actor)} → ${esc(target)}${i.delivery ? ` · ${esc(i.delivery)}` : ""}</div>` +
      `${i.tell ? `<div class="aes-kv" title="Visible warning you can react to before the move resolves."><span class="aes-dim">tell</span> ${esc(i.tell)}</div>` : ""}</div>`;
    return `<div class="aes-enemy-intent" title="This move is committed as the next enemy threat, but has not resolved yet."><div class="aes-enemy-intent-h">` +
      `<b>${esc(i.move_name || i.move_id || "attack")}</b> <span class="aes-tag warn aes-help" title="${dangerTip}">${esc(danger)}</span></div>` +
      `<div class="aes-kv" title="Committed attacker, target, and delivery."><b>${esc(actor)}</b> → <b>${esc(target)}</b>${i.delivery ? ` · ${esc(i.delivery)}` : ""}</div>` +
      `${i.tell ? `<div class="aes-kv" title="Visible warning you can react to before the move resolves."><span class="aes-dim">tell</span> ${esc(i.tell)}</div>` : ""}</div>`;
  }
  function renderEnemyOptions(i, compact) {
    if (!i) return "";
    const counterRows = Array.isArray(i.counterplay) ? i.counterplay : [];
    const counters = counterRows.map(esc).join(" · ");
    const brace = i.reaction && i.reaction.kind === "brace" && i.reaction.cost === "whole_action";
    if (!counters && !brace) return "";
    return `<div class="aes-enemy-options${compact ? " compact" : ""}" title="Grounded responses to the committed move. You can still attempt another valid action.">` +
      `${brace ? `<div class="aes-kv aes-brace" title="Brace is a code-owned reaction: it spends the whole action and halves this move's HP damage if it lands."><b>BRACE</b> · send <code>I brace.</code> · whole action · half HP damage</div>` : ""}` +
      `${counters ? `<div class="aes-kv" title="Openings are grounded counterplay cues, not a closed menu."><span class="aes-dim">openings</span> ${counters}</div>` : ""}</div>`;
  }
  function renderEnemyAction(a, compact = false) {
    if (!a) return "";
    const actor = a.actor_name || a.actor || "Enemy";
    const target = a.target_name || a.target || "Player";
    const move = a.move_name || a.move_id || "attack";
    const tier = String(a.tier || "resolved").toUpperCase();
    const rawDamage = Number(a.damage);
    const damage = Number.isFinite(rawDamage) ? Math.max(0, rawDamage) : 0;
    const reaction = a.reaction && a.reaction.applied ? a.reaction : null;
    const brace = reaction && reaction.kind === "brace";
    const cls = brace ? "brace" : (damage > 0 ? "hit" : "miss");
    const roll = Number.isFinite(Number(a.total)) ? `🎲${esc(a.total)} · ` : "";
    const outcome = damage > 0 ? `${esc(damage)} damage` : "no damage";
    const impactHp = a.hp_cur != null && a.hp_max != null
      ? `<span title="HP immediately after this enemy action settled."><b>Impact HP</b> ${esc(a.hp_cur)}/${esc(a.hp_max)}</span>` : "";
    const currentDiffers = a.current_hp_cur != null && a.current_hp_max != null &&
      (Number(a.current_hp_cur) !== Number(a.hp_cur) || Number(a.current_hp_max) !== Number(a.hp_max));
    const currentHp = currentDiffers
      ? `<span title="Latest HP now, after any later healing or damage."><b>Current HP</b> ${esc(a.current_hp_cur)}/${esc(a.current_hp_max)}</span>` : "";
    const saved = brace && Number.isFinite(Number(a.damage_saved))
      ? ` · Brace saved ${esc(a.damage_saved)} HP` : "";
    return `<div class="aes-enemy-action ${cls}${compact ? " compact" : ""}" title="code-settled enemy action from this turn">` +
      `<div class="aes-enemy-action-h"><b>${esc(move)}</b> <span class="aes-tag aes-help" title="The code-settled outcome tier for this enemy action.">${esc(tier)}</span></div>` +
      `<div class="aes-kv"><b>${esc(actor)}</b> → <b>${esc(target)}</b>` +
      `${a.delivery ? ` · ${esc(a.delivery)}` : ""}</div>` +
      `<div class="aes-kv"><b>${roll}${outcome}</b>${saved}</div>` +
      `${impactHp || currentHp ? `<div class="aes-impact">${impactHp}${currentHp}</div>` : ""}` +
      `</div>`;
  }
  function rollTruthContent(r) {
    const kind = r && r.kind === "roll" ? "Roll" : "Check";
    const label = r && (r.label || r.skill || r.spec) || "roll";
    const turn = r && r.turn != null && r.turn !== "" ? `<span>Turn ${esc(r.turn)}</span>` : "";
    const impact = r && r.impact && typeof r.impact === "object" ? r.impact : null;
    const impactKind = impact && ["none", "miss", "damage"].includes(impact.kind)
      ? impact.kind : "unknown";
    // impact.text is backend-owned settlement truth. Never infer harm from a successful tier,
    // or safety from a failed one. Old/malformed rows are visibly unknown.
    const impactText = impact && String(impact.text || "").trim()
      ? String(impact.text).trim() : "Impact not recorded";
    return `<div class="aes-roll"><span><span class="aes-roll-kind">${kind} · ${esc(label)}</span> = <b>${esc(r && r.result)}</b>${r && r.mod != null ? ` <span class="aes-dim">(mod ${r.mod >= 0 ? "+" : ""}${esc(r.mod)})</span>` : ""}</span>${r && r.tier_label ? `<span class="t ${esc(r.tier)}">${esc(r.tier_label)}</span>` : ""}</div><div class="aes-roll-impact ${impactKind}">${esc(impactText)}</div>${turn || (r && r.note) ? `<div class="aes-roll-meta">${turn}${r && r.note ? `<span>${esc(r.note)}</span>` : ""}</div>` : ""}`;
  }
  function renderWarRoom(w, compact = false, hidePlayerImpacts = false) {
    const die = (d) => d ? `<span class="aes-die ${d.tier === "MISSES" ? "miss" : d.tier === "GRAZES" ? "graze" : "hit"}" title="This ally die was pre-rolled deterministically for the current turn.">🎲${esc(d.total)} ${esc(d.tier.toLowerCase())}</span>` : "";
    const card = (c) => {
      const pct = c.hp.max ? Math.max(0, Math.min(100, Math.round(100 * c.hp.cur / c.hp.max))) : 0;
      let h = `<div class="aes-com ${c.side}${c.defeated ? " down" : ""}">`;
      h += `<div class="aes-com-h">${c.side === "enemy" ? "⚔" : "🛡"} <b>${esc(c.name)}</b>`;
      if (c.tier && c.tier !== "standard") h += ` <span class="aes-tag aes-help${c.tier === "boss" ? " warn" : ""}" title="Combat tier: a grounded measure of this combatant's threat and durability.">${esc(c.tier)}</span>`;
      if (c.kind === "tracked") h += ` <span class="aes-tag ok" title="a tracked character — wounds persist after the fight">tracked</span>`;
      h += c.defeated ? ` <span class="aes-tag bad aes-help" title="Defeated and no longer acting in this exchange.">☠ down</span>` : ` ${die(c.die)}`;
      h += `</div>`;
      if (!c.defeated) h += `<div class="aes-bar hp com" title="Exact code-tracked combat HP: ${esc(c.hp.cur)} of ${esc(c.hp.max)} remaining."><i style="width:${pct}%"></i><span>HP ${esc(c.hp.cur)}/${esc(c.hp.max)}</span></div>`;
      const bits = [];
      if (c.armament) bits.push(`<span class="aes-dim" title="Grounded armament currently used by this combatant.">⚒ ${esc(c.armament)}</span>`);
      if ((c.dropped || []).length) bits.push(`<span class="aes-pill warn" title="dropped loot on the field">💰 ${c.dropped.map(esc).join(", ")}</span>`);
      if (bits.length) h += `<div class="aes-kv">${bits.join(" ")}</div>`;
      return h + `</div>`;
    };
    const active = !!w.active;
    const foes = (w.combatants || []).filter((c) => c.side === "enemy");
    const allies = (w.combatants || []).filter((c) => c.side !== "enemy");
    const sectionHelp = {
      "WHAT YOU DID": "Code-settled Player checks from this turn. The roll tier and target impact are separate truths.",
      "WHAT HAPPENED": "The exact enemy action already settled by code this turn.",
      "WHAT IS COMING": "One committed future enemy move. It is actionable warning, not damage that already happened.",
      "WHAT YOU CAN DO": "Grounded counters to the incoming move. They do not limit Player freedom.",
      "COHORT": "Finite enemy reserves in the wider battle.",
      "ON THE FIELD": "Current combatants, exact HP, initiative, and wider battle state.",
    };
    const section = (label, html) => html ? `<section class="aes-war-section"><div class="aes-war-section-h aes-help" title="${esc(sectionHelp[label] || "")}">${esc(label)}</div>${html}</section>` : "";
    let h = `<div class="aes-war${active ? "" : " ended"}"><div class="aes-war-h aes-help" title="Player-facing code truth for the current combat exchange. Hover the labels and chips for details.">⚔ WAR ROOM <span class="m">${active ? `round ${esc(w.round)}` : "combat ended · last exchange"}</span></div>`;
    const playerImpacts = Array.isArray(w.player_impacts)
      ? w.player_impacts.filter((row) => row && typeof row === "object") : [];
    if (!compact && !hidePlayerImpacts && playerImpacts.length) {
      h += section("WHAT YOU DID", `<div class="aes-player-impacts">${playerImpacts.map((row) =>
        `<div class="aes-player-impact">${rollTruthContent(row)}</div>`).join("")}</div>`);
    }
    if (!compact && w.opposition) h += section("WHAT HAPPENED", renderEnemyAction(w.opposition, compact));
    if (active && w.intent) {
      h += section("WHAT IS COMING", renderEnemyIntent(w.intent, compact));
      h += section("WHAT YOU CAN DO", renderEnemyOptions(w.intent, compact));
    }
    if (compact) {
      const c = active && w.battle && w.battle.cohort;
      if (c) h += section("COHORT", `<div class="aes-kv"><b>${esc(c.name || "cohort")}</b> <span class="aes-dim">${esc(c.active)} active · ${esc(c.defeated)} defeated · ${esc(c.queued)} queued / ${esc(c.total)}</span></div>`);
      return h + `</div>`;
    }
    let field = "";
    if (active && w.battle) {                         // §F: the macro large-scale-battle chip
      const t = w.battle.tide, cls = t === "winning" ? "ok" : t === "losing" ? "bad" : "warn";
      field += `<div class="aes-kv" title="The wider battle beyond the immediate combatants.">⚑ <b>${esc(w.battle.name || "battle")}</b> <span class="aes-tag ${cls}" title="Current battle tide: whether your side is gaining or losing ground.">${esc(t)}</span>${w.battle.waves ? ` <span class="aes-dim" title="Current reinforcement wave.">wave ${esc(w.battle.waves)}</span>` : ""}</div>`;
      const c = w.battle.cohort;
      if (c) field += `<div class="aes-kv" title="finite code-owned enemy cohort"><b>${esc(c.name || "cohort")}</b> <span class="aes-dim">${esc(c.active)} active · ${esc(c.defeated)} defeated · ${esc(c.queued)} queued / ${esc(c.total)}</span></div>`;
    }
    if (active && (w.order || []).length > 1) {       // explicit initiative order (2026-07-10)
      field += `<div class="aes-war-init" title="Code-owned action order for this combat round."><span class="aes-dim">initiative</span> ` +
        w.order.map((o, i) => `<span class="aes-init ${o.side === "player" ? "me" : esc(o.side)}">` +
          `${i + 1}. ${esc(o.name)}</span>`).join(` <span class="aes-dim">→</span> `) + `</div>`;
    }
    if (active && foes.length) field += `<div class="aes-war-side">${foes.map(card).join("")}</div>`;
    if (active && allies.length) field += `<div class="aes-war-side">${allies.map(card).join("")}</div>`;
    h += section("ON THE FIELD", field);
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
      return r + worldEffectLine("World condition", c.world_condition, "actor") + `</div>`;
    }).join("");
    return sec("Cast", "👥", rows);
  }
  // ---- Rolls tab composer: one mechanic draft by default. Skills replace that draft and a
  // matching active upgrades it in place. Only the visible one-shot separate-action control
  // appends another paid check. The ENGINE still owns every result and cost.
  let rollDraft = "";                                  // survives the 5s HUD re-render
  let separateRollArmed = false;
  const rollCheckRe = () => /\(\(\s*aether\.check\s+([^()]+?)\s*\)\)/gi;
  const normalizedRollKey = (value) => String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
  function parsedRollBody(body) {
    const raw = String(body || "").trim();
    if (!raw || /[()]/.test(raw)) return null;
    const hit = raw.match(/^(.+?)(?:\s+use\s+([^\s()]+))?$/i);
    if (!hit || !String(hit[1] || "").trim()) return null;
    return { skill: String(hit[1]).trim(), use: String(hit[2] || "").trim() };
  }
  function rollMacro(skill, use) {
    return `((aether.check ${skill}${use ? " use " + use : ""}))`;
  }
  function appendedDraftText(current, macro) {
    return current + (current && !/\s$/.test(current) ? " " : "") + macro;
  }
  function composeRollDraft(current, slug, use, separate) {
    const text = String(current || "");
    const skill = String(slug || "").trim();
    const ability = String(use || "").trim();
    if (!skill) return { ok: false, changed: false, text, mode: "invalid" };
    const macro = rollMacro(skill, ability);
    if (separate) {
      return { ok: true, changed: true, text: appendedDraftText(text, macro), mode: "separate" };
    }

    const checks = [];
    const re = rollCheckRe();
    let hit;
    while ((hit = re.exec(text)) !== null) {
      const parsed = parsedRollBody(hit[1]);
      if (parsed) checks.push({ index: hit.index, end: re.lastIndex, macro: hit[0], ...parsed });
      if (hit[0] === "") re.lastIndex += 1;
    }
    if (!checks.length) {
      return { ok: true, changed: true, text: appendedDraftText(text, macro), mode: "inserted" };
    }

    const skillKey = normalizedRollKey(skill);
    const abilityKey = normalizedRollKey(ability);
    const matching = checks.filter((check) => normalizedRollKey(check.skill) === skillKey);
    const target = (ability && matching.length ? matching : checks).slice(-1)[0];
    const sameSkill = normalizedRollKey(target.skill) === skillKey;
    const sameAbility = normalizedRollKey(target.use) === abilityKey;

    // Re-selecting the same skill never strips its active. Re-selecting the same active is
    // idempotent. A different active replaces the old upgrade on this one draft check.
    if (sameSkill && ((!ability && target.use) || sameAbility)) {
      return { ok: true, changed: false, text, mode: "selected" };
    }
    return {
      ok: true,
      changed: target.macro !== macro,
      text: text.slice(0, target.index) + macro + text.slice(target.end),
      mode: sameSkill && ability ? "upgraded" : "replaced",
    };
  }
  function updateSeparateRollControl() {
    try {
      const box = document.getElementById("aes_roll_separate");
      if (box) box.checked = separateRollArmed;
    } catch (e) {}
  }
  window.aetherSetSeparateRoll = (armed) => {
    separateRollArmed = Boolean(armed);
    updateSeparateRollControl();
    return separateRollArmed;
  };
  window.aetherSeparateRollArmed = () => separateRollArmed;
  function aetherInsertText(text) {
    const ta = document.querySelector("#send_textarea");
    if (!ta) { console.warn("[AetherState] no #send_textarea to insert into"); return false; }
    const cur = ta.value || "";
    ta.value = cur + (cur && !/\s$/.test(cur) ? " " : "") + text;
    ta.dispatchEvent(new Event("input", { bubbles: true }));   // ST reads value on send + auto-resizes
    ta.focus();
    return true;
  }
  window.aetherInsertText = aetherInsertText;
  window.aetherInsertRoll = (slug, use) => {
    const ta = document.querySelector("#send_textarea");
    if (!ta) { console.warn("[AetherState] no #send_textarea to compose into"); return false; }
    const composed = composeRollDraft(ta.value || "", slug, use, separateRollArmed);
    if (!composed.ok) return false;
    if (composed.changed) {
      ta.value = composed.text;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
    ta.focus();
    if (separateRollArmed) window.aetherSetSeparateRoll(false);
    return true;
  };
  window.aetherRollGated = (msg) => {          // 2026-07-10 (Arinvale): a gated button now
    try {                                       // REFUSES — it used to insert anyway and the
      const m = String(msg || "not available right now");   // engine rolled WITHOUT the ability
      if (window.toastr) toastr.warning(m, "AetherState");
      else console.warn("[AetherState] " + m);
    } catch (e) {}
  };
  // Every Rolls-tab action goes through this gate. The renderer marks basis-locked,
  // recharging, and unaffordable actions as gated; none of them may leak a command into the
  // composer.
  window.aetherTryRoll = (button, slug, use) => {
    try {
      if (button && button.classList && button.classList.contains("gated")) {
        window.aetherRollGated(button.title);
        return false;
      }
      return window.aetherInsertRoll(slug, use);
    } catch (e) { return false; }
  };
  window.aetherRollDraft = (el) => { rollDraft = el.value; };
  window.aetherInsertCustom = () => {
    const inp = document.getElementById("aes_roll_custom");
    const val = ((inp ? inp.value : rollDraft) || "").trim();
    if (!val) return false;
    const macro = val.match(/^\(\(\s*aether\.check\s+([^()]+?)\s*\)\)$/i);
    const parsed = parsedRollBody(macro ? macro[1] : val);
    if (!parsed || !window.aetherInsertRoll(parsed.skill, parsed.use)) return false;
    rollDraft = ""; if (inp) inp.value = "";
    return true;
  };
  function rollResourcePools(p) {
    const rows = [];
    const add = (id, pool, fallback) => {
      if (!pool || typeof pool !== "object") return;
      const label = String(pool.name || fallback || id || "resource").trim();
      const color = /^#[0-9a-f]{6}$/i.test(String(pool.color || ""))
        ? String(pool.color).toLowerCase() : "";
      const cur = Number(pool.cur);
      rows.push({ id: String(id), label, color,
        cur: Number.isFinite(cur) ? Math.max(0, cur) : 0 });
    };
    if (p.hp) add("hp", p.hp, "HP");
    for (const id in (p.resources || {})) {
      add(id, p.resources[id], String(id).replace(/_/g, " ").replace(/\b\w/g,
        (c) => c.toUpperCase()));
    }
    return rows;
  }
  // Costs arrive as presentation-ready labels (for example "Ash Focus 2 + Tempo 1"). Parse
  // them against the structured pools rather than turning resource ids into selectors or CSS
  // classes. Longest-label-first keeps overlapping custom names deterministic.
  function parseRollCost(pools, text) {
    let rest = String(text || "").trim();
    if (!rest) return { ok: true, parts: [] };
    const parts = [];
    const ordered = pools.slice().sort((a, b) => b.label.length - a.label.length);
    while (rest) {
      let hit = null;
      for (const pool of ordered) {
        if (!rest.toLowerCase().startsWith(pool.label.toLowerCase())) continue;
        const tail = rest.slice(pool.label.length);
        const m = tail.match(/^\s+(\d+)(?:\s+\+\s+|$)/);
        if (m) {
          hit = { pool, amount: Number(m[1]), consumed: pool.label.length + m[0].length };
          break;
        }
      }
      if (!hit || !Number.isFinite(hit.amount) || hit.amount < 1) {
        return { ok: false, parts: [] };
      }
      parts.push({ ...hit.pool, amount: hit.amount });
      rest = rest.slice(hit.consumed).trim();
    }
    return { ok: true, parts };
  }
  function rollCostStatus(p, costs) {
    const source = (costs || []).map((cost) => String(cost || "").trim()).filter(Boolean);
    if (!source.length) {
      return { has: false, affordable: true, parts: [], text: "", shortage: "" };
    }
    const pools = rollResourcePools(p), totals = new Map();
    for (const cost of source) {
      const parsed = parseRollCost(pools, cost);
      if (!parsed.ok) return { has: true, affordable: false, parts: [],
        text: source.join(" + "), shortage: "cost availability could not be verified" };
      for (const part of parsed.parts) {
        const old = totals.get(part.id);
        totals.set(part.id, { ...part, amount: part.amount + (old ? old.amount : 0) });
      }
    }
    const parts = [...totals.values()];
    const short = parts.filter((part) => part.cur < part.amount);
    const text = parts.map((part) => `${part.label} ${part.amount}`).join(" + ");
    const shortage = short.map((part) => `${part.label} ${part.cur}/${part.amount}`).join(" · ");
    return { has: true, affordable: short.length === 0, parts, text, shortage };
  }
  function rollCostHtml(status) {
    if (!status.has) return "";
    if (!status.parts.length) {
      return `<span class="aes-action-cost">${esc(status.text)}</span>`;
    }
    return status.parts.map((part) => `<span class="aes-action-cost"${part.color
      ? ` style="--aes-resource-color:${part.color}"` : ""}>${esc(part.label)} ${esc(part.amount)}</span>`).join("");
  }
  const rollGateAttrs = (blocked) => blocked ? ` aria-disabled="true"` : "";
  function renderRollHistory(v) {
    const rolls = Array.isArray(v && v.rolls) ? v.rolls : [];
    if (!rolls.length) return "";
    return sechdr("Recent checks") + `<div class="aes-rows2">${rolls.slice().reverse().map((r) =>
      `<div class="aes-roll-history">${rollTruthContent(r)}</div>`).join("")}</div>`;
  }
  function tabRolls(v, p) {
    const sk = p.skills || [], abils = p.abilities || [];
    let h = `<div class="aes-roll-help"><b>One draft action:</b> tap a skill to set its check. Tapping its active ability upgrades that draft in place, so one intent cannot silently become two rolls or two costs. <b>Or just write it:</b> name a skill or ability in your prose and the engine rolls it for you.</div>`;
    h += `<label class="aes-roll-separate"><input id="aes_roll_separate" type="checkbox"${separateRollArmed ? " checked" : ""} onchange="window.aetherSetSeparateRoll(this.checked)"> <span><b>Add next as a separate action</b> — one-shot; this creates another paid action with its own check and cost.</span></label>`;
    const nts = (v.notices || []).slice(-4);   // 2026-07-10 (pillar 17): engine notices
    if (nts.length) h += sechdr("Engine notices — what recent rolls actually did") + `<div class="aes-rows2">${nts.map((n) => `<div class="aes-roll-note">⚠ t${esc(n.turn)} — ${esc(n.text)}</div>`).join("")}</div>`;
    if (!sk.length) h += `<div class="aes-hud-empty">No skills yet. Build a character in the Creator, or earn skills in-world.</div>`;
    else {
      const { groups, order } = groupByCategory(sk, "Skills");
      const solo = order.length === 1 && order[0] === "Skills";
      for (const g of order) {
        h += sechdr(solo ? "Skills \u2014 tap to roll" : g);
        h += `<div class="aes-rollbtns">${groups[g].map((s) => {
          const basisGated = s.gated && !s.basis_met;
          const worldUnavailable = s.eligible === false;
          const cost = rollCostStatus(p, [s.cost]);
          const unaffordable = cost.has && !cost.affordable;
          const blocked = basisGated || worldUnavailable || unaffordable;
          const reasons = [];
          if (basisGated) {
            reasons.push("needs " + (s.basis_name || "a basis") + " \u2014 this would be a non-move");
          }
          if (worldUnavailable) reasons.push("unavailable due to world change");
          if (cost.has) reasons.push(`costs ${cost.text}` + (unaffordable
            ? ` \u2014 cannot pay${cost.shortage ? " (have/need " + cost.shortage + ")" : ""}` : ""));
          if (!blocked) reasons.unshift("set draft check ((aether.check " + s.id + "))");
          return `<button class="aes-rollbtn${blocked ? " gated" : ""}${worldUnavailable ? " world-unavailable" : ""}${unaffordable ? " unaffordable" : ""}"${rollGateAttrs(blocked)} title="${esc(reasons.join(" · "))}" onclick="window.aetherTryRoll(this,'${esc(s.id)}')">${esc(s.label)} <span class="m">${s.mod >= 0 ? "+" : ""}${esc(s.mod)}</span>${rollCostHtml(cost)}${worldUnavailable ? `<span class="aes-cost-state">unavailable due to world change</span>` : ""}${unaffordable ? `<span class="aes-cost-state">cannot pay</span>` : ""}</button>`;
        }).join("")}</div>`;
      }
    }
    const acts = abils.filter((a) => a.active);
    if (acts.length) {
      h += sechdr("Active abilities \u2014 invoke on a check");
      h += `<div class="aes-rollbtns">${acts.map((a) => {
        const skill = sk.find((s) => String(s.id) === String(a.applies_id));
        const cost = rollCostStatus(p, [skill && skill.cost, a.cost]);
        const unaffordable = cost.has && !cost.affordable;
        const skillGated = !!(skill && skill.gated && !skill.basis_met);
        const worldUnavailable = a.eligible === false || !!(skill && skill.eligible === false);
        if (a.applies_id) {
          const blocked = !!a.on_cd || skillGated || worldUnavailable || unaffordable;
          const reasons = [];
          if (a.on_cd) {
            reasons.push(`${a.name} is recharging (${a.on_cd}t) — a roll now would go WITHOUT it`);
          }
          if (skillGated) {
            reasons.push(`needs ${skill.basis_name || "a basis"} — this would be a non-move`);
          }
          if (worldUnavailable) reasons.push("unavailable due to world change");
          if (cost.has) reasons.push(`total cost ${cost.text}` + (unaffordable
            ? ` — cannot pay${cost.shortage ? " (have/need " + cost.shortage + ")" : ""}` : ""));
          if (!blocked) reasons.unshift(`upgrade ${a.applies_id} draft with ${a.name}; total cost is shown once`);
          return `<button class="aes-rollbtn act${blocked ? " gated" : ""}${worldUnavailable ? " world-unavailable" : ""}${unaffordable ? " unaffordable" : ""}"${rollGateAttrs(blocked)} title="${esc(reasons.join(" · "))}" onclick="window.aetherTryRoll(this,'${esc(a.applies_id)}','${esc(a.id)}')">\u2726 ${esc(a.name)} <span class="m">on ${esc(a.applies_to)}</span>${rollCostHtml(cost)}${worldUnavailable ? `<span class="aes-cost-state">unavailable due to world change</span>` : ""}${unaffordable ? `<span class="aes-cost-state">cannot pay</span>` : ""}</button>`;
        }
        return `<span class="aes-pill${worldUnavailable || unaffordable ? " bad" : ""}">\u2726 ${esc(a.name)} <span class="aes-tag">any check \u2014 type \u2018use ${esc(a.id)}\u2019</span>${rollCostHtml(cost)}${worldUnavailable ? `<span class="aes-cost-state">unavailable due to world change</span>` : ""}${unaffordable ? `<span class="aes-cost-state">cannot pay</span>` : ""}</span>`;
      }).join("")}</div>`;
    }
    const pass = abils.filter((a) => !a.active);
    if (pass.length) {
      h += sechdr("Passive abilities \u2014 always on");
      h += `<div class="aes-rows">${pass.map((a) => `<span class="aes-pill${a.eligible === false ? " bad" : ""}">${esc(a.name)} <span class="aes-tag">${a.applies_to === "all checks" ? "all checks" : "on " + esc(a.applies_to)}</span>${a.eligible === false ? `<span class="aes-cost-state">unavailable due to world change</span>` : ""}</span>`).join("")}</div>`;
    }
    h += sechdr("Custom roll");
    h += `<div class="aes-roll-custom"><input id="aes_roll_custom" type="text" spellcheck="false" placeholder="skill name or slug" value="${esc(rollDraft)}" oninput="window.aetherRollDraft(this)" onkeydown="if(event.key==='Enter'){event.preventDefault();window.aetherInsertCustom();}"><button class="aes-rollbtn" onclick="window.aetherInsertCustom()">Insert</button></div>`;
    h += `<div class="aes-roll-note">Must be a skill you actually have (or one you built in the Creator). An unknown name comes back as a visible \u201cno basis\u201d non-move \u2014 that's by design. Type <code>skill use ability</code> to invoke an active on a check; selecting an active upgrades that draft rather than adding another roll.</div>`;
    return renderRollHistory(v) + h;
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
    const prose = uniqueDisplayProse([s.desc]);
    const governs = Array.isArray(s.governs) ? uniqueDisplayProse(s.governs) : [];
    return `<div class="aes-skill${s.eligible === false ? " unavailable" : ""}"><span class="aes-skl"><b>${esc(s.label)}</b> <span class="m big">${s.mod >= 0 ? "+" : ""}${esc(s.mod)}</span></span><span class="aes-skmeta">${s.keyed_stat ? esc(s.keyed_stat) : ""}${s.bracket ? " · " + esc(s.bracket) : ""}${s.mastery ? " · m" + esc(s.mastery) : ""}${s.cost ? " · costs " + esc(s.cost) : ""}${s.gated ? (s.basis_met ? ` · <span class="aes-tag ok">✦ ${esc(s.basis_name)}</span>` : ` · <span class="aes-tag bad">needs ${esc(s.basis_name || "a basis")}</span>`) : ""}</span>${prose.map((line) => `<div class="aes-abil-desc">${esc(line)}</div>`).join("")}${governs.length ? `<div class="aes-skmeta">Used for: ${governs.map(esc).join(", ")}</div>` : ""}${capabilityAvailability(s.eligible)}</div>`;
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
    const prose = uniqueDisplayProse([a.effect, a.desc]);
    bits.push(a.applies_to && a.applies_to !== "all checks" ? "on " + esc(a.applies_to) : "any check");
    if (a.cost) bits.push("costs " + esc(a.cost));
    if (a.cooldown) bits.push(a.on_cd ? `recharging ${esc(a.on_cd)}t` : `cooldown ${esc(a.cooldown)}t`);
    return `<div class="aes-abil ${a.active ? "act" : ""} ${a.on_cd ? "cd" : ""}${a.eligible === false ? " unavailable" : ""}"><div class="aes-abil-h"><b>${esc(a.name)}</b> ${badge}</div>${a.mechanic_label ? `<div class="aes-abil-mech">${esc(a.mechanic_label)}</div>` : ""}${bits.length ? `<div class="aes-abil-meta">${bits.join(" · ")}</div>` : ""}${prose.map((line) => `<div class="aes-abil-desc">${esc(line)}</div>`).join("")}${capabilityAvailability(a.eligible)}</div>`;
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
    return sechdr(hdr) + stow.map((c) => `<div class="aes-invrow"><b>${esc(c.container)}:</b> ${c.items.map((i) => { const sl = freeSlotFor(p, i); return `<span class="aes-inv">${esc((i.qty > 1 ? i.qty + "\u00d7 " : "") + i.name)}${i.type ? ` <span class="aes-dim">${esc(i.type)}</span>` : ""}${i.aura ? `<span class="aes-aura">✦ ${esc(i.aura)}</span>` : ""}${actBtn("equip", [{ op: "item_equip", instance: i.iid, slot: sl }], "wear/wield \u2192 " + sl, "mini")}</span>`; }).join(" ")}</div>`).join("");
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
    if (!inv.length) return `<div class="aes-hud-empty">No carried items. Worn and stowed equipment stays under ⚔ Gear.</div>`;
    return sechdr("🎒 Inventory — carried items") + inv.map((c) => `<div class="aes-invrow"><b>${esc(c.container)}:</b> ${c.items.map((i) => `<span class="aes-inv">${esc((i.qty > 1 ? i.qty + "× " : "") + i.name)}${i.type ? ` <span class="aes-dim">${esc(i.type)}</span>` : ""}${i.consumable ? actBtn("use", [{ op: "item_consume", instance: i.iid }], "consume one", "mini") : ""}${i.slot ? actBtn("equip", [{ op: "item_equip", instance: i.iid, slot: i.slot }], "wear/wield", "mini") : ""}</span>`).join(" ")}</div>`).join("");
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
        return `<div class="aes-slot filled ${g}"><span class="aes-slot-l">${esc(s.label)}</span><span class="aes-slot-i">${esc(it.name)}${it.mods ? ` <span class="m">${esc(it.mods)}</span>` : ""}${it.aura ? `<span class="aes-aura">✦ ${esc(it.aura)}</span>` : ""}</span>${actBtn("✕", [{ op: "item_unequip", instance: it.iid }], "take off", "x")}</div>`;
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
    if ((v.consent || []).length) h += sec("Consent boundaries", "✔",
      v.consent.map((c) => `<div class="aes-kv" title="Explicit code-tracked boundary for this relationship and category."><b>${esc(c.pair)}</b> · ${esc(c.category)} <span class="m">${esc(c.level)}</span>${c.cap != null ? " ≤" + esc(c.cap) : ""}</div>`).join(""),
      "Explicit relationship boundaries belong with Player status, not buried in world history.");
    return h;
  }
  function knowledgeRows(knowledge, key) {
    const rows = knowledge && Array.isArray(knowledge[key]) ? knowledge[key] : [];
    return rows.filter((row) => row && typeof row === "object" && !Array.isArray(row));
  }
  function knowledgeTone(value, kind) {
    const normalized = String(value || "").trim().toLowerCase();
    if (kind === "stance") {
      if (["knows", "knowledge", "certain"].includes(normalized)) return "known";
      if (["doubts", "doubt", "disbelieves"].includes(normalized)) return "doubt";
      if (["rumor", "rumour", "hearsay"].includes(normalized)) return "rumor";
      return "belief";
    }
    if (["active", "accepted", "current", "admission"].includes(normalized)) return "current";
    if (["scheduled", "scheduled_terminal"].includes(normalized)) return "scheduled";
    return "history";
  }
  function knowledgeStatusLabel(value) {
    const status = String(value || "").trim();
    const normalized = status.toLowerCase();
    if (["expiry", "expired", "expired_by_duration"].includes(normalized)) return "expired";
    if (["reversal", "reversed"].includes(normalized)) return "reversed";
    if (["supersession", "superseded"].includes(normalized)) return "superseded";
    if (normalized === "scheduled_terminal") return "scheduled history";
    if (normalized === "winning_terminal") return "history record";
    if (normalized === "terminal_conflict_lost") return "history conflict";
    return status || "history";
  }
  function renderKnowledge(v) {
    const knowledge = v && v.knowledge && typeof v.knowledge === "object"
      ? v.knowledge : {};
    const claims = knowledgeRows(knowledge, "claims");
    const epistemics = knowledgeRows(knowledge, "epistemics");
    const facts = knowledgeRows(knowledge, "facts");
    const events = knowledgeRows(knowledge, "events");
    if (!claims.length && !epistemics.length && !facts.length && !events.length) return "";

    const groups = [];
    if (claims.length) groups.push(`<div class="aes-knowledge-group claims">
      <div class="aes-knowledge-h">What was said</div>
      <div class="aes-knowledge-note">A claim records speech or attribution. It is not automatically true.</div>
      ${claims.map((claim) => {
        const speaker = claim.speaker || claim.source || "unknown source";
        const addressee = claim.addressee ? ` to <b>${esc(claim.addressee)}</b>` : "";
        const claimClass = claim.class || claim.claim_class || "said";
        const proposition = claim.proposition || claim.statement || claim.proposition_id || "proposition not available";
        const polarity = claim.polarity || claim.proposition_polarity || "polarity not specified";
        const modality = claim.modality || "modality not specified";
        return `<div class="aes-knowledge-row claim"><div><b>${esc(speaker)}</b>${addressee} <span class="aes-knowledge-kind">${esc(claimClass)}</span></div><div class="aes-knowledge-statement">${esc(proposition)}</div><div class="aes-knowledge-meta">${esc(polarity)} · ${esc(modality)}</div></div>`;
      }).join("")}
    </div>`);

    if (epistemics.length) groups.push(`<div class="aes-knowledge-group epistemics">
      <div class="aes-knowledge-h">Who knows, believes, doubts, or treats it as rumor</div>
      ${epistemics.map((record) => {
        const stance = record.stance || "believes";
        const statement = record.statement || record.proposition_id || "proposition not available";
        const source = record.source ? `<div class="aes-knowledge-meta">evidence: ${esc(record.source)}</div>` : "";
        return `<div class="aes-knowledge-row epistemic"><div><b>${esc(record.holder || "unknown actor")}</b> <span class="aes-knowledge-stance ${knowledgeTone(stance, "stance")}">${esc(stance)}</span></div><div class="aes-knowledge-statement">${esc(statement)}</div>${source}</div>`;
      }).join("")}
    </div>`);

    if (facts.length) groups.push(`<div class="aes-knowledge-group facts">
      <div class="aes-knowledge-h">Accepted facts</div>
      ${facts.map((fact) => {
        const status = fact.status || "accepted";
        const statement = fact.statement || fact.proposition_id || "fact not available";
        const meta = [fact.authority ? `authority: ${esc(fact.authority)}` : "", fact.cause ? `cause: ${esc(fact.cause)}` : ""].filter(Boolean).join(" · ");
        return `<div class="aes-knowledge-row fact"><div><span class="aes-knowledge-status ${knowledgeTone(status, "status")}">${esc(knowledgeStatusLabel(status))}</span></div><div class="aes-knowledge-statement">${esc(statement)}</div>${meta ? `<div class="aes-knowledge-meta">${meta}</div>` : ""}</div>`;
      }).join("")}
    </div>`);

    if (events.length) groups.push(`<div class="aes-knowledge-group events">
      <div class="aes-knowledge-h">Admitted world events and history</div>
      ${events.map((event) => {
        const status = event.status || "history";
        const statement = event.what_happened || event.statement || event.id || "event not available";
        const cause = event.cause_visible === true && event.cause
          ? String(event.cause) : "cause not known";
        const domains = Array.isArray(event.affected_domains)
          ? event.affected_domains.filter((domain) => typeof domain === "string" || typeof domain === "number")
          : [];
        const affected = domains.length ? domains.map(esc).join(", ") : "history only";
        const relation = event.relation_target ? ` · relates to ${esc(event.relation_target)}` : "";
        return `<div class="aes-knowledge-row event"><div><span class="aes-knowledge-status ${knowledgeTone(status, "status")}">${esc(knowledgeStatusLabel(status))}</span></div><div class="aes-knowledge-statement">${esc(statement)}</div><div class="aes-knowledge-meta">why: ${esc(cause)} · affected: ${affected}${relation}</div></div>`;
      }).join("")}
    </div>`);

    return sec("Claims & Events", "\u25c8", `<div class="aes-knowledge">${groups.join("")}</div>`,
      "Separates what was said, who believes it, accepted facts, and admitted world history.");
  }
  function renderQuest(q) {
    const worldUnavailable = q.available === false;
    const availability = worldUnavailable
      ? `<div class="aes-world-effect unavailable"><b>Unavailable due to world change</b></div>`
      : (q.available === true
        ? `<div class="aes-world-effect available"><b>Available under current world conditions</b></div>`
        : "");
    const outcome = q.status !== "active"
      ? " — " + esc(String(q.status || "inactive").toUpperCase())
      : (q.note ? " — " + esc(q.note) : "");
    return `<div class="aes-quest${q.status !== "active" ? " done" : ""}${worldUnavailable ? " unavailable" : ""}"><b>${esc(q.name)}</b>${q.stakes ? " (" + esc(q.stakes) + ")" : ""}${outcome}${availability}</div>`;
  }
  function renderSocialEntry(row, faction = false) {
    const modifier = signedWorldModifier(row.reputation_modifier);
    const circumstance = faction
      ? worldEffectLine("World circumstance", row.world_circumstance, "faction") : "";
    return `<div class="aes-social${faction ? " faction" : ""}"><div>${faction ? "⚑ " : ""}<b>${esc(row.name)}</b>${row.tier ? ` <span class="m">${esc(row.tier)}</span>` : ""}</div>${modifier ? worldEffectLine("Reputation modifier", modifier, "reputation") : ""}${circumstance}</div>`;
  }
  function renderRelationship(row) {
    const dims = Array.isArray(row.dims) ? row.dims : [];
    const modifier = signedWorldModifier(row.world_modifier);
    return `<div class="aes-relationship"><div class="aes-kv"><b>${esc(row.a)} → ${esc(row.b)}</b> ${dims.map((d) => `<span class="aes-dim">${esc(d.dim)} ${d.val >= 0 ? "+" : ""}${esc(d.val)}</span>`).join(" ")}</div>${modifier ? worldEffectLine("World modifier", modifier, "relationship") : ""}</div>`;
  }
  function tabWorld(v, p) {
    const parts = [], s = v.scene || {};
    const sceneBits = [];
    if ((s.present || []).length) sceneBits.push("👥 " + s.present.map(esc).join(", "));
    const sceneOverlay = worldEffectLine("World circumstance", s.world_circumstance, "world") +
      worldEffectLine("Location circumstance", s.location_circumstance, "location");
    if (sceneBits.length || sceneOverlay) parts.push(sec("Here now", "🗺", `${sceneBits.length ? `<div class="aes-kv">${sceneBits.join(" · ")}</div>` : ""}${sceneOverlay}`,
      "People present and world conditions affecting the current scene. Location and time stay in the fixed header."));
    const knowledge = renderKnowledge(v);
    if (knowledge) parts.push(knowledge);
    if ((v.cast || []).length) parts.push(renderCast(v.cast));
    if ((v.quests || []).length) parts.push(sec("Quests", "🎯", v.quests.map(renderQuest).join(""),
      "Tracked goals and whether current world conditions still allow them."));
    const rel = [...(v.relations || []).map((r) => renderSocialEntry(r)), ...(v.factions || []).map((f) => renderSocialEntry(f, true))];
    if (rel.length) parts.push(sec("Relations & Factions", "♥", `<div class="aes-rows">${rel.join("")}</div>`,
      "Player-facing standing and world modifiers affecting people and factions."));
    if ((v.relationships || []).length) parts.push(sec("Relationships", "🔗", v.relationships.map(renderRelationship).join(""),
      "Directional relationship dimensions and any current world modifier."));
    if (Object.keys(v.world_flags || {}).length) parts.push(sec("World conditions", "🌍", `<div class="aes-rows">${Object.entries(v.world_flags).map(([k, val]) => {
      const label = humanLabel(k);
      const shown = val === true ? label : val === false ? `${label}: no` : `${label}: ${String(val)}`;
      return `<span class="aes-pill aes-help" title="Ledger-tracked world condition: ${esc(label)}.">${esc(shown)}</span>`;
    }).join("")}</div>`, "Readable current world conditions tracked by the ledger."));
    if ((v.fronts || []).length) parts.push(sec("Agendas", "⏳", v.fronts.map((f) => {
      const segs = Math.max(1, f.segments | 0), fill = Math.min(segs, f.filled | 0);
      const pips = "●".repeat(fill) + "○".repeat(segs - fill);
      const head = `<b>${esc(f.name)}</b>${f.faction ? ` <span class="m">(${esc(f.faction)})</span>` : ""}`;
      if (f.done) return `<div class="aes-kv aes-front done" title="This agenda has concluded and its consequence is now player-facing world history.">${head} — <b>${f.fresh ? "⚠ COME TO A HEAD" : "concluded"}</b>${f.consequence ? `: ${esc(f.consequence)}` : ""}</div>`;
      return `<div class="aes-kv aes-front">${head} <span class="aes-pips aes-help" title="${fill} of ${segs} segments filled. This rumored agenda can advance off-screen.">${pips}</span></div>`;
    }).join("") + `<div class="aes-roll-note">Rumored agendas can advance off-screen.</div>`,
      "Rumored faction clocks. Filled segments show visible progress toward a consequence."));
    if ((v.memories || []).length) parts.push(sec("Recent events", "📜", v.memories.map((m) => `<div class="aes-kv"><span class="m">t${esc(m.turn)}</span> ${esc(m.text)}</div>`).join(""),
      "Recent player-facing continuity, newest events kept as a short ledger-backed list."));
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
        // A Creator card's structured seed is retried and verified before prose genesis.
        const d = await seedThenGenesis("command", true);
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
