"""Typed proposition identity and audience-aware knowledge retrieval.

This module is deliberately read-only.  It joins Claim Records, actor-relative
epistemics, accepted facts, and admitted world events without allowing any one
record family to acquire another family's authority.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from copy import deepcopy
from typing import Any, Mapping

from .capability_glossary import content_fingerprint


_NEGATION = re.compile(
    r"\b(?:not|never|no\s+longer|cannot|can't|didn't|doesn't|isn't|wasn't|won't|wouldn't)\b",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_'-]*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_FRONT_COMPLETION_CAUSE = re.compile(r"front:([^:]+):completion")
_VISIBILITIES = frozenset({"public", "player", "actor_scoped", "hidden"})
_PLAYER_SAFE_AUDIENCES = frozenset({"player", "narrator"})
_ACTOR_REF_SCHEMA = "aetherstate-world-subject-ref/1"
_ACTOR_REF_KINDS = frozenset({"actor", "npc", "enemy", "faction", "world"})
_ACTOR_PREFIXES = frozenset({"actor", "npc", "enemy", "player", "entity", "character"})
_ACTOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def normalized_proposition(statement: object) -> tuple[str, str]:
    """Return a stable proposition core and its surface polarity.

    Polarity is intentionally outside proposition identity.  This lets
    ``the gate is open`` and ``the gate is not open`` form one conflict group
    while preserving their opposite asserted values on the Claim Records.
    """
    text = unicodedata.normalize("NFKC", str(statement or "")).casefold()
    polarity = "negative" if _NEGATION.search(text) else "positive"
    core = _NEGATION.sub(" ", text)
    core = " ".join(_TOKEN.findall(core))
    core = _SPACE.sub(" ", core).strip()
    return core, polarity


def proposition_id(statement: object) -> str:
    """Return the polarity-neutral semantic join retained for historical compatibility."""
    core, _ = normalized_proposition(statement)
    if not core:
        raise ValueError("a proposition needs non-empty statement text")
    return "prop:" + hashlib.sha256(core.encode("utf-8")).hexdigest()


def polarized_proposition_id(statement: object) -> str:
    """Return the assertion identity; opposite polarities never share this identifier."""
    core, polarity = normalized_proposition(statement)
    if not core:
        raise ValueError("a proposition needs non-empty statement text")
    return content_fingerprint({"proposition_core": core, "polarity": polarity})


def normalize_actor_id(value: object, *, typed_only: bool = False) -> str | None:
    """Normalize one actor reference to its stable entity id, rejecting forged typed refs."""
    if isinstance(value, Mapping):
        row = dict(value)
        if set(row) != {"schema", "kind", "id", "fingerprint"} \
                or row.get("schema") != _ACTOR_REF_SCHEMA \
                or row.get("kind") not in _ACTOR_REF_KINDS:
            return None
        fingerprint = row.pop("fingerprint", None)
        if fingerprint != content_fingerprint(row):
            return None
        value = row.get("id")
    elif typed_only:
        return None
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or _ACTOR_ID.fullmatch(text) is None:
        return None
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        if prefix.casefold() in _ACTOR_PREFIXES:
            text = suffix
    return text.casefold() if text and _ACTOR_ID.fullmatch(text) else None


def normalize_actor_scope(values: object) -> frozenset[str]:
    if isinstance(values, (str, Mapping)):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        actor_id for value in values if (actor_id := normalize_actor_id(value)) is not None
    )


def visibility_allows(
    visibility: object,
    *,
    audience: str,
    actor_id: str | None = None,
    scoped_actors: object = None,
) -> bool:
    visibility = str(visibility or "public")
    if visibility not in _VISIBILITIES:
        return False
    if audience in {"engine", "narrator_internal", "creator"}:
        return True
    if visibility == "public":
        return True
    if visibility == "hidden":
        return False
    if visibility == "player":
        return audience in {"player", "narrator"}
    normalized_actor = normalize_actor_id(actor_id)
    if normalized_actor is None:
        return False
    return normalized_actor in normalize_actor_scope(scoped_actors)


def _tokens(value: object) -> frozenset[str]:
    return frozenset(_TOKEN.findall(str(value or "").casefold()))


def _query_context(state: Mapping[str, Any], query: str) -> frozenset[str]:
    values: list[object] = [query]
    scene = state.get("scene") or {}
    if isinstance(scene, Mapping):
        values.extend((scene.get("location_id"), scene.get("location"), scene.get("phase")))
        values.extend(scene.get("participants") or [])
    for eid, row in (state.get("entities") or {}).items():
        if isinstance(row, Mapping) and row.get("present"):
            values.extend((eid, row.get("name")))
    return frozenset().union(*(_tokens(value) for value in values))


def _score(text: object, turn: object, current_turn: int, context: frozenset[str]) -> tuple[int, int]:
    overlap = len(_tokens(text) & context)
    try:
        recency = max(-1_000_000, int(turn))
    except (TypeError, ValueError):
        recency = -1_000_000
    # Exact relevance dominates recency; stable secondary order is newest first.
    return overlap, recency - max(0, current_turn - recency) // 16


def _frame_from_claim(row: Mapping[str, Any]) -> Mapping[str, Any]:
    frame = row.get("frame")
    return frame if isinstance(frame, Mapping) else row


def _claim_visibility(row: Mapping[str, Any], frame: Mapping[str, Any]) -> tuple[str, list[str]]:
    visibility = str(row.get("visibility") or frame.get("visibility") or "public")
    actors: list[str] = []
    for value in (
        row.get("audience"), row.get("scoped_actors"), frame.get("audience"),
        frame.get("speaker"), frame.get("addressee"),
    ):
        if isinstance(value, str) and value:
            actors.append(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            actors.extend(str(item) for item in value if item)
    return visibility, list(dict.fromkeys(actors))


def _record_scope_allows(state: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Fail closed for typed records carrying Store-owned occurrence scope.

    Older replay rows did not carry every scope field, so absent fields remain
    compatible.  A present field must agree with the current view.  An explicit
    ancestor branch is allowed because forked checkpoints preserve the original
    immutable record rather than rewriting its occurrence identity.
    """
    scope = state.get("knowledge_record_scope")
    if not isinstance(scope, Mapping):
        return True

    current_world = str((state.get("world_identity") or {}).get("world_id") or "")
    record_world = str(row.get("world_id") or "")
    if current_world and record_world and record_world != current_world:
        return False

    current_branch = str(scope.get("branch_id") or "")
    source_branches = {
        str(value) for value in scope.get("source_branch_ids") or [] if value
    }
    allowed_branches = source_branches | ({current_branch} if current_branch else set())
    record_branch = str(row.get("branch_id") or "")
    if record_branch and allowed_branches and record_branch not in allowed_branches:
        return False

    current_session = str(scope.get("session_id") or "")
    record_session = str(row.get("session_id") or "")
    if current_session and record_session and record_session != current_session:
        # A cross-session fork is still an explicit immutable lineage edge.  No
        # unrelated session can opt in merely by naming the current branch.
        if not record_branch or record_branch not in source_branches:
            return False
    return True


