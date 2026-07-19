"""Config loading per planning/12-config-schema.md.

Precedence: CLI > AETHERSTATE_* env > config.toml > defaults.
Invalid config never prevents startup (09 F1): last-known-good -> defaults.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # py310
    import tomli as tomllib  # type: ignore[no-redef]


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9130
    cors_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    data_dir: str = "./aetherstate-data"
    log_polling: bool = False        # 2026-07-09: access-log the extension's hud/status
    #                                  polling GETs too (default off — they drowned real
    #                                  events at ~1 line/second)
    turn_trace: bool = False         # local structured per-turn diagnostics; personal launchers
    #                                  force this on without changing standalone/server defaults
    turn_trace_max_mb: int = 16      # one JSONL segment; old segments rotate, never grow forever
    turn_trace_backups: int = 3      # retained rotated segments in addition to the active file


class UpstreamConfig(BaseModel):
    base_url: str = ""
    # ``api_key`` is legacy/transient only (old files and AETHERSTATE_UPSTREAM__API_KEY).
    # New Console saves keep only an opaque OS-vault reference in ordinary configuration.
    api_key: str = Field(default="", exclude=True, repr=False)
    credential_ref: str = ""
    model: str = ""                  # DEFAULT model for engine-initiated calls (creator
    #                                  authoring, genesis stage B) when nothing has been
    #                                  proxied yet. The RELAY never uses it — the frontend
    #                                  names its own model per request. Console-set.
    force_rung: int = 0
    disable_narrator_reasoning: bool = True
    # True (default): Venice-backed, typed narrator requests receive reasoning.enabled=false
    # plus Venice's hard disable_thinking flag. AetherState already settles mechanics; the live
    # narrator translates receipts instead of spending latency re-solving them. Engine-initiated
    # Creator/extraction jobs are unaffected. False returns narrator reasoning control to the
    # frontend/provider.
    probe_ttl_days: int = 7
    idle_timeout_s: int = 0          # 0 = no proxy-imposed stream timeout (09 U6)
    max_parse_mb: int = 20
    # ---- Phase 0a: KV-cache / prompt-caching enablement (plan doc 13, 2026-07-09) ----
    cache_key: bool = True           # add prompt_cache_key=<session id> to requests the
    #                                  engine ENRICHES (untouched requests stay byte-identical;
    #                                  a client-sent key always wins) — routes every turn of a
    #                                  conversation to the same warm provider cache server
    include_usage: bool = False      # opt-in: set stream_options.include_usage on enriched
    #                                  streaming requests so the upstream reports cache hits
    #                                  (adds one spec-standard usage chunk the frontend sees)
    prewarm: bool = False            # opt-in: at chat-open, re-send the session's last
    #                                  enriched prompt once (max_tokens=1) so the first real
    #                                  message hits a warm prefix — pays one full-price
    #                                  prefill to buy first-turn latency; cooldown-limited


class CreatorConfig(BaseModel):
    """Cold-path main-model authoring quality and completion limits.

    Creator responses are much larger than extraction deltas: one complete world includes
    locations, NPCs, loot, fronts, and routes, while one complete Player sheet includes all
    stats and frozen custom definitions.  The old 9k hard-coded ceiling truncated both.
    """

    max_tokens: int = Field(default=32768, ge=16384, le=131072)
    timeout_s: float = Field(default=600.0, ge=60.0, le=1800.0)
    validation_retries: int = Field(default=1, ge=1, le=2)


class StampConfig(BaseModel):
    header_name: str = "x-aetherstate-session"
    sentinel_prefix: str = "<<AETHER:"


class SessionConfig(BaseModel):
    """Engine knobs (12 [session], 03 SS2-3, 08 S5/S7/B1)."""
    min_anchor_msgs: int = 2         # LCP below this = new session
    dedup_window_s: int = 30         # duplicate-request window (08 S7)
    adopt_min_lcp: int = 6           # unknown external id needs this much chain evidence (08 S4/S5)
    align_k: int = 3                 # consecutive content matches to verify alignment (08 B1)
    checkpoint_every_turns: int = 20  # state_at replay spine (03 SS3.3)
    reserve_lost_turns: bool = True  # 2026-07-10 (Eranmor): a new turn whose user text is
    #                                  byte-identical to the PREVIOUS turn's, when that turn's
    #                                  reply settled EMPTY (lost stream), RE-SERVES the settled
    #                                  rolls on its [DIRECTIVE] instead of re-rolling — one
    #                                  player action, one resolution; no double cost/cooldown


class InjectionConfig(BaseModel):
    """Budget governor + placement (12 [injection], 01 SS8, 03 SS4, 06 B.1)."""
    max_tokens: int = 1200           # hard cap...
    max_fraction: float = 0.15       # ...whichever is smaller wins (needs known ctx)
    header_floor_tokens: int = 150   # below floor -> header-only; cap<=0 -> nothing (03 SS4)
    placement: str = "depth"         # depth | system_merge | suffix | st_native
    depth: int = 3                   # messages from the end (Q1)
    tc_marker: str = "{{aetherstate}}"
    briefing_style: str = "verbose"  # compression item 2 (2026-07-09): "compact" = dense
    #                                  key:val briefing notation (+ a one-line [KEY] legend on
    #                                  the DM contract under rpg) — leaner state blocks, same
    #                                  facts. OPT-IN for now (Bean's call); may become the
    #                                  API-class default after a live campaign verifies
    #                                  adherence. "verbose" (default) is byte-identical to 1.11
    assumed_ctx_tokens: int = 0      # 0 = unknown -> cap = max_tokens (probe fills this, P3)
    priorities: dict[str, int] = {"state_header": 100, "director_note": 80,
                                  "player_lessons": 71, "memories": 60,
                                  "relationship_belief": 40, "lore": 20}


class LinterConfig(BaseModel):
    """12 [linter] (03 SS9)."""
    enabled: bool = True
    rules_off: list[str] = []        # e.g. ["L6"] to silence timeline checks; ["L10"] the NLI check
    corrective_notes: bool = True    # false = detect + inspector only, never steer
    nli_threshold: float = 0.85      # L10 (03 SS9): min contradiction confidence to stage a note.
    #                                  Tune per model: local NLI classifiers (roberta/DeBERTa-MNLI)
    #                                  over-fire on RP prose — keep ~0.85-0.9; a chat-judge (main)
    #                                  can sit lower. Only consulted when linter_nli = assist|main.


class ConsentConfig(BaseModel):
    """Q13/Q14, 02 SS6."""
    mode: str = "strict"             # strict | negotiated | cnc | unrestricted (raw = inert for generation)
    safeword_scan: str = "user_only"  # user_only | both (raw: user commands/own-message only regardless)
    safewords: list[str] = []        # exact-match list; empty = only ((aether.freeze)) forms
    guard_escalate_turns: int = 3    # L9 escalated note duration (04 SS3.2)


class UserGuardConfig(BaseModel):
    """Q12."""
    enabled: bool = True
    mode: str = "prevent_and_correct"  # prevent | prevent_and_correct
    name: str = ""                   # empty -> extension/stamp-resolved persona -> heuristic (P5)
    aliases: list[str] = []


class ManualOverrideConfig(BaseModel):
    """Q11, 02 SS12b."""
    enabled: bool = False            # realism default: organic values evolve through play
    allow_ooc_set: bool = True       # ((aether.set)) obeys the same authority matrix either way


class DrivesConfig(BaseModel):
    """02 SS4.1."""
    craving_default_ramp: int = 5
    craving_default_satisfaction: int = 40
    withdrawal_level: int = 70
    withdrawal_dependency: int = 50
    dependency_per_consume: int = 2  # implementation constant ('rises with use frequency via rule')
    inject_threshold: int = 40       # drives below this stay out of the state header


class DirectorConfig(BaseModel):
    """12 [director]: beat engine + Tier-0 clock + stagnation knobs (03 SS8, 02 SS9)."""
    enabled: bool = True
    beat_libraries: list[str] = ["core_drama", "erp_tension", "erp_escalation",
                                 "erp_aftercare", "aftercare_checkin"]
    stagnation_ngram: int = 3
    stagnation_threshold: float = 0.82
    minutes_per_turn: int = 3        # Tier-0 clock advance default (03 R2)


class ExtractionConfig(BaseModel):
    """Tier-1 (12 [extraction], Q2, 03 SS5)."""
    mode: str = "main"               # off | rules | main | assist ("Q8 group shortcut", 12)
    lag_turns: int = 1               # settle-before-extract (swipe protection)
    debounce_s: float = 20.0         # idle flush (also settles the head turn — see jobs)
    live_recalc: bool = True         # 2026-07-07 (Bean): the NEWEST reply is ingested the
    #                                  instant its stream ends — the DM's world-tags commit and
    #                                  Tier-1 extraction flushes on the head turn's OWN cold path
    #                                  (was lag-1: the reply-before-last was the newest state saw).
    #                                  A same-turn swipe retracts narrator extraction, retires that
    #                                  range as skipped, and stores replacement prose without a
    #                                  second cold-state pass. false = legacy lag-1 settle-on-next-turn.
    cadence_turns: int = 1           # 2026-07-04: update state every N settled turns
    #                                  (1 = every turn, immediate). Idle flush still catches
    #                                  stragglers below the cadence so state never lags a walk-away.
    intake_chars: int = 12000        # transcript budget per extraction call: the new batch
    #                                  always ships whole; leftover budget prepends earlier
    #                                  turns as reference-only context (recency-first).
    batch_max_turns: int = 3         # turns per extraction call
    transient_batch_retries: int = 1  # one delayed whole-batch retry after persistent 429/5xx
    transient_retry_s: float = 2.0   # do not immediately hammer the same limited endpoint
    fail_autodisable_after: int = 5  # consecutive failed batches -> Tier-1 off for session (09 C2)
    fail_reenable_after_turns: int = 10
    language_hint: str = ""          # (08 E8)
    auto_entity_create: bool = True  # entity discovery (08 B2)
    max_tokens: int = 600            # extraction reply budget (04 SS6: ~150-400 typical)
    # Reasoning/thinking models (design decision 2026-07-03, ties into Q8 tiers):
    # low-budget users turn thinking OFF to save tokens; high-budget ON for delta quality.
    thinking: str = "auto"           # auto (on iff model supports it) | on | off
    thinking_max_tokens: int = 3000  # max_tokens when thinking is active (reasoning + output)
    trim_op_card: bool = False       # Q17: drop the ~300-tok OP CARD at schema rungs 1-2.
    #                                  Live eval #1: the drop cost recall (ops absent from
    #                                  shots were unlearnable) — quality default is FULL card.
    use_anyof: bool = True           # Q18 addendum: per-op anyOf schema at rung 2 where the
    #                                  endpoint's strict mode accepts it (capability-probed,
    #                                  caps.anyof; flat-schema fallback). Escape hatch, not
    #                                  a quality dial — anyOf is strictly tighter + cheaper.


class MemoryConfig(BaseModel):
    """12 [memory] (02 SS10, 03 SS7, Proxy SS4)."""
    top_k: int = 4                   # 3-5 sane range
    w_recency: float = 1.0           # equal-weight defaults (Park et al.); inspector tunes
    w_importance: float = 1.0
    w_relevance: float = 1.0
    recency_decay: float = 0.995
    prefilter_limit: int = 200       # candidates before scoring (08 L3)
    reflection_every_scenes: int = 3


class AssistEndpointConfig(BaseModel):
    """Q8 / 06 C — local-LLM sidecar."""
    name: str = "local"
    base_url: str = ""
    api_key: str = Field(default="", exclude=True, repr=False)
    credential_ref: str = ""
    model: str = ""
    tier: str = "small"              # nano | small | medium (06 C presets)
    max_concurrent: int = 1


class AssistGroupsConfig(BaseModel):
    """12 [assist.groups] — per-group off|rules|main|assist, all live-toggleable (Q8, 01 SS9b).
    extraction="" means unset: [extraction].mode (the documented shortcut) stays authoritative.
    Groups beyond extraction are parsed now, consumed in P4+."""
    extraction: str = ""
    director_selection: str = "rules"
    linter_nli: str = "rules"
    memory_reflection: str = "rules"
    embeddings: str = "off"
    lore_gen: str = "off"


class AssistGroupEndpointsConfig(BaseModel):
    """Optional per-group endpoint OVERRIDE (Q8) — the `name` of an [[assist.endpoints]] a group
    should use when its mode is 'assist'. Empty = the first endpoint (endpoints[0]), i.e. today's
    behaviour, so an all-empty table is byte-identical to 1.0. Lets e.g. linter_nli hit a LOCAL NLI
    box while memory_reflection hits a cloud chat model — different assist endpoints at once. An
    unknown name fails open to endpoints[0]."""
    extraction: str = ""
    director_selection: str = ""
    linter_nli: str = ""
    memory_reflection: str = ""
    embeddings: str = ""
    lore_gen: str = ""


class AssistConfig(BaseModel):
    endpoints: list[AssistEndpointConfig] = []
    groups: AssistGroupsConfig = Field(default_factory=AssistGroupsConfig)
    group_endpoints: AssistGroupEndpointsConfig = Field(
        default_factory=AssistGroupEndpointsConfig)


class SpecializationConfig(BaseModel):
    """Q27 / doc 05: narrative-mode profile. name='none' is byte-identical to pre-RPG
    behaviour (invariant 3 — non-RPG sessions never see RPG blocks or the DM guard). When
    name='rpg' the built-in RPG_PROFILE supplies lower-priority DEFAULTS for OTHER sections
    (injection priorities today; beats/knobs as later phases land); the user's own config
    always wins (overlay applied in load_config). The fields below are consulted ONLY when
    name == 'rpg' and are the profile's own knobs a table may override normally."""
    name: str = "none"               # none | rpg
    semantic_truth_gate: bool = False  # Revision-4 Semantic Layer: buffer every RPG reply,
    #                                  commit a proof-complete code fallback with mechanics,
    #                                  and release only a terminal canonical-visible artifact.
    #                                  This stays opt-in until the bounded upstream plan-selection
    #                                  path is proof-complete; fallback-only narration is not a
    #                                  playable RPG default. Non-RPG relay transparency remains
    #                                  byte-exact.
    narration_pre_display_guard: bool = True
    #                                  Default-on, RPG-only contradiction prevention. Current
    #                                  mechanically authoritative narrator turns are buffered and
    #                                  checked before visibility; truthful rich prose is released
    #                                  unchanged, while definite uncommitted roll/harm/death claims
    #                                  become a code-authored safe result. Pure RP and `none` keep
    #                                  the ordinary transparent stream.
    blocks: list[str] = ["PLAYER", "EFFECTS", "GEAR", "INVENTORY", "FACTIONS",
                         "RELATIONS", "NEARBY", "QUEST", "WORLD", "DIRECTIVE"]   # doc 05 §6
    #                      catalog (+ NEARBY: 0b home anchors, 2026-07-09)
    dm_guard: bool = True            # DM/Game-Master framing of the Q12 user guard (05 §3.2)
    dice: str = "2d6"                # D1 resolution dice knob   (consumed at RPG-1)
    tiers: str = "pbta3"             # resolution tier model     (consumed at RPG-1)
    nemesis_enabled: bool = False    # RPG-3b: single-nemesis machinery (D6 — off by default;
    #                                  gates the one_nemesis linter rule, not the op itself)
    faction_cascade: float = 0.1     # RPG-3b (05 §5.4): NPC->faction affinity ripple factor
    #                                  (negatives halved; 0 disables the cascade entirely)
    contract: str = "full"           # RPG-4 (05 §5.9/D7): DM rules-contract size — "full"
    #                                  for strong models, "compact" for weak/local budgets
    auto_compact_contract: bool = False   # A1 (2026-07-10, Bean): on calm, ESTABLISHED turns
    #                                  auto-flip the DM rules-contract to its ~40-tok compact form
    #                                  (the model has internalized the full rules by then — the big
    #                                  per-turn token + reasoning cut). The FULL contract still rides
    #                                  the first `contract_full_turns` turns and EVERY combat turn.
    #                                  Opt-in: off = the contract size is fixed by `contract` (an rpg
    #                                  session stays byte-identical until the table enables this).
    #                                  Ignored when `contract == "compact"` (already compact).
    contract_full_turns: int = 3     # A1: keep the FULL contract for this many opening turns before
    #                                  auto-compact may kick in (0 = compact-eligible from turn 1)
    combat_opening_primer: bool = True  # Private, non-canonical worked examples on the
    #                                  opening combat requests only. They demonstrate how several
    #                                  code-owned rolls become vivid prose without exposing dice,
    #                                  inventing mechanics, or telegraphing a fresh foe's first move.
    #                                  Live-disable this experiment without changing combat state.
    enemy_rolls: bool = True         # R8c (2026-07-09, Bean): pre-roll ONE enemy-action die
    #                                  per turn and hand it to the DM via [OPPOSITION] — foes
    #                                  attack on real dice, resolved BEFORE the reply streams
    hardcore: bool = False           # RPG-5 (doc 10 §7): defeat_resolve routes to DEATH —
    #                                  permadeath; off = contextual non-lethal outcomes
    war_room: bool = True            # Phase 1 (plan doc 13, ratified 2026-07-09): combatant
    #                                  instances (extras + tracked NPCs, 3v3), code-derived
    #                                  player strike damage, [ALLY] dice, code-detected
    #                                  defeat -> XP + frozen loot, the [WAR] board, the
    #                                  [foe]/[clash] tag channels, and the War Room HUD lane.
    #                                  off = 1.12 combat behavior (R8c/[hp] only)
    large_battle: bool = True        # §F (2026-07-10, Bean): large-scale battle — the Player
    #                                  fights their MICRO 3v3 slice on the dice while the MACRO
    #                                  battle lives in PROSE; a code-owned tide (losing/holding/
    #                                  winning) the DM reports via [tide], and fresh WAVES press
    #                                  the War Room until it turns. off = no [BATTLE]/waves.
    foe_floor: bool = True           # 2026-07-10 (Eranmor floor): the Player ATTACKING a
    #                                  target the DM's own last reply narrated (but never
    #                                  tagged [foe]) stages it as an enemy combatant — the
    #                                  War Room opens even with a zero-protocol narrator.
    #                                  Basis-gated: every name token must appear in the DM's
    #                                  prose (the fiction grounds it, never the player alone)
    intent_floor: bool = True        # 2026-07-10 (Bean): the pure-code semantic REFLEX floor —
    #                                  intent by MEANING, no model/network (invariant-2 safe).
    #                                  (a) a light stemmer + curated intent lexicon so natural
    #                                  phrasings ("sweet-talk", "sneaked", "haggle") map to the
    #                                  owned skill they mean, not just literal `governs` words;
    #                                  (b) the entity-aware target picker — a strike resolves to
    #                                  a PRESENT cast member, never a token run off the prose
    #                                  (ends the 'Out'/'Pines'/'Shortsword' phantom-foe class).
    #                                  Quoted speech is narration, not a performed action, so its
    #                                  skill/attack words cannot roll or stage combat.
    #                                  off = the exact-match keyword floor (v1.17.0 behavior)
    stealth_kills: bool = True       # 2026-07-10 (Bean): out-of-combat kill gating. Outside an
    #                                  active fight a DECLARED kill on a present target is a
    #                                  NON-MOVE unless routed — a Stealth/concealed approach makes
    #                                  it a real roll (success = silent kill + XP), or a grand
    #                                  working (epic/mythic scope, ritual/reality-warp) kills by
    #                                  prose + XP. Off = the model narrates kills unchecked.
    living_world: bool = True        # Phase 2 (plan doc 13, ratified 2026-07-09): the
    #                                  living-world referee — travel consumes clock segments,
    #                                  idle turns auto-advance the clock, authored faction
    #                                  fronts tick deterministically and FILL into world
    #                                  events, rumor-gated in HUD/briefing (Console shows
    #                                  all). off = 1.13 behavior (clock moves only by hand)
    clock_turns: int = 6             # Phase 2: idle auto-tick — advance one time segment
    #                                  after this many turns with no real time passing
    #                                  (0 disables the idle floor; travel still costs time)
    narrator_card_dir: str = ""      # optional: a SillyTavern characters dir where the
    #                                  world-specific Narrator card (narrator.py) is installed
    #                                  on request; empty = download-only (never writes out)


