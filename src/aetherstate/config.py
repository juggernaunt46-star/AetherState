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


class UpstreamConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""                  # DEFAULT model for engine-initiated calls (creator
    #                                  authoring, genesis stage B) when nothing has been
    #                                  proxied yet. The RELAY never uses it — the frontend
    #                                  names its own model per request. Console-set.
    force_rung: int = 0
    probe_ttl_days: int = 7
    idle_timeout_s: int = 0          # 0 = no proxy-imposed stream timeout (09 U6)
    max_parse_mb: int = 20


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


class InjectionConfig(BaseModel):
    """Budget governor + placement (12 [injection], 01 SS8, 03 SS4, 06 B.1)."""
    max_tokens: int = 1200           # hard cap...
    max_fraction: float = 0.15       # ...whichever is smaller wins (needs known ctx)
    header_floor_tokens: int = 150   # below floor -> header-only; cap<=0 -> nothing (03 SS4)
    placement: str = "depth"         # depth | system_merge | suffix | st_native
    depth: int = 3                   # messages from the end (Q1)
    tc_marker: str = "{{aetherstate}}"
    assumed_ctx_tokens: int = 0      # 0 = unknown -> cap = max_tokens (probe fills this, P3)
    priorities: dict[str, int] = {"state_header": 100, "director_note": 80, "memories": 60,
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
    #                                  A swipe retracts the extracted turn (retract_extraction_at)
    #                                  and re-derives it. false = legacy lag-1 settle-on-next-turn.
    cadence_turns: int = 1           # 2026-07-04: update state every N settled turns
    #                                  (1 = every turn, immediate). Idle flush still catches
    #                                  stragglers below the cadence so state never lags a walk-away.
    intake_chars: int = 12000        # transcript budget per extraction call: the new batch
    #                                  always ships whole; leftover budget prepends earlier
    #                                  turns as reference-only context (recency-first).
    batch_max_turns: int = 3         # turns per extraction call
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
    api_key: str = ""
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
    blocks: list[str] = ["PLAYER", "EFFECTS", "GEAR", "INVENTORY", "FACTIONS",
                         "RELATIONS", "QUEST", "WORLD", "DIRECTIVE"]   # doc 05 §6 catalog
    dm_guard: bool = True            # DM/Game-Master framing of the Q12 user guard (05 §3.2)
    dice: str = "2d6"                # D1 resolution dice knob   (consumed at RPG-1)
    tiers: str = "pbta3"             # resolution tier model     (consumed at RPG-1)
    nemesis_enabled: bool = False    # RPG-3b: single-nemesis machinery (D6 — off by default;
    #                                  gates the one_nemesis linter rule, not the op itself)
    faction_cascade: float = 0.1     # RPG-3b (05 §5.4): NPC->faction affinity ripple factor
    #                                  (negatives halved; 0 disables the cascade entirely)
    contract: str = "full"           # RPG-4 (05 §5.9/D7): DM rules-contract size — "full"
    #                                  for strong models, "compact" for weak/local budgets
    hardcore: bool = False           # RPG-5 (doc 10 §7): defeat_resolve routes to DEATH —
    #                                  permadeath; off = contextual non-lethal outcomes
    narrator_card_dir: str = ""      # optional: a SillyTavern characters dir where the
    #                                  world-specific Narrator card (narrator.py) is installed
    #                                  on request; empty = download-only (never writes out)


# Built-in RPG specialization profile (Q27 / doc 05 §7): lower-priority DEFAULTS overlaid
# UNDER the user's config when [specialization].name == 'rpg' (see _apply_specialization).
# Every value here is a default a table may override; the overlay only fills gaps, so the
# effective precedence is user-override > profile > base-default. Non-RPG loads never touch
# it. Keep entries to things a later RPG phase actually consumes — no dead config.
RPG_PROFILE: dict[str, Any] = {
    "director": {
        # RPG-5: the adventure beat pack rides the profile default (user's own list wins).
        "beat_libraries": ["core_drama", "erp_tension", "erp_escalation",
                           "erp_aftercare", "aftercare_checkin", "rpg_adventure"],
    },
    "injection": {
        # RPG genuinely injects more than chat — the whole sheet PLUS the DM rules-contract.
        # A bigger budget (Bean 07-07) keeps the state blocks AND the contract from colliding
        # at the default 1200 (the contract alone is ~1k tokens). The user's own value wins.
        "max_tokens": 2200,
        # RPG header-class ranking (doc 05 §6): directive very high so it is never
        # budget-dropped, player_card high, then quest/relations/factions/gear/inventory/
        # world. A superset of the base priorities so no base class is lost on override.
        "priorities": {
            # RPG ordering (Bean 07-07): the DM rules-contract is what MAKES rpg mode an RPG —
            # it must survive budget pressure, so it now outranks memories. The state header +
            # directive stay top (never dropped); the sheet/quest/social blocks follow.
            "state_header": 100, "directive": 98, "player_card": 90, "director_note": 80,
            "rules_contract": 72, "quest": 70, "relations": 66, "factions": 62,
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


def load_config(path: str | Path | None) -> Config:
    """Never raises. Returns a valid Config with .source recording what was loaded."""
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
                cfg.source_path = str(p)
                if label == "file":  # write last-known-good on every successful load (09 F1)
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
    if path:
        cfg.source_path = str(Path(path))
    return cfg
