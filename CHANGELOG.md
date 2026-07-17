# Changelog

## 1.22.0 — 2026-07-17

This release is a cumulative public sync of AetherState's local-first narrative engine, RPG layer,
and Player-controlled semantic systems. The model still narrates; code-owned mechanics and the
Ledger remain authoritative for outcomes and world truth.

- **Player Lessons:** Players can explicitly test, save, inspect, revise, disable, re-enable, and
  securely remove narration preferences and bounded action/target intent corrections in the
  Console. Nothing is mined from chats. Narration preferences have visible provider disclosure;
  intent lesson prose stays local.
- **PlayerLex and Semantic Atlas:** Players can approve local names, aliases, and bounded patterns
  for one exact current meaning. The Atlas exposes 311 meanings across CapabilityLex, ReferentLex,
  SceneLex, and ActionLex. Recognition never grants a capability or settles an outcome.
- **Retry and replay safety:** Fresh-turn identities, frozen receipts, current-revision checks, and
  duplicate-delivery guards prevent semantic learning or committed mechanics from being casually
  reapplied on retries, swipes, Continue, replay, or lost replies.
- **RPG and world systems:** The public source now includes the current Player HUD, Creator,
  code-owned checks and resources, progression, relationships, factions, quests, combatants,
  grounded enemy kits, deterministic enemy intent, War Room, semantic binding, narrator transfer,
  and pre-display truth checks.
- **Local privacy and lifecycle:** Player-approved records live in the local database. Secure removal
  covers the active AetherState database, WAL, and owned process caches, without claiming to recall
  external backups or content already received by a configured provider.
- **Faster SillyTavern install:** Windows Players can run `Install-AetherState.bat`; Linux Players can
  run `./install-aetherstate.sh`. Both install the companion, create the private environment and
  configuration, and launch AetherState, with SillyTavern auto-detection and a path fallback.
- **Public release hygiene:** The release includes public source, tests, runtime corpora,
  documentation, launchers, and the SillyTavern companion while excluding local configuration,
  databases, logs, traces, credentials, private evidence, and developer-only experiments.

Known limits remain explicit: the semantic truth gate is off by default, active Player damage is
currently single-target, recognized language is not a complete area-of-effect mechanic, and learned
text cannot create authority or world truth.