# Built-in RPG specialization profile (Q27 / doc 05 §7): lower-priority DEFAULTS overlaid
# UNDER the user's config when [specialization].name == 'rpg' (see _apply_specialization).
# Every value here is a default a table may override; the overlay only fills gaps, so the
# effective precedence is user-override > profile > base-default. Non-RPG loads never touch
# it. Keep entries to things a later RPG phase actually consumes — no dead config.
RPG_PROFILE: dict[str, Any] = {
    "specialization": {
        "semantic_truth_gate": False,
        "narration_pre_display_guard": True,
    },
    "director": {
        # RPG-5: the adventure beat pack rides the profile default (user's own list wins).
        "beat_libraries": ["core_drama", "erp_tension", "erp_escalation",
                           "erp_aftercare", "aftercare_checkin", "rpg_adventure"],
    },
    "injection": {
        # RPG genuinely injects more than chat — the whole sheet PLUS the DM rules-contract.
        # A bigger budget (Bean 07-07) keeps the state blocks AND the contract from colliding
        # at the default 1200 (the contract alone is ~1k tokens). The user's own value wins.
        "max_tokens": 2400,
        # 2026-07-10 (Eranmor): volatile state sits DIRECTLY above the Player's newest
        # message — depth 3 put the [DIRECTIVE] before the previous exchange in reading
        # order, and GLM burned reasoning deciding whether it was stale ("the [DIRECTIVE]
        # mentioned earlier... from before"). depth 1 also moves the provider prompt-cache
        # cut later (longer stable prefix). The user's own value wins.
        "depth": 1,
        # RPG header-class ranking (doc 05 §6): directive very high so it is never
        # budget-dropped, player_card high, then quest/relations/factions/gear/inventory/
        # world. A superset of the base priorities so no base class is lost on override.
        "priorities": {
            # RPG ordering (Bean 07-07): the DM rules-contract is what MAKES rpg mode an RPG —
            # it must survive budget pressure, so it now outranks memories. The state header +
            # directive stay top (never dropped); the sheet/quest/social blocks follow.
            "state_header": 100, "directive": 98, "player_card": 90, "director_note": 80,
            "combat_primer": 74, "rules_contract": 72, "player_lessons": 71,
            "quest": 70,
            "relations": 66, "factions": 62,
            "gear": 58, "effects": 56, "inventory": 54,
            "world": 50, "memories": 60,
            "relationship_belief": 40, "lore": 20,
        },
    },
}


