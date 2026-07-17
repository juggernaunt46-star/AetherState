"""Deterministic semantic evidence frames for the fast intent floor.

This is deliberately a seam, not a second rules engine. Candidate generation remains in the
domain detector; this module keeps the source spans, groups candidates that explain the same
piece of prose, and either picks one grounded capability or records an honest ambiguity.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .capability_glossary import content_fingerprint
from .playerlex_recognition import merge_playerlex_proposal
from .semantic_fabric import CompiledMeaning, SemanticFabric
from .worldlex import ContextFrame


_SOURCE_PRIORITY = {"inferred": 1, "construction": 2, "named": 3, "explicit": 4}
_INDEPENDENT_EVENT_RE = re.compile(
    r"(?:,\s*)?\b(?:but|yet|whereas)\b|"
    r"(?:,\s*)?\bwhile\s+(?=(?:i|we|he|she|they|it)\b|(?-i:[A-Z])[a-z0-9'-]*\s+)|"
    r"(?:,\s*)?\band\s+(?=(?:i|we)\b)",
    re.IGNORECASE,
)
LEGACY_ACTION_FRAME_SCHEMA = "semantic-action-frame/1"
V2_ACTION_FRAME_SCHEMA = "semantic-action-frame/2"
ACTION_FRAME_SCHEMA = "semantic-action-frame/3"
DECLARED_MODIFIER_LIMIT = 20
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STABLE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")

_ACTION_FRAME_V1_FIELDS = {
    "schema",
    "frame_id",
    "context_frame",
    "actor_id",
    "capability_id",
    "action_class",
    "target_entity_id",
    "target_name",
    "possessed_object",
    "possessed_object_owner_id",
    "possessed_object_part",
    "target_locus",
    "target_locus_owner_id",
    "polarity",
    "modality",
    "time_scope",
    "evidence",
    "ambiguity",
    "fingerprint",
}
_ACTION_FRAME_V2_FIELDS = _ACTION_FRAME_V1_FIELDS | {
    "meaning_ref",
    "fabric_fingerprint",
    "linguistic_possessor_id",
    "possessed_object_instance_id",
}
_ACTION_FRAME_FIELDS = _ACTION_FRAME_V2_FIELDS | {
    "meaning_binding_ref",
    "event_node_id",
    "world_alignment_refs",
    "invoked_capability_ids",
    "declared_modifier",
}


@dataclass(frozen=True)
class CapabilityCandidate:
    """One owned capability supported by a specific span of the player's prose."""

    capability_id: str
    kind: str
    source: str
    phrase: str
    start: int
    end: int
    clause_index: int
    rank: int = 0
    fallback: bool = False
    use: tuple[str, ...] = ()
    action: str = ""
    target: str | None = None
    instrument: str | None = None

    @property
    def priority(self) -> tuple[int, int, int, int]:
        """Semantic precedence only; capability IDs never break a genuine meaning tie."""
        return (
            _SOURCE_PRIORITY.get(self.source, 0),
            0 if self.fallback else 1,
            max(0, self.end - self.start),
            self.rank,
        )


