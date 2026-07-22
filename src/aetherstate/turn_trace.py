"""One fail-closed, content-free persistence gate for local turn diagnostics."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional


TURN_TRACE_SCHEMA = "aetherstate-turn-trace/2"
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,191}\Z")
_SAFE_FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def text_receipt(value: object) -> dict[str, Any]:
    text = str(value or "")
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def canonical_sha256(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def safe_token(value: object) -> Optional[str]:
    text = value if isinstance(value, str) else ""
    return text if _SAFE_TOKEN_RE.fullmatch(text) is not None else None


def _assert_content_free(value: object, path: str = "trace") -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if value and safe_token(value) is None:
            raise ValueError(f"unsafe string at {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _SAFE_FIELD_RE.fullmatch(key) is None:
                raise ValueError(f"unsafe field at {path}")
            _assert_content_free(item, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_content_free(item, f"{path}[{index}]")
        return
    raise ValueError(f"unsupported value at {path}")


def emit_turn_trace(logger: logging.Logger, payload: Mapping[str, Any]) -> bool:
    """Emit one versioned trace row only when every persisted scalar is content-free.

    The gate deliberately drops an unsafe diagnostic row rather than risk copying authored prose
    into either the rotating JSONL file or ordinary process-log handlers.
    """
    try:
        row = {"trace_schema": TURN_TRACE_SCHEMA, **dict(payload)}
        _assert_content_free(row)
        encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("turn trace dropped unsafe payload")
        return False
    logger.info("TURN_TRACE %s", encoded)
    return True