class PrivacyConfig(BaseModel):
    trace_level: str = "meta"        # off | meta | full
    trace_ring: int = 200
    log_prose: bool = False
    backups_keep: int = 3
    # telemetry: THERE IS NO KEY. It does not exist to be enabled.


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    creator: CreatorConfig = Field(default_factory=CreatorConfig)
    stamp: StampConfig = Field(default_factory=StampConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    injection: InjectionConfig = Field(default_factory=InjectionConfig)
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    user_guard: UserGuardConfig = Field(default_factory=UserGuardConfig)
    manual_override: ManualOverrideConfig = Field(default_factory=ManualOverrideConfig)
    drives: DrivesConfig = Field(default_factory=DrivesConfig)
    director: DirectorConfig = Field(default_factory=DirectorConfig)
    linter: LinterConfig = Field(default_factory=LinterConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    memory: MemoryConfig = MemoryConfig()
    assist: AssistConfig = Field(default_factory=AssistConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    specialization: SpecializationConfig = Field(default_factory=SpecializationConfig)
    # Later phases append their sections here (memory/linter/degradation/ui)
    source: str = "defaults"         # which config actually loaded: file | last_known_good | defaults
    source_path: str = ""            # absolute path the config loaded from (where Console saves write back)
    persistence_enabled: bool = True  # runtime-only fuse; isolated read-only launches disable saves

    @model_validator(mode="after")
    def _sync_extraction_group(self) -> "Config":
        """12: [assist.groups].extraction is canonical when set; [extraction].mode is the
        shortcut. After validation extraction.mode always holds the effective value."""
        g = self.assist.groups.extraction
        if g:
            self.extraction.mode = g
        else:
            self.assist.groups.extraction = self.extraction.mode
        return self


def _env_overrides() -> dict[str, dict[str, Any]]:
    """AETHERSTATE_SECTION__KEY=value -> {section: {key: value}} (strings; pydantic coerces)."""
    out: dict[str, dict[str, Any]] = {}
    for key, val in os.environ.items():
        if not key.startswith("AETHERSTATE_") or "__" not in key:
            continue
        section, _, field = key[len("AETHERSTATE_"):].lower().partition("__")
        out.setdefault(section, {})[field] = val
    return out


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _apply_specialization(user: dict) -> dict:
    """Overlay the built-in profile UNDER the user's config so precedence is
    user-override > profile > base-default (doc 05 §7). No-op unless [specialization].name
    resolves to a known profile. Never raises (invariant 1: config never blocks startup)."""
    try:
        name = str((user.get("specialization") or {}).get("name", "none")).lower()
    except Exception:
        return user
    profile = {"rpg": RPG_PROFILE}.get(name)
    if not profile:
        return user
    import copy
    return _merge(copy.deepcopy(profile), user)   # user keys win over profile keys


def load_config(path: str | Path | None, *, read_only: bool = False) -> Config:
    """Never raises. Return a valid config, optionally without any writable source binding.

    ``read_only`` is for isolated live tests that borrow the personal connection settings.  It
    still reads the named file (or its existing last-known-good fallback), but never refreshes the
    backup, never retains a Console persistence target, and disables every Console config save.
    """
    data: dict[str, Any] = {}
    source = "defaults"
    if path:
        p = Path(path)
        bak = p.with_suffix(p.suffix + ".bak")
        for candidate, label in ((p, "file"), (bak, "last_known_good")):
            if not candidate.is_file():
                continue
            try:
                raw = tomllib.loads(candidate.read_text(encoding="utf-8"))
                cfg = Config.model_validate(
                    _apply_specialization(_merge(dict(raw), _env_overrides())))
                cfg.source = label
                cfg.persistence_enabled = not read_only
                cfg.source_path = "" if read_only else str(p)
                if label == "file" and not read_only:
                    # Normal personal startup refreshes last-known-good (09 F1).  An isolated
                    # process borrowing this file has no write authority over either source.
                    try:
                        shutil.copyfile(p, bak)
                    except OSError:
                        pass
                return cfg
            except Exception:
                continue
    data = _apply_specialization(_merge(data, _env_overrides()))
    try:
        cfg = Config.model_validate(data)
    except Exception:
        cfg = Config()
    cfg.source = source
    cfg.persistence_enabled = not read_only
    if path and not read_only:
        cfg.source_path = str(Path(path))
    return cfg
