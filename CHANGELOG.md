# Changelog

## 1.0.0 — 2026-07-04

First public release.

- Transparent, streaming-safe OpenAI-compatible proxy with a fail-open guarantee: AetherState
  can never block, edit, or crash the story stream.
- Session engine: multi-chat identity (per-request sentinel wins over stale headers), branch
  alignment, swipe/regenerate handling, duplicate-request protection.
- Two-stage genesis seeding from the character card + greeting (rules pass inline, full-matrix
  helper-LLM pass in the background), with `/aether-genesis` force re-seed.
- Tier-1 extraction ladder with capability probing (native grammar → strict JSON schema →
  JSON mode → freeform), per-op validation, quarantine, and entity discovery.
- User-set update cadence (`cadence_turns`, 1 = every turn) and transcript intake budget
  (`intake_chars`) — newest turns always ship whole, leftover budget carries earlier context.
- Idle settle + restart recovery: the newest turn extracts without waiting for your next
  message, and pending work resumes after a proxy restart.
- Memory tiers (episodic → summaries → durable facts) with recall injection; director beats;
  consistency linter; consent/safeword system; user-voice guard.
- Built-in web Console (sessions, live state view/edit, connection setup with real auth test).
- SillyTavern Companion extension: panel, slash commands, turn-0 seeding, cadence controls.
- Antivirus hardening baked in (TLS truststore injection, SSLKEYLOGFILE workaround).
