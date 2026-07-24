"""Portable, non-RPG SillyTavern Character Cards for AetherState Chat."""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

CORE_SCHEMA = "aetherstate-character-core/1"
CORE_FINGERPRINT_DOMAIN = b"aetherstate-chat-core/1\0"
ENVELOPE_FINGERPRINT_DOMAIN = b"aetherstate-chat-card-envelope/1\0"
WORLD_FINGERPRINT_DOMAIN = b"aetherstate-chat-world/1\0"

_CORE_STRING_CAPS = {
    "name": 160,
    "description": 12000,
    "personality": 8000,
    "scenario": 12000,
    "first_message": 8000,
    "example_dialogue": 12000,
}
_CORE_LIST_CAPS = {"anchors": (48, 1000), "boundaries": (48, 1000)}
PERSONA_KEY_CAP = 512


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value must be finite JSON") from exc


def _fingerprint(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical(value)).hexdigest()


def _string(value: object, *, cap: int, field: str, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Character Core {field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"Character Core {field} is required")
    if len(text) > cap:
        raise ValueError(f"Character Core {field} exceeds {cap} characters")
    return text


def _string_list(value: object, *, field: str, count: int, cap: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Character Core {field} must be a list")
    if len(value) > count:
        raise ValueError(f"Character Core {field} exceeds {count} entries")
    out = []
    for item in value:
        text = _string(item, cap=cap, field=field)
        if text:
            out.append(text)
    return out


def ordinary_core(card: Mapping[str, object]) -> dict:
    """Normalize ordinary V2 fields into the immutable Character Core shape."""
    if not isinstance(card, Mapping):
        raise ValueError("ordinary character card must be an object")
    value = {
        "schema": CORE_SCHEMA,
        "revision": 1,
        "name": card.get("name", ""),
        "description": card.get("description", ""),
        "personality": card.get("personality", ""),
        "scenario": card.get("scenario", ""),
        "first_message": card.get("first_mes", card.get("first_message", "")),
        "example_dialogue": card.get(
            "mes_example", card.get("example_dialogue", ""),
        ),
        "anchors": [],
        "boundaries": [],
    }
    return validate_core(value)


def validate_core(value: object) -> dict:
    """Return one closed, JSON-only Character Core or reject the whole value."""
    if not isinstance(value, Mapping):
        raise ValueError("Character Core must be an object")
    if value.get("schema") != CORE_SCHEMA:
        raise ValueError(f"Character Core schema must be {CORE_SCHEMA}")
    if value.get("revision") != 1 or isinstance(value.get("revision"), bool):
        raise ValueError("Character Core revision must be 1")
    out: dict[str, Any] = {"schema": CORE_SCHEMA, "revision": 1}
    for field, cap in _CORE_STRING_CAPS.items():
        out[field] = _string(
            value.get(field, ""),
            cap=cap,
            field=field,
            required=field == "name",
        )
    for field, (count, cap) in _CORE_LIST_CAPS.items():
        out[field] = _string_list(
            value.get(field, []), field=field, count=count, cap=cap,
        )
    _canonical(out)
    return out


def core_fingerprint(core: Mapping[str, object]) -> str:
    return _fingerprint(CORE_FINGERPRINT_DOMAIN, validate_core(core))


def validate_persona_key(value: object) -> str:
    """Validate one exact SillyTavern avatar key without normalizing its identity bytes."""
    if not isinstance(value, str) or not value or value.isspace():
        raise ValueError("an exact Persona avatar key is required")
    if len(value) > PERSONA_KEY_CAP:
        raise ValueError(f"Persona avatar key exceeds {PERSONA_KEY_CAP} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Persona avatar key contains control characters")
    return value


def _world(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Chat World must be an object")
    clean = {
        str(key): item
        for key, item in value.items()
        if key not in {"notes", "creative_direction"}
    }
    _canonical(clean)
    if len(_canonical(clean)) > 256_000:
        raise ValueError("Chat World exceeds the portable card limit")
    return json.loads(_canonical(clean))


def world_fingerprint(world: dict | None) -> str:
    clean = _world(world)
    return _fingerprint(WORLD_FINGERPRINT_DOMAIN, clean) if clean is not None else ""


def chat_envelope_fingerprint(
    core: Mapping[str, object], world: dict | None,
) -> str:
    return _fingerprint(
        ENVELOPE_FINGERPRINT_DOMAIN,
        {"core": validate_core(core), "world": _world(world)},
    )


def build_card(
    core: Mapping[str, object],
    *,
    world: dict | None = None,
    continuity: dict | None = None,
) -> dict:
    validated = validate_core(core)
    portable_world = _world(world)
    from .chat_continuity import validate_starting_continuity

    portable_continuity = (
        validate_starting_continuity(continuity) if continuity is not None else None
    )
    seed = {}
    if portable_world is not None:
        seed["world"] = portable_world
    if portable_continuity is not None:
        seed["continuity"] = portable_continuity
    metadata = {
        "role": "character",
        "mode": "chat",
        "core": validated,
        "core_fingerprint": core_fingerprint(validated),
        "seed": seed,
        "core_envelope_fingerprint": chat_envelope_fingerprint(
            validated, portable_world,
        ),
    }
    data = {
        "name": validated["name"],
        "description": validated["description"],
        "personality": validated["personality"],
        "scenario": validated["scenario"],
        "first_mes": validated["first_message"],
        "mes_example": validated["example_dialogue"],
        "creator_notes": "",
        "system_prompt": "",
        "post_history_instructions": "",
        "alternate_greetings": [],
        "tags": ["aetherstate", "chat", "character"],
        "creator": "AetherState",
        "character_version": "aether-chat-1.0",
        "extensions": {"aetherstate": metadata},
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def _json_card(payload: bytes) -> dict:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid character card JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("character card JSON must be an object")
    return value


def _png_card(payload: bytes) -> dict:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid character card PNG")
    offset = 8
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset:offset + 4], "big")
        end = offset + 12 + length
        if end > len(payload):
            break
        kind = payload[offset + 4:offset + 8]
        data = payload[offset + 8:offset + 8 + length]
        offset = end
        if kind == b"tEXt":
            keyword, separator, encoded = data.partition(b"\x00")
            if keyword == b"chara" and separator:
                try:
                    return _json_card(base64.b64decode(encoded, validate=True))
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid embedded character card") from exc
    raise ValueError("PNG does not contain a V2 character card")


def inspect_card_json_or_png(payload: bytes, content_type: str) -> dict:
    """Inspect bounded bytes and return ordinary V2 fields plus valid Chat metadata."""
    if not isinstance(payload, bytes) or len(payload) > 4_000_000:
        raise ValueError("character card payload exceeds the inspection limit")
    card = _png_card(payload) if "png" in str(content_type).lower() else _json_card(payload)
    data = card.get("data")
    if card.get("spec") != "chara_card_v2" or not isinstance(data, dict):
        raise ValueError("payload is not a V2 character card")
    ordinary = {
        key: data.get(key, "")
        for key in (
            "name", "description", "personality", "scenario", "first_mes", "mes_example",
        )
    }
    ordinary["extensions"] = {}
    extensions = data.get("extensions")
    metadata = extensions.get("aetherstate") if isinstance(extensions, dict) else None
    if isinstance(metadata, dict):
        try:
            core = validate_core(metadata.get("core"))
            world = (metadata.get("seed") or {}).get("world") \
                if isinstance(metadata.get("seed"), dict) else None
            continuity = (metadata.get("seed") or {}).get("continuity") \
                if isinstance(metadata.get("seed"), dict) else None
            from .chat_continuity import validate_starting_continuity

            portable_continuity = (
                validate_starting_continuity(continuity)
                if continuity is not None else None
            )
            if metadata.get("role") != "character" or metadata.get("mode") != "chat" \
                    or metadata.get("core_fingerprint") != core_fingerprint(core) \
                    or metadata.get("core_envelope_fingerprint") != \
                    chat_envelope_fingerprint(core, world):
                raise ValueError("Chat card metadata fingerprint is invalid")
            seed = {}
            if world is not None:
                seed["world"] = _world(world)
            if portable_continuity is not None:
                seed["continuity"] = portable_continuity
            ordinary["extensions"]["aetherstate"] = {
                "role": "character",
                "mode": "chat",
                "core": core,
                "core_fingerprint": core_fingerprint(core),
                "seed": seed,
                "core_envelope_fingerprint": chat_envelope_fingerprint(core, world),
            }
        except ValueError:
            pass
    return ordinary
