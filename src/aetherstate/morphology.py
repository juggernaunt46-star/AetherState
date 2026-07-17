"""Small, domain-neutral morphology helpers for shared semantic grammar."""
from __future__ import annotations

from collections.abc import Iterable


def productive_compound_head(
    token: str,
    ordered_heads: Iterable[str],
    min_modifier_chars: int = 3,
) -> str | None:
    """Return the first licensed head of a compact productive compound.

    The caller owns the vocabulary and its precedence.  This helper deliberately does not
    normalize case, invent aliases, or decide what the returned head authorizes.
    """
    if min_modifier_chars < 0:
        raise ValueError("min_modifier_chars must be non-negative")
    for head in ordered_heads:
        if not head or token == head or not token.endswith(head):
            continue
        if len(token) - len(head) >= min_modifier_chars:
            return head
    return None