def _projected_proposition_refs(
    statement: str, stored_id: object, stored_identity: object = None,
) -> tuple[str, str, str]:
    """Return canonical assertion/join ids while retaining an immutable record's original id."""
    record_id = str(stored_id or "")
    if not statement:
        identity = str(stored_identity or record_id)
        return record_id, identity, record_id
    return polarized_proposition_id(statement), proposition_id(statement), record_id


def _event_cause_scope(event: Mapping[str, Any]) -> list[str]:
    """Derive legacy-v2 actor scope only from its fingerprinted typed actor reference."""
    if event.get("schema") != "aetherstate-world-event-record/2" \
            or event.get("schema_version") != "world-event-record-v2":
        return []
    actor_id = normalize_actor_id(event.get("actor"), typed_only=True)
    return [actor_id] if actor_id is not None else []


def _project_event_cause(
    state: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    audience: str,
    visible: bool,
) -> tuple[str | None, bool]:
    """Return a display-only cause without rewriting immutable event identity.

    Player-safe front causes need both the event's frozen visibility and the
    matching revealed front.  If either side is missing, the projection fails
    closed so a stale or forged ``front:<id>:completion`` string cannot leak a
    hidden agenda.  Owner/internal retrieval keeps its existing exact id.
    """
    if not visible:
        return None, False
    cause_id = str(event.get("cause_id") or "")
    if audience not in _PLAYER_SAFE_AUDIENCES:
        return cause_id or None, bool(cause_id)
    match = _FRONT_COMPLETION_CAUSE.fullmatch(cause_id)
    if match is None:
        return cause_id or None, bool(cause_id)
    front_id = match.group(1)
    try:
        from .world_events import front_identity_visible_to_player

        front_visible = front_identity_visible_to_player(state, front_id)
    except Exception:
        front_visible = False
    fronts = state.get("fronts")
    front = fronts.get(front_id) if isinstance(fronts, Mapping) else None
    label = str(front.get("name") or "").strip() if isinstance(front, Mapping) else ""
    if not front_visible or not label:
        return None, False
    return label, True