@dataclass
class ActionFrame:
    """One canonical interpretation consumed by every mechanic for this action.

    Capability candidates remain recognition evidence.  The fields below freeze the contextual
    decision made at the Tier-0 boundary; downstream mechanics receive only this frame (or its
    fingerprint), never another opportunity to reinterpret the Player's prose.
    """

    frame_id: str
    clause_index: int
    start: int
    end: int
    candidates: list[CapabilityCandidate] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    ambiguity: list[str] = field(default_factory=list)
    actor_id: str | None = None
    capability_id: str | None = None
    invoked_capability_ids: tuple[str, ...] = ()
    declared_modifier: int = 0
    action_class: str = ""
    target_entity_id: str | None = None
    target_name: str | None = None
    possessed_object: str = ""
    # `linguistic_possessor_id` aligns the referent in a possessive construction (for example,
    # Iven in "Iven's polehammer") to a runtime entity.  It is not item ownership.  The two
    # fields below remain null unless an independent ledger lookup finds an exact item instance.
    linguistic_possessor_id: str | None = None
    possessed_object_instance_id: str | None = None
    possessed_object_owner_id: str | None = None
    possessed_object_part: str = ""
    target_locus: str = ""
    target_locus_owner_id: str | None = None
    polarity: str = "unknown"
    modality: str = "unknown"
    time_scope: str = "unknown"
    genre_ids: tuple[str, ...] = ()
    meaning_ref: str = ""
    fabric_fingerprint: str = ""
    meaning_binding_ref: str = ""
    event_node_id: str = ""
    world_alignment_refs: tuple[str, ...] = ()
    # Derived from the exact meaning binding at interpretation time.  This is intentionally
    # absent from the journal snapshot: committed state re-derives and cross-checks it from the
    # referenced binding rather than trusting a caller-authored authority flag.
    mechanic_disposition: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # Transient construction evidence.  The existing V3 journal schema remains unchanged; the
    # frame snapshot below carries only fields proven from this one node.  Tier-0 must never use a
    # neighboring occurrence to fill a missing role.
    occurrence: dict[str, Any] | None = None

    def overlaps(self, cand: CapabilityCandidate) -> bool:
        return self.clause_index == cand.clause_index \
            and cand.start < self.end and cand.end > self.start

    def shares_capability(self, cand: CapabilityCandidate, source_text: str = "") -> bool:
        """Treat one named technique and its same-clause verbs as one action, not many rolls."""
        if self.clause_index != cand.clause_index or not any(
                prior.capability_id == cand.capability_id for prior in self.candidates):
            return False
        if source_text:
            left = min(self.end, cand.end)
            right = max(self.start, cand.start)
            if right > left and _INDEPENDENT_EVENT_RE.search(source_text[left:right]):
                return False
        return True

    @property
    def executable(self) -> bool:
        """Compatibility alias for semantic actionability, never settlement authority."""
        return self.mechanically_actionable

    @property
    def mechanically_actionable(self) -> bool:
        """Whether alignment is complete enough to ask a mechanic for an independent receipt."""
        return bool(
            self.actor_id
            and self.capability_id
            and self.action_class
            and self.meaning_ref
            and self.fabric_fingerprint
            and self.meaning_binding_ref
            and self.event_node_id
            and self.mechanic_disposition == "candidate"
            and self.polarity == "positive"
            and self.modality in ("actual", "command")
            and self.time_scope == "current"
            and not self.ambiguity
        )

    def add_evidence(self, kind: str, start: int, end: int, value: Any = None) -> None:
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            return
        row: dict[str, Any] = {"kind": str(kind), "start": start, "end": end}
        if value is not None:
            row["value"] = value
        if row not in self.evidence:
            self.evidence.append(row)

    def snapshot(self, source_text: str) -> dict[str, Any]:
        """Return the privacy-safe, versioned semantic object journaled for replay.

        The source itself is never copied into state.  Exact evidence spans plus the canonical
        source fingerprint are sufficient to prove what the live interpreter decided.
        """
        source_text = str(source_text or "")
        if not source_text:
            raise ValueError("semantic action source must not be empty")
        source_fingerprint = content_fingerprint(source_text)
        frame_id = self.frame_id or "f1"
        span_start = max(0, int(self.start))
        span_end = min(len(source_text), int(self.end))
        if span_end <= span_start:
            span_start, span_end = 0, len(source_text)
        context = ContextFrame(
            frame_id=frame_id,
            source_id=f"user:{source_fingerprint[7:31]}",
            source_fingerprint=source_fingerprint,
            span_start=span_start,
            span_end=span_end,
            polarity=self.polarity,
            modality=self.modality,
            time_scope=self.time_scope,
            quoted=False,
            genre_ids=self.genre_ids,
        ).as_dict()
        evidence = sorted(
            (dict(row) for row in self.evidence),
            key=lambda row: (
                int(row.get("start", 0)),
                int(row.get("end", 0)),
                str(row.get("kind", "")),
                str(row.get("value", "")),
            ),
        )
        if self.meaning_binding_ref:
            schema = ACTION_FRAME_SCHEMA
        elif self.meaning_ref:
            schema = V2_ACTION_FRAME_SCHEMA
        else:
            schema = LEGACY_ACTION_FRAME_SCHEMA
        payload: dict[str, Any] = {
            "schema": schema,
            "frame_id": frame_id,
            "context_frame": context,
            "actor_id": self.actor_id,
            "capability_id": self.capability_id,
            "action_class": self.action_class,
            "target_entity_id": self.target_entity_id,
            "target_name": self.target_name,
            "possessed_object": self.possessed_object,
            "possessed_object_owner_id": self.possessed_object_owner_id,
            "possessed_object_part": self.possessed_object_part,
            "target_locus": self.target_locus,
            "target_locus_owner_id": self.target_locus_owner_id,
            "polarity": self.polarity,
            "modality": self.modality,
            "time_scope": self.time_scope,
            "evidence": evidence,
            "ambiguity": sorted(set(self.ambiguity)),
        }
        if self.meaning_ref:
            payload.update({
                "meaning_ref": self.meaning_ref,
                "fabric_fingerprint": self.fabric_fingerprint,
                "linguistic_possessor_id": self.linguistic_possessor_id,
                "possessed_object_instance_id": self.possessed_object_instance_id,
            })
        if self.meaning_binding_ref:
            payload.update({
                "meaning_binding_ref": self.meaning_binding_ref,
                "event_node_id": self.event_node_id,
                "world_alignment_refs": sorted(set(self.world_alignment_refs)),
                "invoked_capability_ids": sorted(set(self.invoked_capability_ids)),
                "declared_modifier": self.declared_modifier,
            })
        return {**payload, "fingerprint": content_fingerprint(payload)}


