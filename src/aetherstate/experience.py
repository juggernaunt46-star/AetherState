"""Per-session product experience binding, separate from relay enrichment mode."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from .config import Config, build_effective_config

CHAT = "chat"
RPG = "rpg"


@dataclass(frozen=True)
class ExperienceBinding:
    mode: str
    source: str = ""
    locked_turn: Optional[int] = None
    core_fingerprint: str = ""
    character_actor_id: str = ""
    persona_actor_id: str = ""

    @property
    def locked(self) -> bool:
        return self.locked_turn is not None


class ExperienceModeLocked(RuntimeError):
    def __init__(self, binding: ExperienceBinding) -> None:
        super().__init__("session experience mode is locked")
        self.binding = binding


def normalize_experience_mode(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"", "none", "chat"}:
        return CHAT
    if raw == RPG:
        return RPG
    raise ValueError("experience mode must be chat|rpg")


def config_for_experience(cfg: Config, mode: str) -> Config:
    return build_effective_config(
        cfg,
        "rpg" if normalize_experience_mode(mode) == RPG else "none",
    )


def _row_value(row: object, key: str, default: object = "") -> object:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return default


def infer_legacy_experience(
    row: object,
    state: object,
    stamp: object,
    *,
    fallback: str = CHAT,
) -> tuple[str, str]:
    """Resolve legacy/card evidence without allowing it to override explicit player choice."""
    persisted = normalize_experience_mode(_row_value(row, "experience_mode", ""))
    source = str(_row_value(row, "experience_mode_source", "") or "")
    locked_turn = _row_value(row, "experience_mode_locked_turn", None)
    if locked_turn is not None:
        return persisted, source or "locked"
    if source == "explicit":
        return persisted, source

    card_mode = getattr(stamp, "mode", None) if stamp is not None else None
    if str(card_mode or "").strip():
        return normalize_experience_mode(card_mode), "stamp"

    role = str(getattr(stamp, "card_role", "") or "").strip().lower()
    speaker = str(getattr(stamp, "speaker", "") or "").strip()
    stored_speaker = str(_row_value(row, "narrator_speaker", "") or "").strip()
    state_map = state if isinstance(state, Mapping) else {}
    players = state_map.get("player") if isinstance(state_map, Mapping) else None
    has_player_card = isinstance(players, Mapping) and bool(players)
    if role == "narrator":
        return RPG, "card:narrator"
    if role == "character":
        return CHAT, "card:character"
    if stored_speaker and (
        not speaker or speaker.casefold() == stored_speaker.casefold()
    ):
        return RPG, "stored:narrator"
    if has_player_card:
        return RPG, "state:player-card"
    return normalize_experience_mode(fallback), "default"