def select_knowledge(
    state: Mapping[str, Any],
    *,
    audience: str = "player",
    actor_id: str | None = None,
    query: str = "",
    limit: int = 12,
    include_history: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Return a bounded, joined knowledge projection for one audience.

    The selector never returns raw conversation prose.  Every row comes from a
    typed state record, and every private row passes the audience/actor gate
    before relevance ranking.
    """
    cap = max(1, min(int(limit), 64))
    current_turn = int((state.get("meta") or {}).get("turn", -1))
    context = _query_context(state, query)
    propositions = state.get("propositions") or {}
    candidates: list[tuple[tuple[int, int], str, dict[str, Any]]] = []

    for raw in state.get("claims") or []:
        if not isinstance(raw, Mapping):
            continue
        if not _record_scope_allows(state, raw):
            continue
        frame = _frame_from_claim(raw)
        visibility, scoped = _claim_visibility(raw, frame)
        if not visibility_allows(
            visibility, audience=audience, actor_id=actor_id, scoped_actors=scoped
        ):
            continue
        claim_pid = str(frame.get("proposition_id") or raw.get("proposition_id") or "")
        identity = str(
            frame.get("proposition_identity") or raw.get("proposition_identity") or claim_pid
        )
        statement = str(
            frame.get("proposition") or raw.get("proposition")
            or ((propositions.get(identity) or {}).get("statement") if identity else "") or ""
        )
        projected_pid, projected_identity, record_pid = _projected_proposition_refs(
            statement, claim_pid, identity
        )
        turn = raw.get("turn", raw.get("turn_index", frame.get("turn", -1)))
        row = {
            "kind": "claim",
            "id": raw.get("claim_id") or raw.get("record_id") or raw.get("fingerprint")
            or frame.get("fingerprint"),
            "proposition_id": projected_pid,
            "proposition_identity": projected_identity,
            "record_proposition_id": record_pid,
            "claim_proposition_id": claim_pid,
            "statement": statement,
            "speaker": frame.get("speaker"),
            "addressee": frame.get("addressee"),
            "claim_class": frame.get("claim_class"),
            "proposition_polarity": frame.get("proposition_polarity"),
            "speech_act_polarity": frame.get("speech_act_polarity"),
            "modality": frame.get("modality"),
            "stance": frame.get("speaker_stance"),
            "turn": turn,
            "status": "said_or_held",
        }
        candidates.append((_score(statement, turn, current_turn, context), "claim", row))

    belief_rows: list[Mapping[str, Any]] = [
        row for row in (state.get("beliefs") or {}).values() if isinstance(row, Mapping)
    ]
    if include_history:
        belief_rows.extend(
            row for row in state.get("epistemic_history") or [] if isinstance(row, Mapping)
        )
    seen_beliefs: set[str] = set()
    for raw in belief_rows:
        if not _record_scope_allows(state, raw):
            continue
        rid = str(raw.get("belief_id") or raw.get("fingerprint") or "")
        if rid and rid in seen_beliefs:
            continue
        seen_beliefs.add(rid)
        holder = str(raw.get("holder") or raw.get("learner") or "")
        visibility = raw.get("visibility") or "actor_scoped"
        scoped = raw.get("scoped_actors") or [holder]
        if not visibility_allows(
            visibility, audience=audience, actor_id=actor_id, scoped_actors=scoped
        ):
            continue
        pid = str(raw.get("proposition_id") or raw.get("fact") or "")
        statement = str(
            raw.get("statement")
            or ((propositions.get(pid) or {}).get("statement") if pid else "") or ""
        )
        turn = raw.get("acquired_turn", raw.get("turn", -1))
        projected_pid, projected_identity, record_pid = _projected_proposition_refs(
            statement, pid, raw.get("proposition_identity")
        )
        row = {
            "kind": "epistemic",
            "id": rid or f"{holder}|{pid}",
            "holder": holder,
            "proposition_id": projected_pid,
            "proposition_identity": projected_identity,
            "record_proposition_id": record_pid,
            "statement": statement,
            "proposition_polarity": raw.get("proposition_polarity"),
            "stance": raw.get("stance"),
            "source": raw.get("source"),
            "claim_id": raw.get("claim_id"),
            "turn": turn,
            "status": raw.get("status", "current"),
        }
        candidates.append((_score(statement, turn, current_turn, context), "epistemic", row))

    for fact_id, raw in (state.get("facts") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        if not _record_scope_allows(state, raw):
            continue
        visibility = raw.get("visibility") or ("hidden" if raw.get("is_secret") else "public")
        if not visibility_allows(
            visibility,
            audience=audience,
            actor_id=actor_id,
            scoped_actors=raw.get("scoped_actors"),
        ):
            continue
        pid = str(raw.get("proposition_id") or "")
        statement = str(raw.get("statement") or ((propositions.get(pid) or {}).get("statement") if pid else "") or "")
        turn = raw.get("established_turn", raw.get("turn", -1))
        status = (
            str(raw.get("status") or "retired")
            if raw.get("retired_turn") is not None
            else str(raw.get("status") or "accepted")
        )
        if not include_history and status not in {"accepted", "current"}:
            continue
        projected_pid, projected_identity, record_pid = _projected_proposition_refs(
            statement, pid, raw.get("proposition_identity")
        )
        row = {
            "kind": "fact",
            "id": fact_id,
            "proposition_id": projected_pid,
            "proposition_identity": projected_identity,
            "record_proposition_id": record_pid,
            "statement": statement,
            "proposition_polarity": raw.get("proposition_polarity"),
            "authority": raw.get("authority", "legacy_replay"),
            "cause": raw.get("cause"),
            "turn": turn,
            "status": status,
        }
        candidates.append((_score(statement, turn, current_turn, context), "fact", row))

    try:
        from .world_events import project_state_overlay

        overlay = project_state_overlay(state)
    except Exception:
        overlay = state.get("world_overlay") or {}
    event_status = {
        str(row.get("event_id")): str(row.get("status"))
        for row in overlay.get("history") or [] if isinstance(row, Mapping)
    }
    for raw in state.get("world_events") or []:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("event_id") or "") not in event_status:
            continue
        cause_visibility = str(raw.get("cause_visibility") or "hidden")
        event_visibility = raw.get("visibility") or (
            "public" if cause_visibility in {"public", "player"} else "player"
        )
        if not visibility_allows(
            event_visibility,
            audience=audience,
            actor_id=actor_id,
            scoped_actors=raw.get("cause_audience"),
        ):
            continue
        visible_cause = visibility_allows(
            cause_visibility,
            audience=audience,
            actor_id=actor_id,
            scoped_actors=_event_cause_scope(raw)
            if cause_visibility == "actor_scoped" else raw.get("cause_audience"),
        )
        cause, visible_cause = _project_event_cause(
            state,
            raw,
            audience=audience,
            visible=visible_cause,
        )
        description = str(raw.get("description") or "")
        turn = raw.get("turn", -1)
        row = {
            "kind": "world_event",
            "id": raw.get("event_id"),
            "statement": description,
            "cause": cause,
            "cause_visible": visible_cause,
            "affected_domains": list(raw.get("affected_domains") or []),
            "turn": turn,
            "status": event_status.get(str(raw.get("event_id")), str(raw.get("kind"))),
            "relation_target": raw.get("relation_target"),
        }
        if include_history or row["status"] in {"active", "scheduled", "admission"}:
            candidates.append((_score(description, turn, current_turn, context), "world_event", row))

    # Prefer direct proposition/query matches, then current facts/events, then recency.
    kind_priority = {"fact": 4, "world_event": 3, "epistemic": 2, "claim": 1}
    candidates.sort(
        key=lambda item: (item[0][0], kind_priority[item[1]], item[0][1], str(item[2].get("id"))),
        reverse=True,
    )
    selected = candidates[:cap]
    out = {"claims": [], "epistemics": [], "facts": [], "events": []}
    bucket = {"claim": "claims", "epistemic": "epistemics", "fact": "facts", "world_event": "events"}
    for _, kind, row in selected:
        out[bucket[kind]].append(deepcopy(row))
    return out


def render_knowledge_context(
    state: Mapping[str, Any],
    *,
    audience: str = "narrator_internal",
    actor_id: str | None = None,
    query: str = "",
    limit: int = 10,
    char_cap: int = 2400,
) -> str:
    """Render a compact typed briefing; never include untyped conversation prose."""
    view = select_knowledge(
        state, audience=audience, actor_id=actor_id, query=query, limit=limit
    )
    lines: list[str] = []
    for row in view["facts"]:
        lines.append(f"FACT[{row['status']}] {row['statement']} (authority={row['authority']})")
    for row in view["epistemics"]:
        lines.append(
            f"EPISTEMIC {row['holder']} {row['stance']}: {row['statement']}"
            f" (source={row['source']})"
        )
    for row in view["claims"]:
        lines.append(
            f"CLAIM {row.get('speaker') or '?'} {row.get('claim_class') or 'said'}:"
            f" {row['statement']} (polarity={row.get('proposition_polarity') or 'unknown'},"
            f" modality={row.get('modality') or 'unspecified'})"
        )
    for row in view["events"]:
        cause = f" cause={row['cause']}" if row.get("cause_visible") else " cause=not-visible"
        lines.append(
            f"WORLD EVENT[{row['status']}] {row['statement']}"
            f" domains={','.join(row['affected_domains'])}{cause}"
        )
    if not lines:
        return ""
    prefix = "[KNOWLEDGE — typed, audience-filtered]"
    total_cap = max(256, int(char_cap))
    available = max(0, total_cap - len(prefix) - 1)
    body = "\n".join(lines)
    if len(body) > available:
        clipped = body[:available]
        body = (clipped.rsplit("\n", 1)[0] if "\n" in clipped else clipped).rstrip()
    return (prefix + ("\n" + body if body else ""))[:total_cap]