def validate_action_frame_snapshot(value: object) -> dict[str, Any]:
    """Strict V1/V2/V3 validator kept stable for journal replay and receipt admission."""
    if not isinstance(value, dict):
        raise ValueError("semantic action frame must be an object")
    schema = value.get("schema")
    expected_fields = {
        LEGACY_ACTION_FRAME_SCHEMA: _ACTION_FRAME_V1_FIELDS,
        V2_ACTION_FRAME_SCHEMA: _ACTION_FRAME_V2_FIELDS,
        ACTION_FRAME_SCHEMA: _ACTION_FRAME_FIELDS,
    }.get(schema)
    if expected_fields is None:
        raise ValueError("unsupported semantic action frame schema")
    if set(value) != expected_fields:
        raise ValueError("semantic action frame fields do not match its schema")
    for key in ("frame_id", "actor_id", "action_class"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"semantic action frame {key} must not be empty")
    capability_id = value.get("capability_id")
    if capability_id is not None \
            and (not isinstance(capability_id, str) or not capability_id.strip()):
        raise ValueError("semantic action frame capability_id must be a string or null")
    for key in (
        "target_entity_id",
        "target_name",
        "possessed_object_owner_id",
        "target_locus_owner_id",
    ):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise ValueError(f"semantic action frame {key} must be a string or null")
    if schema in (V2_ACTION_FRAME_SCHEMA, ACTION_FRAME_SCHEMA):
        for key in ("linguistic_possessor_id", "possessed_object_instance_id"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise ValueError(f"semantic action frame {key} must be a string or null")
        for key in ("meaning_ref", "fabric_fingerprint"):
            raw = value.get(key)
            if not isinstance(raw, str) or _FINGERPRINT_RE.fullmatch(raw) is None:
                raise ValueError(f"semantic action frame {key} must be a content fingerprint")
        item_id = value.get("possessed_object_instance_id")
        owner_id = value.get("possessed_object_owner_id")
        if owner_id is not None and item_id is None:
            raise ValueError("semantic action frame cannot claim object ownership without an item")
    if schema == ACTION_FRAME_SCHEMA:
        if not isinstance(value.get("event_node_id"), str) \
                or _STABLE_ID_RE.fullmatch(value["event_node_id"]) is None:
            raise ValueError("semantic action frame event_node_id must be a stable identifier")
        if not isinstance(value.get("meaning_binding_ref"), str) \
                or _FINGERPRINT_RE.fullmatch(value["meaning_binding_ref"]) is None:
            raise ValueError("semantic action frame meaning_binding_ref must be a fingerprint")
        alignment_refs = value.get("world_alignment_refs")
        if not isinstance(alignment_refs, list) \
                or alignment_refs != sorted(set(alignment_refs)) \
                or any(not isinstance(ref, str) or _FINGERPRINT_RE.fullmatch(ref) is None
                       for ref in alignment_refs):
            raise ValueError("semantic action frame world_alignment_refs must be canonical")
        invoked = value.get("invoked_capability_ids")
        if not isinstance(invoked, list) \
                or invoked != sorted(set(invoked)) \
                or any(not isinstance(item, str) or _STABLE_ID_RE.fullmatch(item) is None
                       for item in invoked) \
                or capability_id in invoked:
            raise ValueError("semantic action frame invoked capabilities must be canonical")
        declared_modifier = value.get("declared_modifier")
        if isinstance(declared_modifier, bool) or not isinstance(declared_modifier, int) \
                or not -DECLARED_MODIFIER_LIMIT <= declared_modifier <= DECLARED_MODIFIER_LIMIT:
            raise ValueError("semantic action frame declared modifier is outside its bound")
        modifier_evidence = [
            row for row in value.get("evidence") or []
            if isinstance(row, dict) and row.get("kind") == "declared_modifier"
        ]
        if declared_modifier and value.get("modality") != "command":
            raise ValueError("only a command frame may carry a declared modifier")
        if modifier_evidence:
            if any(isinstance(row.get("value"), bool)
                   or not isinstance(row.get("value"), int)
                   for row in modifier_evidence) \
                    or sum(int(row["value"]) for row in modifier_evidence) != declared_modifier:
                raise ValueError("semantic action frame modifier evidence does not match its value")
        elif declared_modifier:
            raise ValueError("semantic action frame declared modifier lacks source evidence")
    for key in ("possessed_object", "possessed_object_part", "target_locus"):
        if not isinstance(value.get(key), str):
            raise ValueError(f"semantic action frame {key} must be a string")
    context = ContextFrame.from_dict(value.get("context_frame"))
    if context.frame_id != value["frame_id"]:
        raise ValueError("semantic and WorldLex frame IDs disagree")
    for key in ("polarity", "modality", "time_scope"):
        if getattr(context, key) != value.get(key):
            raise ValueError(f"semantic and WorldLex {key} disagree")
    ambiguity = value.get("ambiguity")
    if not isinstance(ambiguity, list) or any(
            not isinstance(item, str) or not item for item in ambiguity):
        raise ValueError("semantic ambiguity must contain stable candidate IDs")
    if capability_id is None and not ambiguity:
        raise ValueError("semantic action frame needs a capability or ambiguity candidates")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("semantic action frame needs bounded source evidence")
    for row in evidence:
        if not isinstance(row, dict) or any(
                forbidden in row for forbidden in ("text", "source_text", "phrase", "prose")):
            raise ValueError("semantic evidence must not retain source prose")
        start, end = row.get("start"), row.get("end")
        if isinstance(start, bool) or isinstance(end, bool) \
                or not isinstance(start, int) or not isinstance(end, int) \
                or start < context.span_start or end > context.span_end or end <= start:
            raise ValueError("semantic evidence span is outside its context frame")
    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value.get("fingerprint") != content_fingerprint(payload):
        raise ValueError("semantic action frame fingerprint mismatch")
    return dict(value)


@dataclass
class SemanticTurn:
    """Inspectable source evidence and deterministic resolution for one player turn."""

    source_text: str
    frames: list[ActionFrame] = field(default_factory=list)
    compiled_meaning: CompiledMeaning | None = None
    occurrence_graph: dict[str, Any] | None = None
    # Content-free, transient Player Lesson choices selected after recognition.  They are never
    # copied into meaning receipts or frame snapshots; Tier-0 may use them only to choose among
    # options the current turn already contains.
    interpretation_preferences: list[dict[str, Any]] = field(default_factory=list)

    def compile(
        self,
        fabric: SemanticFabric,
        *,
        genre_ids: tuple[str, ...] = (),
        recognition_overlay: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> CompiledMeaning:
        """Compile shared recognition and at most one explicit local overlay for this source."""
        if self.compiled_meaning is None:
            self.compiled_meaning = fabric.translate(self.source_text, genre_ids=genre_ids)
            if recognition_overlay is not None:
                proposal = recognition_overlay(self.source_text)
                self.compiled_meaning = merge_playerlex_proposal(
                    self.compiled_meaning,
                    proposal,
                    fabric=fabric,
                    source_text=self.source_text,
                )
        elif self.compiled_meaning.source_fingerprint != content_fingerprint(self.source_text):
            raise ValueError("semantic turn meaning belongs to a different source")
        elif self.compiled_meaning.fabric_fingerprint != fabric.fingerprint:
            raise ValueError("semantic turn meaning belongs to a different fabric")
        elif self.compiled_meaning.genre_ids != tuple(dict.fromkeys(genre_ids)):
            raise ValueError("semantic turn meaning belongs to a different genre context")
        return self.compiled_meaning

    def add_candidate(self, cand: CapabilityCandidate) -> None:
        overlapping = [frame for frame in self.frames
                       if frame.overlaps(cand)
                       or frame.shares_capability(cand, self.source_text)]
        if not overlapping:
            self.frames.append(ActionFrame(
                frame_id="",
                clause_index=cand.clause_index,
                start=cand.start,
                end=cand.end,
                candidates=[cand],
            ))
            return

        frame = overlapping[0]
        frame.start = min(frame.start, cand.start)
        frame.end = max(frame.end, cand.end)
        frame.candidates.append(cand)
        for other in overlapping[1:]:
            frame.start = min(frame.start, other.start)
            frame.end = max(frame.end, other.end)
            frame.candidates.extend(other.candidates)
            self.frames.remove(other)

    def resolve(self) -> list[CapabilityCandidate]:
        """Select unique best meanings and abstain when distinct capabilities remain tied."""
        self.frames.sort(key=lambda f: (f.start, f.end, f.clause_index))
        resolved: list[CapabilityCandidate] = []
        for number, frame in enumerate(self.frames, 1):
            frame.frame_id = f"f{number}"
            frame.selected = []
            frame.ambiguity = []
            frame.capability_id = None
            frame.invoked_capability_ids = ()
            by_capability: dict[str, CapabilityCandidate] = {}
            for cand in frame.candidates:
                prior = by_capability.get(cand.capability_id)
                if prior is None:
                    by_capability[cand.capability_id] = cand
                    continue
                uses = tuple(dict.fromkeys((*prior.use, *cand.use)))
                best = cand if cand.priority > prior.priority else prior
                by_capability[cand.capability_id] = replace(best, use=uses)
            if not by_capability:
                continue
            best_priority = max(c.priority for c in by_capability.values())
            winners = sorted(
                (c for c in by_capability.values() if c.priority == best_priority),
                key=lambda c: c.capability_id,
            )
            if len(winners) != 1:
                frame.ambiguity = [c.capability_id for c in winners]
                continue
            frame.selected = [winners[0].capability_id]
            frame.capability_id = winners[0].capability_id
            frame.invoked_capability_ids = tuple(sorted(set(winners[0].use)))
            resolved.append(winners[0])
        return resolved
