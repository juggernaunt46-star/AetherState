"""Typed, replay-pure Living Character relationship continuity.

Recognition may propose these records.  Only the journal authority boundary may admit them.
Collective participant references remain descriptive scope and never become actors or consent
holders.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any


AGREEMENT_SCHEMA = "aetherstate-relationship-agreement/1"
OCCURRENCE_SCHEMA = "aetherstate-social-occurrence/1"
OCCURRENCE_SUPERSESSION_SCHEMA = "aetherstate-social-occurrence-supersession/1"
REFERENT_BINDING_SCHEMA = "aetherstate-social-referent-binding/1"
THREAD_TRANSITION_SCHEMA = "aetherstate-continuity-thread-transition/1"
STARTING_CONTINUITY_SCHEMA = "aetherstate-chat-starting-continuity/1"
EVIDENCE_SCHEMA = "aetherstate-recognized-evidence/1"
OCCURRENCE_ADMISSION_SCHEMA = "aetherstate-social-occurrence-admission/1"

CONTINUITY_FINGERPRINT_DOMAIN = b"aetherstate-chat-continuity/1\0"
CONTINUITY_SEED_FINGERPRINT_DOMAIN = b"aetherstate-chat-continuity-seed/1\0"
EVIDENCE_FINGERPRINT_DOMAIN = b"aetherstate-recognized-evidence/1\0"
OCCURRENCE_ADMISSION_FINGERPRINT_DOMAIN = b"aetherstate-social-occurrence-admission/1\0"

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_COLLECTIVE_KINDS = frozenset({"group", "category", "unknown"})
_CONCRETE_KINDS = frozenset({"actor", "person", "anonymous"})
_PORTABLE_ROLES = frozenset({"character", "current_persona"})
_STARTING_FAMILIES = (
    "memories",
    "player_visible_possessions_conditions",
    "character_knowledge",
    "relationship_causes",
    "agreement_revisions",
    "open_threads",
)
OCCURRENCE_ACTIONS = frozenset({"sexual_contact", "romantic_contact"})
SOCIAL_SPEECH_ACTS = frozenset({
    "agreement_create", "agreement_amend", "agreement_withdraw",
    "agreement_release", "agreement_end",
    "promise_make", "promise_fulfill", "promise_violate",
    "promise_withdraw", "promise_release",
    "thread_resolve", "disclosure",
})
_AUTOMATIC_SOURCE_SEGMENTS = frozenset({
    "direct_dialogue", "shared_observation",
    "private_action_or_thought", "offscreen_third_party",
})
_WORLD_RELEVANCE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.IGNORECASE)
_WORLD_RELEVANCE_STOPWORDS = frozenset({
    "after", "again", "also", "and", "are", "before", "but", "can", "could",
    "did", "does", "for", "from", "had", "has", "have", "her", "here", "hers",
    "him", "his", "into", "its", "may", "might", "not", "now", "our", "ours",
    "over", "she", "should", "that", "the", "their", "theirs", "them", "then",
    "there", "these", "they", "this", "those", "through", "under", "upon", "was",
    "were", "what", "when", "where", "which", "who", "will", "with", "would",
    "you", "your", "yours",
})


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
        raise ValueError("continuity value must be finite JSON") from exc


def _fingerprint(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical(value)).hexdigest()


def _record_fingerprint(value: Mapping[str, object]) -> str:
    clean = {
        key: item for key, item in value.items()
        if key not in {"fingerprint", "record_fingerprint"}
    }
    return _fingerprint(CONTINUITY_FINGERPRINT_DOMAIN, clean)


def _verify_record_fingerprint(value: Mapping[str, object]) -> None:
    supplied = value.get("fingerprint")
    if supplied is not None and supplied != _record_fingerprint(value):
        raise ValueError("continuity record fingerprint is forged or stale")


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable bounded identifier")
    return value


def _text(
    value: object,
    *,
    field: str,
    cap: int = 4000,
    required: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    clean = value.strip()
    if required and not clean:
        raise ValueError(f"{field} is required")
    if len(clean) > cap:
        raise ValueError(f"{field} exceeds {cap} characters")
    return clean


def _revision(value: object, *, field: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _turn(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _exact_fingerprint(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be an exact sha256 fingerprint")
    return value


def _evidence(value: object, *, kind: str) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError(f"{kind} requires one code-sealed recognized evidence frame")
    if value.get("kind") != kind:
        raise ValueError(f"{kind} evidence has the wrong recognized frame kind")
    if value.get("accepted") is not True or value.get("code_sealed") is not True:
        raise ValueError(f"{kind} evidence must be accepted and code-sealed")
    message_fingerprint = _exact_fingerprint(
        value.get("message_fingerprint"), field=f"{kind} message_fingerprint",
    )
    start = value.get("start")
    end = value.get("end")
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 \
            or isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ValueError(f"{kind} evidence requires one exact non-empty source span")
    clean = {
        "schema": EVIDENCE_SCHEMA,
        "kind": kind,
        "message_fingerprint": message_fingerprint,
        "start": start,
        "end": end,
        "accepted": True,
        "code_sealed": True,
    }
    expected = _fingerprint(EVIDENCE_FINGERPRINT_DOMAIN, clean)
    if value.get("fingerprint") != expected:
        raise ValueError(f"{kind} evidence fingerprint is forged or stale")
    clean["fingerprint"] = expected
    return clean


def validate_cause_ref(value: object, *, context: str = "cause_ref") -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a typed exact reference")
    kind = _identifier(value.get("kind"), field=f"{context}.kind")
    fingerprint = _exact_fingerprint(
        value.get("fingerprint"), field=f"{context}.fingerprint",
    )
    return {"kind": kind, "fingerprint": fingerprint}


def validate_occurrence_admission_evidence(value: object) -> dict:
    """Validate the exact Task-4 seam without performing lifecycle extraction here."""
    if not isinstance(value, Mapping) or value.get("schema") != OCCURRENCE_ADMISSION_SCHEMA:
        raise ValueError(f"occurrence admission schema must be {OCCURRENCE_ADMISSION_SCHEMA}")
    source = value.get("source")
    if source not in {"accepted_response", "user_text"}:
        raise ValueError("occurrence admission source must be accepted_response|user_text")
    if value.get("accepted") is not True or value.get("code_sealed") is not True:
        raise ValueError("occurrence admission must be accepted and code-sealed")
    start = value.get("start")
    end = value.get("end")
    if isinstance(start, bool) or not isinstance(start, int) or start < 0 \
            or isinstance(end, bool) or not isinstance(end, int) or end <= start:
        raise ValueError("occurrence admission requires one exact non-empty source span")
    clean = {
        "schema": OCCURRENCE_ADMISSION_SCHEMA,
        "source": source,
        "message_fingerprint": _exact_fingerprint(
            value.get("message_fingerprint"), field="occurrence admission message_fingerprint",
        ),
        "start": start,
        "end": end,
        "accepted": True,
        "code_sealed": True,
    }
    fingerprint = _fingerprint(OCCURRENCE_ADMISSION_FINGERPRINT_DOMAIN, clean)
    if value.get("fingerprint") != fingerprint:
        raise ValueError("occurrence admission fingerprint is forged or stale")
    clean["fingerprint"] = fingerprint
    return clean


def validate_participant_ref(value: object, *, context: str) -> dict:
    """Validate one typed participant without promoting descriptive scope into identity."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} participant must be a typed object")
    kind = value.get("kind")
    if kind not in _CONCRETE_KINDS | _COLLECTIVE_KINDS:
        raise ValueError(f"{context} participant kind is unsupported")
    actor_only = context in {
        "agreement party", "scoped actor", "motive holder", "thread participant",
    }
    concrete_only = context in {"consent", "voluntariness"}
    if actor_only and kind != "actor":
        raise ValueError(f"{context} must use an exact actor reference")
    if concrete_only and kind not in _CONCRETE_KINDS:
        raise ValueError(f"{context} cannot be held by a group, category, or unknown participant")

    if kind == "actor":
        return {"kind": "actor", "actor_id": _identifier(
            value.get("actor_id"), field=f"{context}.actor_id",
        )}
    if kind == "person":
        out = {
            "kind": "person",
            "person_id": _identifier(value.get("person_id"), field=f"{context}.person_id"),
        }
        label = _text(value.get("label", ""), field=f"{context}.label", cap=240, required=False)
        if label:
            out["label"] = label
        return out
    if kind == "anonymous":
        out = {
            "kind": "anonymous",
            "occurrence_id": _identifier(
                value.get("occurrence_id"), field=f"{context}.occurrence_id",
            ),
            "anonymous_id": _identifier(
                value.get("anonymous_id"), field=f"{context}.anonymous_id",
            ),
        }
        label = _text(value.get("label", ""), field=f"{context}.label", cap=240, required=False)
        if label:
            out["label"] = label
        return out
    if kind == "group":
        out = {
            "kind": "group",
            "label": _text(value.get("label"), field=f"{context}.label", cap=240),
        }
        if value.get("count") is not None:
            count = value["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100_000:
                raise ValueError(f"{context}.count must be a positive bounded integer")
            out["count"] = count
        return out
    return {
        "kind": str(kind),
        "label": _text(value.get("label"), field=f"{context}.label", cap=240),
    }


def _participant_key(ref: Mapping[str, object]) -> tuple[str, str, str]:
    kind = str(ref["kind"])
    if kind == "actor":
        return kind, str(ref["actor_id"]), ""
    if kind == "person":
        return kind, str(ref["person_id"]), ""
    if kind == "anonymous":
        return kind, str(ref["occurrence_id"]), str(ref["anonymous_id"])
    return kind, str(ref.get("label") or ""), ""


def _participants(
    value: object,
    *,
    context: str,
    minimum: int = 0,
    maximum: int = 64,
) -> list[dict]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{context} participant array must contain {minimum}..{maximum} rows")
    out = [validate_participant_ref(item, context=context) for item in value]
    if len({_participant_key(item) for item in out}) != len(out):
        raise ValueError(f"{context} participant array contains duplicates")
    return out


def _lineage_fields(value: Mapping[str, object], *, action: str) -> tuple[int, str | None]:
    revision = _revision(value.get("revision"))
    predecessor = value.get("supersedes_fingerprint")
    if revision == 1:
        if action not in {"create", "admit"} or predecessor is not None:
            raise ValueError("revision 1 must create/admit without a predecessor")
        return revision, None
    if action in {"create", "admit"}:
        raise ValueError("later revisions cannot use create/admit")
    return revision, _exact_fingerprint(predecessor, field="supersedes_fingerprint")


def _record_provenance(value: Mapping[str, object], out: dict[str, Any]) -> None:
    lifecycle = ""
    if value.get("lifecycle_source") is not None:
        lifecycle = _identifier(
            value.get("lifecycle_source"),
            field="lifecycle_source",
        )
        out["lifecycle_source"] = lifecycle
    if value.get("response_occurrence_id") is not None:
        response_id = str(value.get("response_occurrence_id") or "")
        if response_id and re.fullmatch(r"response:[0-9a-f]{64}", response_id) is None:
            raise ValueError("response_occurrence_id must name one exact accepted response")
        if response_id and not lifecycle:
            raise ValueError("response_occurrence_id requires lifecycle_source")
        if lifecycle in {"assistant_response", "deferred_extraction"} \
                and not response_id:
            raise ValueError("accepted-response lifecycle requires response_occurrence_id")
        if lifecycle == "user_text" and response_id:
            raise ValueError("user_text lifecycle cannot carry response_occurrence_id")
        out["response_occurrence_id"] = response_id
    elif lifecycle in {"assistant_response", "deferred_extraction"}:
        raise ValueError("accepted-response lifecycle requires response_occurrence_id")


def _preserve_starting_continuity_scope(
    value: Mapping[str, object],
    out: dict[str, Any],
    *,
    participants: list[dict[str, Any]],
) -> None:
    """Retain only the server-baked audience of privileged portable starting records."""
    if out.get("lifecycle_source") != "creator_starting_continuity":
        return
    expected = sorted({
        str(ref.get("actor_id") or "")
        for ref in participants
        if isinstance(ref, Mapping)
        and ref.get("kind") == "actor"
        and str(ref.get("actor_id") or "")
    })
    scoped_value = value.get("scoped_actors")
    if value.get("visibility") != "actor_scoped" \
            or not isinstance(scoped_value, list):
        raise ValueError("starting continuity requires a server-baked actor scope")
    scoped = sorted({
        _identifier(actor, field="starting continuity scoped actor")
        for actor in scoped_value
    })
    if scoped != expected:
        raise ValueError("starting continuity scope must match its exact resolved actors")
    out["visibility"] = "actor_scoped"
    out["scoped_actors"] = scoped


def validate_agreement_revision(value: object) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != AGREEMENT_SCHEMA:
        raise ValueError(f"agreement schema must be {AGREEMENT_SCHEMA}")
    action = value.get("action")
    if action not in {"create", "amend", "withdraw", "release", "end"}:
        raise ValueError("agreement action is unsupported")
    revision, predecessor = _lineage_fields(value, action=str(action))
    parties = _participants(
        value.get("parties"), context="agreement party", minimum=2, maximum=8,
    )
    exclusivity = value.get("exclusivity")
    if exclusivity not in {"exclusive", "open", "ended"}:
        raise ValueError("agreement exclusivity must be exclusive|open|ended")
    acts = value.get("allowed_outside_acts", [])
    if not isinstance(acts, list) or len(acts) > 64:
        raise ValueError("allowed_outside_acts must be a bounded list")
    allowed_acts = sorted({
        _identifier(item, field="allowed_outside_acts item") for item in acts
    })
    requires_disclosure = value.get("requires_disclosure")
    if not isinstance(requires_disclosure, bool):
        raise ValueError("requires_disclosure must be boolean")
    deadline = value.get("disclosure_deadline")
    if requires_disclosure and deadline not in {
        "before_act", "after_act_allowed", "unspecified",
    }:
        raise ValueError("requires_disclosure needs an explicit deadline")
    if not requires_disclosure and deadline is not None:
        raise ValueError("a disclosure deadline requires requires_disclosure=true")
    assent_value = value.get("assent", [])
    if not isinstance(assent_value, list) or len(assent_value) > len(parties):
        raise ValueError("agreement assent must be a bounded participant-specific list")
    assent: list[dict] = []
    for row in assent_value:
        if not isinstance(row, Mapping):
            raise ValueError("agreement assent row must be an object")
        party = validate_participant_ref(row.get("party"), context="agreement party")
        if party not in parties:
            raise ValueError("agreement assent party is not an agreement party")
        status = row.get("status")
        if status not in {"accepted", "proposed", "rejected"}:
            raise ValueError("agreement assent status is unsupported")
        evidence = _evidence(row.get("evidence"), kind="assent") \
            if status in {"accepted", "proposed"} else None
        assent.append({"party": party, "status": status, "evidence": evidence})
    if len({_participant_key(row["party"]) for row in assent}) != len(assent):
        raise ValueError("agreement assent contains duplicate parties")

    out: dict[str, Any] = {
        "schema": AGREEMENT_SCHEMA,
        "agreement_id": _identifier(value.get("agreement_id"), field="agreement_id"),
        "revision": revision,
        "action": action,
        "parties": parties,
        "exclusivity": exclusivity,
        "allowed_outside_acts": allowed_acts,
        "requires_disclosure": requires_disclosure,
        "disclosure_deadline": deadline,
        "effective_turn": _turn(value.get("effective_turn"), field="effective_turn"),
        "assent": assent,
    }
    if predecessor is not None:
        out["supersedes_fingerprint"] = predecessor
    if action in {"withdraw", "release", "end"}:
        out["acting_party"] = validate_participant_ref(
            value.get("acting_party"), context="agreement party",
        )
        if out["acting_party"] not in parties:
            raise ValueError("agreement acting_party is not an agreement party")
        out["evidence"] = _evidence(value.get("evidence"), kind="agreement_transition")
    _record_provenance(value, out)
    _preserve_starting_continuity_scope(value, out, participants=parties)
    _verify_record_fingerprint(value)
    if value.get("fingerprint") is not None:
        out["fingerprint"] = str(value["fingerprint"])
    return out


def automatic_agreement_has_exact_assent(record: Mapping[str, object]) -> bool:
    clean = validate_agreement_revision(record)
    accepted = {
        _participant_key(row["party"])
        for row in clean["assent"]
        if row["status"] == "accepted"
    }
    return accepted == {_participant_key(party) for party in clean["parties"]}


def automatic_agreement_is_lifecycle_proposal(
    record: Mapping[str, object],
) -> bool:
    """Return whether one role proposed exact terms without asserting counterpart assent."""
    clean = validate_agreement_revision(record)
    if clean["action"] not in {"create", "amend"} or len(clean["assent"]) != 1:
        return False
    row = clean["assent"][0]
    return (
        row["status"] == "proposed"
        and isinstance(row.get("evidence"), Mapping)
        and row["evidence"].get("accepted") is True
        and row["evidence"].get("code_sealed") is True
    )


def validate_social_occurrence(value: object) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != OCCURRENCE_SCHEMA:
        raise ValueError(f"occurrence schema must be {OCCURRENCE_SCHEMA}")
    action = value.get("action")
    if action not in {"admit", "correct", "retract"}:
        raise ValueError("occurrence action is unsupported")
    revision, predecessor = _lineage_fields(value, action=str(action))
    occurrence_id = _identifier(value.get("occurrence_id"), field="occurrence_id")
    outside = _participants(
        value.get("outside_participants"), context="outside participant", maximum=64,
    )
    if any(ref["kind"] in _COLLECTIVE_KINDS for ref in outside) and len(outside) != 1:
        raise ValueError("one collective outside participant describes the whole open-ended set")
    for ref in outside:
        if ref["kind"] == "anonymous" and ref["occurrence_id"] != occurrence_id:
            raise ValueError("anonymous participant escaped its exact occurrence")
    agreement_actor = validate_participant_ref(
        value.get("agreement_actor"), context="agreement party",
    )

    evidence_spans: list[tuple[str, int, int]] = []
    voluntary_value = value.get("voluntariness")
    if not isinstance(voluntary_value, list) or not 1 <= len(voluntary_value) <= 64:
        raise ValueError("voluntariness must be a bounded participant-specific list")
    voluntariness: list[dict] = []
    for row in voluntary_value:
        if not isinstance(row, Mapping):
            raise ValueError("voluntariness row must be an object")
        participant = validate_participant_ref(row.get("participant"), context="voluntariness")
        if participant["kind"] == "anonymous" and participant["occurrence_id"] != occurrence_id:
            raise ValueError("anonymous voluntariness escaped its exact occurrence")
        status = row.get("status")
        if status not in {"voluntary", "coerced", "assaulted", "unknown"}:
            raise ValueError("voluntariness status is unsupported")
        evidence = None if status == "unknown" else _evidence(
            row.get("evidence"), kind="voluntariness",
        )
        if evidence is not None:
            evidence_spans.append((
                evidence["message_fingerprint"], evidence["start"], evidence["end"],
            ))
        voluntariness.append({
            "participant": participant, "status": status, "evidence": evidence,
        })

    consent_value = value.get("consent", [])
    if not isinstance(consent_value, list) or len(consent_value) > 128:
        raise ValueError("consent must be a bounded participant- and act-specific list")
    consent: list[dict] = []
    for row in consent_value:
        if not isinstance(row, Mapping):
            raise ValueError("consent row must be an object")
        participant = validate_participant_ref(row.get("participant"), context="consent")
        if participant["kind"] == "anonymous" and participant["occurrence_id"] != occurrence_id:
            raise ValueError("anonymous consent escaped its exact occurrence")
        status = row.get("status")
        if status not in {"granted", "refused", "unknown"}:
            raise ValueError("consent status is unsupported")
        channel = row.get("channel")
        if channel not in {"in_fiction", "ooc_content"}:
            raise ValueError("consent channel must distinguish fiction from OOC content consent")
        evidence = None if status == "unknown" else _evidence(row.get("evidence"), kind="consent")
        if evidence is not None:
            evidence_spans.append((
                evidence["message_fingerprint"], evidence["start"], evidence["end"],
            ))
        consent.append({
            "participant": participant,
            "act": _identifier(row.get("act"), field="consent.act"),
            "status": status,
            "channel": channel,
            "evidence": evidence,
        })

    disclosure_value = value.get("disclosures", [])
    if not isinstance(disclosure_value, list) or len(disclosure_value) > 64:
        raise ValueError("disclosures must be a bounded participant-specific list")
    disclosures: list[dict] = []
    for row in disclosure_value:
        if not isinstance(row, Mapping):
            raise ValueError("disclosure row must be an object")
        participant = validate_participant_ref(row.get("participant"), context="agreement party")
        status = row.get("status")
        if status == "withheld":
            raise ValueError("callers cannot propose withheld disclosure from silence")
        if status not in {"timely", "late", "unknown"}:
            raise ValueError("disclosure status is unsupported")
        evidence = None if status == "unknown" else _evidence(
            row.get("evidence"), kind="disclosure",
        )
        if evidence is not None:
            evidence_spans.append((
                evidence["message_fingerprint"], evidence["start"], evidence["end"],
            ))
        disclosures.append({
            "agreement_id": _identifier(
                row.get("agreement_id"), field="disclosure.agreement_id",
            ),
            "participant": participant,
            "status": status,
            "evidence": evidence,
        })
    disclosure_targets = {
        (row["agreement_id"], _participant_key(row["participant"]))
        for row in disclosures
    }
    if len(disclosure_targets) != len(disclosures):
        raise ValueError("disclosures contain a duplicate disclosure target")
    if len(evidence_spans) != len(set(evidence_spans)):
        raise ValueError(
            "voluntariness, consent, and disclosure require distinct exact evidence spans",
        )

    motive = value.get("motive_claim_ref")
    if motive is not None:
        if not isinstance(motive, Mapping):
            raise ValueError("motive must be an exact Claim Record reference")
        motive = {
            "claim_id": _identifier(motive.get("claim_id"), field="motive.claim_id"),
            "fingerprint": _exact_fingerprint(
                motive.get("fingerprint"), field="motive.fingerprint",
            ),
        }
    out: dict[str, Any] = {
        "schema": OCCURRENCE_SCHEMA,
        "occurrence_id": occurrence_id,
        "revision": revision,
        "action": action,
        "occurred_turn": _turn(value.get("occurred_turn"), field="occurred_turn"),
        "act": _identifier(value.get("act"), field="act"),
        "agreement_actor": agreement_actor,
        "outside_participants": outside,
        "voluntariness": voluntariness,
        "consent": consent,
        "disclosures": disclosures,
        "motive_claim_ref": motive,
        "summary": _text(value.get("summary", ""), field="summary", required=False),
    }
    if value.get("admission_evidence") is not None:
        out["admission_evidence"] = validate_occurrence_admission_evidence(
            value.get("admission_evidence"),
        )
    if value.get("source_segment") is not None:
        segment = str(value.get("source_segment") or "")
        if segment not in _AUTOMATIC_SOURCE_SEGMENTS:
            raise ValueError("occurrence source_segment is unsupported")
        out["source_segment"] = segment
    if value.get("visibility") is not None:
        visibility = str(value.get("visibility") or "")
        if visibility not in {"public", "player", "actor_scoped", "hidden"}:
            raise ValueError("occurrence visibility is unsupported")
        scoped = value.get("scoped_actors", [])
        if not isinstance(scoped, list) or len(scoped) > 64:
            raise ValueError("occurrence scoped_actors must be a bounded actor list")
        actors = sorted({
            _identifier(actor, field="occurrence scoped actor") for actor in scoped
        })
        if visibility == "actor_scoped" and not actors:
            raise ValueError("actor-scoped occurrence requires exact scoped actors")
        if visibility != "actor_scoped" and actors:
            raise ValueError("only actor-scoped occurrences may carry scoped actors")
        out["visibility"] = visibility
        out["scoped_actors"] = actors
    if predecessor is not None:
        out["supersedes_fingerprint"] = predecessor
    if value.get("correction_cause") is not None:
        out["correction_cause"] = _evidence(
            value.get("correction_cause"), kind="correction",
        )
    _record_provenance(value, out)
    _verify_record_fingerprint(value)
    if value.get("fingerprint") is not None:
        out["fingerprint"] = str(value["fingerprint"])
    return out


def validate_social_occurrence_supersession(value: object) -> dict:
    if not isinstance(value, Mapping) \
            or value.get("schema") != OCCURRENCE_SUPERSESSION_SCHEMA:
        raise ValueError(
            f"occurrence supersession schema must be {OCCURRENCE_SUPERSESSION_SCHEMA}",
        )
    action = value.get("action")
    if action not in {"correct", "retract"}:
        raise ValueError("occurrence supersession action must be correct|retract")
    revision = _revision(value.get("revision"))
    if revision < 2:
        raise ValueError("occurrence supersession must follow an admitted occurrence")
    occurrence_id = _identifier(value.get("occurrence_id"), field="occurrence_id")
    out: dict[str, Any] = {
        "schema": OCCURRENCE_SUPERSESSION_SCHEMA,
        "occurrence_id": occurrence_id,
        "revision": revision,
        "action": action,
        "supersedes_fingerprint": _exact_fingerprint(
            value.get("supersedes_fingerprint"), field="supersedes_fingerprint",
        ),
        "cause": _evidence(value.get("cause"), kind="correction"),
    }
    if action == "correct":
        replacement = validate_social_occurrence(value.get("replacement"))
        if replacement["occurrence_id"] != occurrence_id:
            raise ValueError("occurrence correction replacement changes occurrence identity")
        out["replacement"] = replacement
    elif value.get("replacement") is not None:
        raise ValueError("occurrence retraction cannot carry a replacement")
    _record_provenance(value, out)
    return out


def validate_social_referent_binding(value: object) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != REFERENT_BINDING_SCHEMA:
        raise ValueError(f"referent binding schema must be {REFERENT_BINDING_SCHEMA}")
    out = {
        "schema": REFERENT_BINDING_SCHEMA,
        "occurrence_id": _identifier(value.get("occurrence_id"), field="occurrence_id"),
        "anonymous_id": _identifier(value.get("anonymous_id"), field="anonymous_id"),
        "actor_id": _identifier(value.get("actor_id"), field="actor_id"),
        "cause_ref": validate_cause_ref(value.get("cause_ref")),
    }
    _record_provenance(value, out)
    _verify_record_fingerprint(value)
    if value.get("fingerprint") is not None:
        out["fingerprint"] = str(value["fingerprint"])
    return out


def validate_thread_transition(value: object) -> dict:
    if not isinstance(value, Mapping) or value.get("schema") != THREAD_TRANSITION_SCHEMA:
        raise ValueError(f"thread transition schema must be {THREAD_TRANSITION_SCHEMA}")
    action = value.get("action")
    if action not in {"create", "update", "resolve", "abandon"}:
        raise ValueError("thread transition action is unsupported")
    revision, predecessor = _lineage_fields(value, action=str(action))
    kind = value.get("kind")
    if kind not in {
        "promise", "plan", "disagreement", "discovered_secret", "unfinished_conversation",
    }:
        raise ValueError("continuity thread kind is unsupported")
    status = value.get("status")
    if status not in {"open", "fulfilled", "violated", "resolved", "abandoned"}:
        raise ValueError("continuity thread status is unsupported")
    out: dict[str, Any] = {
        "schema": THREAD_TRANSITION_SCHEMA,
        "thread_id": _identifier(value.get("thread_id"), field="thread_id"),
        "revision": revision,
        "action": action,
        "kind": kind,
        "summary": _text(value.get("summary"), field="thread summary"),
        "participants": _participants(
            value.get("participants"), context="thread participant", minimum=1, maximum=16,
        ),
        "status": status,
    }
    if kind == "promise":
        has_promisor = value.get("promisor_actor_id") is not None
        has_promisee = value.get("promisee_actor_id") is not None
        if has_promisor != has_promisee:
            raise ValueError("promise direction must name both exact actors")
        if has_promisor:
            promisor = _identifier(
                value.get("promisor_actor_id"),
                field="promise promisor_actor_id",
            )
            promisee = _identifier(
                value.get("promisee_actor_id"),
                field="promise promisee_actor_id",
            )
            participant_ids = _actor_ref_ids(out["participants"])
            if promisor == promisee or participant_ids != {promisor, promisee}:
                raise ValueError(
                    "promise direction must name two distinct exact thread participants",
                )
            out["promisor_actor_id"] = promisor
            out["promisee_actor_id"] = promisee
        if value.get("promise_terms") is not None:
            terms = value.get("promise_terms")
            if not isinstance(terms, Mapping) or set(terms) != {
                "polarity", "predicate",
            }:
                raise ValueError("promise_terms must be one exact typed predicate")
            polarity = str(terms.get("polarity") or "")
            if polarity not in {"perform", "refrain"}:
                raise ValueError("promise_terms.polarity is unsupported")
            predicate = re.sub(
                r"\s+",
                " ",
                _text(
                    terms.get("predicate"),
                    field="promise_terms.predicate",
                    cap=240,
                ),
            ).casefold()
            out["promise_terms"] = {
                "polarity": polarity,
                "predicate": predicate,
            }
    elif value.get("promisor_actor_id") is not None \
            or value.get("promisee_actor_id") is not None \
            or value.get("promise_terms") is not None:
        raise ValueError("only promise threads may carry directional promise actors")
    if predecessor is not None:
        out["supersedes_fingerprint"] = predecessor
    if value.get("cause_ref") is not None:
        out["cause_ref"] = validate_cause_ref(value.get("cause_ref"))
    _record_provenance(value, out)
    _preserve_starting_continuity_scope(
        value,
        out,
        participants=out["participants"],
    )
    _verify_record_fingerprint(value)
    if value.get("fingerprint") is not None:
        out["fingerprint"] = str(value["fingerprint"])
    return out


def _agreement_ref(record: Mapping[str, object]) -> dict:
    return {
        "agreement_id": record["agreement_id"],
        "revision": record["revision"],
        "fingerprint": record["fingerprint"],
    }


def _occurrence_ref(record: Mapping[str, object]) -> dict:
    return {
        "occurrence_id": record["occurrence_id"],
        "revision": record["revision"],
        "fingerprint": record["fingerprint"],
    }


def assess_infidelity(agreements: Iterable[dict], occurrence: dict) -> dict:
    """Assess only the relationship boundary; never apply a delta or moral reaction."""
    admitted_occurrence = validate_social_occurrence(occurrence)
    actor = admitted_occurrence["agreement_actor"]
    occurred_turn = admitted_occurrence["occurred_turn"]
    candidates = []
    for value in agreements:
        agreement = validate_agreement_revision(value)
        if actor not in agreement["parties"] or agreement["effective_turn"] > occurred_turn:
            continue
        candidates.append(agreement)
    active_by_id: dict[str, dict] = {}
    for agreement in sorted(candidates, key=lambda row: (
        row["effective_turn"], row["revision"], row["agreement_id"],
    )):
        if agreement.get("lifecycle_source") in {
            "user_text", "assistant_response",
        } and agreement["action"] in {"create", "amend"} \
                and not automatic_agreement_has_exact_assent(agreement):
            # A one-role proposal is durable negotiation evidence, but it does
            # not replace the last mutually accepted boundary.
            continue
        active_by_id[agreement["agreement_id"]] = agreement
    active = [
        agreement for agreement in active_by_id.values()
        if agreement["action"] not in {"withdraw", "release", "end"}
        and agreement["exclusivity"] != "ended"
        and (
            agreement.get("lifecycle_source") not in {
                "user_text", "assistant_response",
            }
            or automatic_agreement_has_exact_assent(agreement)
        )
    ]
    active.sort(key=lambda row: (
        row["agreement_id"], row["revision"], row["effective_turn"],
    ))
    occurrence_ref = _occurrence_ref(admitted_occurrence)
    if not active or not admitted_occurrence["outside_participants"]:
        return {
            "status": "not_applicable",
            "agreement_ref": None,
            "boundary_ref": None,
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "no_applicable_relationship_boundary"}],
        }
    if len(active) > 1:
        return {
            "status": "unresolved",
            "agreement_ref": None,
            "candidate_agreement_refs": [_agreement_ref(row) for row in active],
            "boundary_ref": {"kind": "agreement_selection", "actor": actor},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "multiple_active_relationship_boundaries"}],
        }
    agreement = active[0]
    agreement_ref = _agreement_ref(agreement)
    voluntariness_rows = [
        row for row in admitted_occurrence["voluntariness"]
        if row["participant"] == actor
    ]
    if len(voluntariness_rows) != 1 or voluntariness_rows[0]["status"] == "unknown":
        return {
            "status": "unresolved",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "voluntariness", "actor": actor},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "agreement_actor_voluntariness_unknown"}],
        }
    voluntariness = voluntariness_rows[0]["status"]
    if voluntariness in {"coerced", "assaulted"}:
        return {
            "status": "not_violated",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "voluntariness", "actor": actor},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "agreement_actor_not_voluntary", "status": voluntariness}],
        }
    act = admitted_occurrence["act"]
    allowed = agreement["exclusivity"] == "open" \
        and act in agreement["allowed_outside_acts"]
    if not allowed:
        return {
            "status": "violated",
            "agreement_ref": agreement_ref,
            "boundary_ref": {
                "kind": "outside_act",
                "exclusivity": agreement["exclusivity"],
                "act": act,
            },
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "voluntary_outside_act_prohibited"}],
        }
    if not agreement["requires_disclosure"]:
        return {
            "status": "not_violated",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "outside_act", "act": act, "allowed": True},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "outside_act_explicitly_allowed"}],
        }
    deadline = agreement["disclosure_deadline"]
    disclosures = [
        row for row in admitted_occurrence["disclosures"]
        if row["agreement_id"] == agreement["agreement_id"] and row["participant"] == actor
    ]
    if deadline != "before_act":
        return {
            "status": "unresolved",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "disclosure", "deadline": deadline},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "disclosure_deadline_not_concrete_in_v1"}],
        }
    if len(disclosures) == 1 and disclosures[0]["status"] == "timely":
        return {
            "status": "not_violated",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "disclosure", "deadline": "before_act"},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{
                "code": "timely_disclosure",
                "evidence_fingerprint": disclosures[0]["evidence"]["fingerprint"],
            }],
        }
    if len(disclosures) == 1 and disclosures[0]["status"] == "unknown":
        return {
            "status": "unresolved",
            "agreement_ref": agreement_ref,
            "boundary_ref": {"kind": "disclosure", "deadline": "before_act"},
            "occurrence_ref": occurrence_ref,
            "reason_refs": [{"code": "disclosure_evidence_unknown"}],
        }
    if len(disclosures) == 1 and disclosures[0]["status"] == "late":
        reason = {
            "code": "disclosure_after_deadline",
            "evidence_fingerprint": disclosures[0]["evidence"]["fingerprint"],
        }
    else:
        reason = {"code": "withheld_derived_after_before_act_deadline"}
    return {
        "status": "violated",
        "agreement_ref": agreement_ref,
        "boundary_ref": {"kind": "disclosure", "deadline": "before_act"},
        "occurrence_ref": occurrence_ref,
        "reason_refs": [reason],
    }


def _bake(validated: dict) -> dict:
    out = deepcopy(validated)
    out.pop("fingerprint", None)
    out["fingerprint"] = _record_fingerprint(out)
    return out


def bake_agreement_revision(
    value: object,
    current: list[dict],
    *,
    admitted_turn: int,
) -> dict:
    record = validate_agreement_revision(value)
    expected_revision = len(current) + 1
    if record["revision"] != expected_revision:
        raise ValueError("agreement revision must be exactly sequential")
    if current:
        if record.get("supersedes_fingerprint") != current[-1].get("fingerprint"):
            raise ValueError("agreement predecessor fingerprint does not match current state")
    record["effective_turn"] = _turn(admitted_turn, field="admitted_turn")
    return _bake(record)


def bake_social_occurrence(value: object, current: list[dict]) -> dict:
    record = validate_social_occurrence(value)
    expected_revision = len(current) + 1
    if record["revision"] != expected_revision:
        raise ValueError("occurrence revision must be exactly sequential")
    if current:
        if record.get("supersedes_fingerprint") != current[-1].get("fingerprint"):
            raise ValueError("occurrence predecessor fingerprint does not match current state")
    return _bake(record)


def bake_social_occurrence_supersession(value: object, current: list[dict]) -> dict:
    supersession = validate_social_occurrence_supersession(value)
    if not current:
        raise ValueError("occurrence supersession has no admitted predecessor")
    if supersession["revision"] != len(current) + 1:
        raise ValueError("occurrence revision must be exactly sequential")
    if supersession["supersedes_fingerprint"] != current[-1].get("fingerprint"):
        raise ValueError("occurrence predecessor fingerprint does not match current state")
    if supersession["action"] == "correct":
        record = deepcopy(supersession["replacement"])
        record["revision"] = supersession["revision"]
        record["action"] = "correct"
        record["supersedes_fingerprint"] = supersession["supersedes_fingerprint"]
        record["correction_cause"] = supersession["cause"]
        for key in ("lifecycle_source", "response_occurrence_id"):
            if key in supersession:
                record[key] = supersession[key]
        record.pop("fingerprint", None)
        return _bake(validate_social_occurrence(record))
    prior = deepcopy(current[-1])
    prior.update({
        "revision": supersession["revision"],
        "action": "retract",
        "supersedes_fingerprint": supersession["supersedes_fingerprint"],
        "correction_cause": supersession["cause"],
    })
    for key in ("lifecycle_source", "response_occurrence_id"):
        if key in supersession:
            prior[key] = supersession[key]
    prior.pop("fingerprint", None)
    return _bake(validate_social_occurrence(prior))


def bake_social_referent_binding(
    value: object,
    *,
    occurrence: Mapping[str, object],
    existing: list[dict],
) -> tuple[dict, bool]:
    binding = validate_social_referent_binding(value)
    occurrence_id = str(occurrence.get("occurrence_id") or "")
    anonymous_ids = {
        ref["anonymous_id"]
        for ref in occurrence.get("outside_participants") or []
        if isinstance(ref, Mapping) and ref.get("kind") == "anonymous"
    }
    if binding["occurrence_id"] != occurrence_id \
            or binding["anonymous_id"] not in anonymous_ids:
        raise ValueError("binding does not target one exact occurrence-local anonymous reference")
    matches = [
        row for row in existing
        if row.get("occurrence_id") == binding["occurrence_id"]
        and row.get("anonymous_id") == binding["anonymous_id"]
    ]
    baked = _bake(binding)
    if matches:
        if matches[-1] != baked:
            raise ValueError("anonymous referent is already bound to a different actor")
        return baked, True
    return baked, False


def bake_thread_transition(value: object, current: list[dict]) -> dict:
    record = validate_thread_transition(value)
    if record["kind"] == "promise" \
            and "promisor_actor_id" not in record:
        raise ValueError("fresh promise thread requires exact direction")
    if record["revision"] != len(current) + 1:
        raise ValueError("thread revision must be exactly sequential")
    if current and record.get("supersedes_fingerprint") != current[-1].get("fingerprint"):
        raise ValueError("thread predecessor fingerprint does not match current state")
    return _bake(record)


def project_social_occurrence(state: Mapping[str, object], occurrence: dict) -> dict:
    """Project manual anonymous bindings without editing the immutable occurrence."""
    clean = validate_social_occurrence(occurrence)
    bindings = state.get("social_referent_bindings") or []
    by_anonymous = {
        (row.get("occurrence_id"), row.get("anonymous_id")): row.get("actor_id")
        for row in bindings if isinstance(row, Mapping)
    }
    projected = deepcopy(clean)
    for index, ref in enumerate(projected["outside_participants"]):
        if ref["kind"] != "anonymous":
            continue
        actor_id = by_anonymous.get((clean["occurrence_id"], ref["anonymous_id"]))
        if actor_id:
            projected["outside_participants"][index] = {
                "kind": "actor",
                "actor_id": actor_id,
                "bound_from": deepcopy(ref),
            }
    return projected


def continuity_record_fingerprint(family: str, value: Mapping[str, object]) -> str:
    clean = {
        key: item for key, item in value.items()
        if key not in {"record_fingerprint", "fingerprint"}
    }
    return _fingerprint(
        CONTINUITY_SEED_FINGERPRINT_DOMAIN,
        {"family": family, "record": clean},
    )


def _portable_role(value: object, *, field: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != {"role"} \
            or value.get("role") not in _PORTABLE_ROLES:
        raise ValueError(f"{field} must use a character/current_persona placeholder")
    return {"role": str(value["role"])}


def _portable_general_record(value: object, *, family: str) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{family} record must be an object")
    caps = {
        "memories": ("memory_id", "text"),
        "player_visible_possessions_conditions": ("record_id", "summary"),
        "character_knowledge": ("knowledge_id", "statement"),
    }
    id_field, prose_field = caps[family]
    out = {
        id_field: _identifier(value.get(id_field), field=f"{family}.{id_field}"),
        prose_field: _text(value.get(prose_field), field=f"{family}.{prose_field}"),
    }
    if family == "memories":
        if value.get("visibility") not in {"shared", "character_private", "persona_visible"}:
            raise ValueError("memory visibility is unsupported")
        out["visibility"] = value["visibility"]
    elif family == "character_knowledge":
        if value.get("visibility") not in {"character_private", "shared"}:
            raise ValueError("Character knowledge visibility is unsupported")
        out["visibility"] = value["visibility"]
    else:
        if value.get("kind") not in {"possession", "condition"}:
            raise ValueError("player-visible record kind must be possession|condition")
        out["kind"] = value["kind"]
    return out


def _portable_relationship_cause(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("relationship cause must be an object")
    if value.get("quality") not in {"gain", "loss", "repair", "strain", "forgiveness"}:
        raise ValueError("relationship cause quality is unsupported")
    return {
        "cause_id": _identifier(value.get("cause_id"), field="relationship cause id"),
        "from": _portable_role(value.get("from"), field="relationship cause from"),
        "to": _portable_role(value.get("to"), field="relationship cause to"),
        "dimension": _identifier(value.get("dimension"), field="relationship dimension"),
        "quality": value["quality"],
        "reason": _text(value.get("reason"), field="relationship cause reason"),
        "cause_ref": validate_cause_ref(value.get("cause_ref")),
    }


def _portable_agreement(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("portable agreement revision must be an object")
    parties = value.get("parties")
    if not isinstance(parties, list) or len(parties) != 2:
        raise ValueError("portable agreement parties require both exact role placeholders")
    portable_parties = [
        _portable_role(party, field="portable agreement party") for party in parties
    ]
    if {party["role"] for party in portable_parties} != _PORTABLE_ROLES:
        raise ValueError("portable agreement parties require both exact role placeholders")
    substituted = dict(value)
    substituted["parties"] = [
        {"kind": "actor", "actor_id": f"portable:{party['role']}"}
        for party in portable_parties
    ]
    portable_acting_party = None
    if value.get("action") in {"withdraw", "release", "end"}:
        portable_acting_party = _portable_role(
            value.get("acting_party"),
            field="portable agreement acting_party",
        )
        substituted["acting_party"] = {
            "kind": "actor",
            "actor_id": f"portable:{portable_acting_party['role']}",
        }
    validated = validate_agreement_revision(substituted)
    validated["parties"] = portable_parties
    if portable_acting_party is not None:
        validated["acting_party"] = portable_acting_party
    validated.pop("fingerprint", None)
    return validated


def _portable_thread(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("portable open thread must be an object")
    participants = value.get("participants")
    if not isinstance(participants, list) or not participants:
        raise ValueError("portable open thread needs role placeholders")
    portable = [
        _portable_role(item, field="portable thread participant") for item in participants
    ]
    substituted = dict(value)
    substituted["participants"] = [
        {"kind": "actor", "actor_id": f"portable:{item['role']}"}
        for item in portable
    ]
    validated = validate_thread_transition(substituted)
    if validated["status"] != "open":
        raise ValueError("starting continuity may contain only open threads")
    validated["participants"] = portable
    validated.pop("fingerprint", None)
    return validated


def validate_starting_continuity(value: object) -> dict:
    """Validate independent bounded portable lists and seal every record separately."""
    if not isinstance(value, Mapping) or value.get("schema") != STARTING_CONTINUITY_SCHEMA:
        raise ValueError(f"starting continuity schema must be {STARTING_CONTINUITY_SCHEMA}")
    out: dict[str, Any] = {"schema": STARTING_CONTINUITY_SCHEMA}
    for family in _STARTING_FAMILIES:
        rows = value.get(family, [])
        if not isinstance(rows, list) or len(rows) > 64:
            raise ValueError(f"{family} must be an independently bounded record list")
        clean_rows = []
        for row in rows:
            clean_rows.append(validate_starting_continuity_record(family, row))
        if family in {"agreement_revisions", "open_threads"}:
            identity_key = (
                "agreement_id" if family == "agreement_revisions" else "thread_id"
            )
            lineages: dict[str, tuple[int, str]] = {}
            for clean in clean_rows:
                identity = str(clean[identity_key])
                prior = lineages.get(identity)
                expected_revision = 1 if prior is None else prior[0] + 1
                if clean["revision"] != expected_revision:
                    raise ValueError(
                        f"{family} revisions must be unique and exactly sequential",
                    )
                expected_predecessor = None if prior is None else prior[1]
                if clean.get("supersedes_fingerprint") != expected_predecessor:
                    raise ValueError(
                        f"{family} predecessor fingerprint is forged or stale",
                    )
                lineages[identity] = (
                    clean["revision"], clean["record_fingerprint"],
                )
        out[family] = clean_rows
    _canonical(out)
    return out


def validate_starting_continuity_record(family: object, value: object) -> dict:
    """Validate one portable seed independently so a bad sibling cannot roll back the Core."""
    if family not in _STARTING_FAMILIES:
        raise ValueError("starting continuity family is unsupported")
    exact_family = str(family)
    if exact_family in {
        "memories",
        "player_visible_possessions_conditions",
        "character_knowledge",
    }:
        clean = _portable_general_record(value, family=exact_family)
    elif exact_family == "relationship_causes":
        clean = _portable_relationship_cause(value)
    elif exact_family == "agreement_revisions":
        clean = _portable_agreement(value)
    else:
        clean = _portable_thread(value)
    fingerprint = continuity_record_fingerprint(exact_family, clean)
    if isinstance(value, Mapping) and value.get("record_fingerprint") is not None \
            and value.get("record_fingerprint") != fingerprint:
        raise ValueError(f"{exact_family} record_fingerprint is forged or stale")
    clean["record_fingerprint"] = fingerprint
    _canonical(clean)
    return clean


def _resolve_portable_role_ref(
    value: Mapping[str, object],
    *,
    character_actor_id: str,
    persona_actor_id: str,
) -> dict:
    role = str(value.get("role") or "")
    actor_id = (
        character_actor_id if role == "character"
        else persona_actor_id if role == "current_persona"
        else ""
    )
    if not actor_id:
        raise ValueError("portable continuity role could not be resolved")
    return {"kind": "actor", "actor_id": actor_id}


def starting_continuity_op(
    family: object,
    value: object,
    *,
    character_actor_id: str,
    persona_actor_id: str,
) -> dict:
    """Resolve one validated portable record into an existing typed state operation."""
    exact_family = str(family)
    clean = validate_starting_continuity_record(exact_family, value)
    resolved = deepcopy(clean)
    fingerprint = str(clean["record_fingerprint"])
    actors = [character_actor_id, persona_actor_id]

    if exact_family == "memories":
        visibility = str(clean["visibility"])
        scoped = (
            [character_actor_id]
            if visibility == "character_private"
            else actors
        )
        op = {
            "op": "memory_event",
            "text": clean["text"],
            "participants": actors,
            "importance": 7,
            "tags": ["starting_continuity", str(clean["memory_id"])],
            "visibility": "actor_scoped",
            "scoped_actors": scoped,
        }
        resolved["visibility"] = "actor_scoped"
        resolved["scoped_actors"] = scoped
    elif exact_family == "player_visible_possessions_conditions":
        op = {
            "op": "set_attribute",
            "entity": character_actor_id,
            "key": f"chat_observable.{clean['record_id']}",
            "value": {
                "kind": str(clean["kind"]),
                "summary": str(clean["summary"]),
                "visibility": "actor_scoped",
                "scoped_actors": sorted(actors),
            },
        }
        resolved["entity"] = character_actor_id
        resolved["visibility"] = "actor_scoped"
        resolved["scoped_actors"] = sorted(actors)
    elif exact_family == "character_knowledge":
        scoped = (
            [character_actor_id]
            if clean["visibility"] == "character_private"
            else actors
        )
        op = {
            "op": "belief_acquire",
            "holder": character_actor_id,
            "statement": clean["statement"],
            "stance": "knows",
            "source": "creator_starting_continuity",
            "visibility": "actor_scoped",
            "scoped_actors": scoped,
        }
        resolved["holder"] = character_actor_id
        resolved["visibility"] = "actor_scoped"
        resolved["scoped_actors"] = scoped
    elif exact_family == "relationship_causes":
        from_ref = _resolve_portable_role_ref(
            clean["from"],
            character_actor_id=character_actor_id,
            persona_actor_id=persona_actor_id,
        )
        to_ref = _resolve_portable_role_ref(
            clean["to"],
            character_actor_id=character_actor_id,
            persona_actor_id=persona_actor_id,
        )
        delta = -1 if clean["quality"] in {"loss", "strain"} else 1
        op = {
            "op": "relationship_adj",
            "from_char": from_ref["actor_id"],
            "to_char": to_ref["actor_id"],
            "dimension": clean["dimension"],
            "delta": delta,
            "reason": clean["reason"],
            "quality": clean["quality"],
            "cause_ref": clean["cause_ref"],
        }
        resolved["from"] = from_ref
        resolved["to"] = to_ref
    elif exact_family == "agreement_revisions":
        resolved["parties"] = [
            _resolve_portable_role_ref(
                party,
                character_actor_id=character_actor_id,
                persona_actor_id=persona_actor_id,
            )
            for party in clean["parties"]
        ]
        if isinstance(clean.get("acting_party"), Mapping):
            resolved["acting_party"] = _resolve_portable_role_ref(
                clean["acting_party"],
                character_actor_id=character_actor_id,
                persona_actor_id=persona_actor_id,
            )
        resolved["visibility"] = "actor_scoped"
        resolved["scoped_actors"] = sorted({
            str(party["actor_id"]) for party in resolved["parties"]
        })
        resolved.pop("record_fingerprint", None)
        op = {"op": "relationship_agreement_revision", "record": resolved}
    else:
        resolved["participants"] = [
            _resolve_portable_role_ref(
                participant,
                character_actor_id=character_actor_id,
                persona_actor_id=persona_actor_id,
            )
            for participant in clean["participants"]
        ]
        resolved["visibility"] = "actor_scoped"
        resolved["scoped_actors"] = sorted({
            str(participant["actor_id"]) for participant in resolved["participants"]
        })
        resolved.pop("record_fingerprint", None)
        op = {"op": "continuity_thread_transition", "record": resolved}

    op["_continuity_seed"] = {
        "family": exact_family,
        "record_fingerprint": fingerprint,
        "record": resolved,
    }
    return op


def _actor_ref_ids(value: object) -> set[str]:
    """Return only exact actor ids from one typed participant collection."""
    rows = value if isinstance(value, list) else [value]
    return {
        str(row.get("actor_id") or "")
        for row in rows
        if isinstance(row, Mapping)
        and row.get("kind") == "actor"
        and str(row.get("actor_id") or "")
    }


def _world_relevance_terms(value: object) -> set[str]:
    """Return small lexical anchors used only for bounded Chat packet selection."""
    terms: set[str] = set()
    for raw in _WORLD_RELEVANCE_TOKEN_RE.findall(str(value or "").casefold()):
        if len(raw) < 3 or raw in _WORLD_RELEVANCE_STOPWORDS:
            continue
        term = raw
        if len(term) > 4 and term.endswith("ies"):
            term = term[:-3] + "y"
        elif len(term) > 5 and term.endswith("ing"):
            term = term[:-3]
        elif len(term) > 4 and term.endswith("ed"):
            term = term[:-2]
        elif len(term) > 4 and term.endswith("es"):
            term = term[:-2]
        elif len(term) > 3 and term.endswith("s") \
                and not term.endswith(("ss", "us", "is")):
            term = term[:-1]
        if len(term) >= 3 and term not in _WORLD_RELEVANCE_STOPWORDS:
            terms.add(term)
    return terms


def _chat_world_context(
    state: Mapping[str, Any],
    threads: Mapping[str, list[dict[str, Any]]],
) -> tuple[str, set[str]]:
    """Build a relevance query from visible Core, current scene, and open threads."""
    fragments: list[str] = []
    core_state = state.get("chat_core") or {}
    core = core_state.get("core") if isinstance(core_state, Mapping) else {}
    if isinstance(core, Mapping):
        for key in (
            "name", "description", "personality", "scenario", "anchors", "boundaries",
        ):
            value = core.get(key)
            if isinstance(value, str):
                fragments.append(value)
            elif isinstance(value, list):
                fragments.extend(
                    str(item) for item in value[:16]
                    if isinstance(item, (str, int, float)) and str(item).strip()
                )
    scene = state.get("scene") or {}
    if isinstance(scene, Mapping):
        for key in (
            "location", "location_id", "phase", "summary", "situation",
            "weather", "participants",
        ):
            value = scene.get(key)
            if isinstance(value, (str, int, float)):
                fragments.append(str(value))
            elif isinstance(value, list):
                fragments.extend(
                    str(item) for item in value[:16]
                    if isinstance(item, (str, int, float)) and str(item).strip()
                )
    for revisions in threads.values():
        if not revisions or revisions[-1].get("status") != "open":
            continue
        latest = revisions[-1]
        for key in ("kind", "summary"):
            value = latest.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value)
    text = "\n".join(fragment[:1200] for fragment in fragments if fragment)
    return text, _world_relevance_terms(text)


def project_continuity(
    state: Mapping[str, Any],
    *,
    viewer_actor_id: str,
    player_actor_id: str = "",
    limit: int = 64,
) -> dict[str, Any]:
    """Project durable Living Character state for one exact actor.

    This is a read-only audience boundary.  It does not infer shared knowledge:
    actor-scoped rows must name the viewer, while Player-visible rows additionally
    require the exact bound Persona actor.
    """
    from .knowledge import record_visible_to, select_knowledge

    viewer = _identifier(viewer_actor_id, field="viewer_actor_id")
    player = (
        _identifier(player_actor_id, field="player_actor_id")
        if player_actor_id
        else ""
    )
    cap = max(1, min(int(limit), 128))

    def visible(row: Mapping[str, Any]) -> bool:
        return record_visible_to(
            row,
            viewer_actor_id=viewer,
            player_actor_id=player,
            blank_visibility="deny",
        )

    memories = [
        deepcopy(dict(row))
        for row in (state.get("memories") or [])[-cap:]
        if isinstance(row, Mapping) and visible(row)
    ]
    observables = []
    for key, value in sorted(
        ((state.get("attributes") or {}).get(viewer) or {}).items()
    ):
        if not str(key).startswith("chat_observable.") \
                or not isinstance(value, Mapping) \
                or not visible(value):
            continue
        observables.append({
            "record_id": str(key).removeprefix("chat_observable."),
            "kind": str(value.get("kind") or ""),
            "summary": str(value.get("summary") or "")[:600],
        })
        if len(observables) >= cap:
            break

    relationships: dict[str, Any] = {}
    for key, raw in (state.get("relationships") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        source, _, target = str(key).partition("->")
        scoped = [source, target]
        if record_visible_to(
            {
                **raw,
                "visibility": raw.get("visibility") or "actor_scoped",
                "scoped_actors": raw.get("scoped_actors") or scoped,
            },
            viewer_actor_id=viewer,
            player_actor_id=player,
        ):
            relationships[str(key)] = deepcopy(dict(raw))

    agreements: dict[str, list[dict[str, Any]]] = {}
    for record_id, revisions in (state.get("relationship_agreements") or {}).items():
        if not isinstance(revisions, list):
            continue
        rows = [
            deepcopy(dict(row))
            for row in revisions
            if isinstance(row, Mapping)
            and record_visible_to(
                row,
                viewer_actor_id=viewer,
                player_actor_id=player,
                blank_visibility="deny",
            )
        ]
        if rows:
            agreements[str(record_id)] = rows[-cap:]

    occurrences: dict[str, list[dict[str, Any]]] = {}
    for record_id, revisions in (state.get("social_occurrences") or {}).items():
        if not isinstance(revisions, list):
            continue
        rows = []
        for row in revisions:
            if not isinstance(row, Mapping):
                continue
            if record_visible_to(
                row,
                viewer_actor_id=viewer,
                player_actor_id=player,
                blank_visibility="deny",
            ):
                projected = deepcopy(dict(row))
                motive = projected.get("motive_claim_ref")
                if isinstance(motive, Mapping):
                    visible_claims = [
                        claim
                        for claim in state.get("claims") or []
                        if isinstance(claim, Mapping)
                        and claim.get("claim_id") == motive.get("claim_id")
                        and claim.get("fingerprint") == motive.get("fingerprint")
                        and record_visible_to(
                            claim,
                            viewer_actor_id=viewer,
                            player_actor_id=player,
                            blank_visibility="deny",
                        )
                    ]
                    if len(visible_claims) != 1:
                        projected["motive_claim_ref"] = None
                rows.append(projected)
        if rows:
            occurrences[str(record_id)] = rows[-cap:]

    threads: dict[str, list[dict[str, Any]]] = {}
    for record_id, revisions in (state.get("continuity_threads") or {}).items():
        if not isinstance(revisions, list):
            continue
        rows = [
            deepcopy(dict(row))
            for row in revisions
            if isinstance(row, Mapping)
            and record_visible_to(
                row,
                viewer_actor_id=viewer,
                player_actor_id=player,
                blank_visibility="deny",
            )
        ]
        if rows:
            threads[str(record_id)] = rows[-cap:]

    world_context, world_context_terms = _chat_world_context(state, threads)
    knowledge = select_knowledge(
        state,
        audience="actor",
        actor_id=viewer,
        player_actor_id=player,
        query=world_context,
        limit=cap,
        include_history=False,
    )
    authored = (
        (state.get("creator_world") or {}).get("document")
        if isinstance(state.get("creator_world"), Mapping)
        else {}
    )
    safe_world: dict[str, Any] = {}
    remaining = 4000
    if isinstance(authored, Mapping):
        # Stable world identity remains available; prose and lore are selected
        # against the Character-visible current context instead of riding every turn.
        for key in ("name", "genre"):
            value = authored.get(key)
            if isinstance(value, str):
                clean = value.strip()[: min(1200, remaining)]
                if clean:
                    safe_world[key] = clean
                    remaining -= len(clean)
            if remaining <= 0:
                break
        for key in ("setting", "premise", "summary", "themes", "lore"):
            if remaining <= 0:
                break
            value = authored.get(key)
            if isinstance(value, str):
                clean = value.strip()[: min(1200, remaining)]
                if clean and (
                    _world_relevance_terms(clean) & world_context_terms
                ):
                    safe_world[key] = clean
                    remaining -= len(clean)
            elif isinstance(value, list):
                rows = [
                    str(item).strip()[:400]
                    for item in value[:16]
                    if isinstance(item, (str, int, float))
                    and str(item).strip()
                    and (
                        _world_relevance_terms(item) & world_context_terms
                    )
                ]
                while rows and sum(len(item) for item in rows) > remaining:
                    rows.pop()
                if rows:
                    safe_world[key] = rows
                    remaining -= sum(len(item) for item in rows)
    selected_events = [
        deepcopy(row)
        for row in (knowledge.get("events") or [])
        if _world_relevance_terms(row.get("statement")) & world_context_terms
    ][:16]
    world = {
        "authored": safe_world,
        "events": selected_events,
    }
    return {
        "viewer_actor_id": viewer,
        "observables": observables,
        "memories": memories,
        "knowledge": knowledge,
        "relationships": relationships,
        "agreements": agreements,
        "social_occurrences": occurrences,
        "open_threads": {
            key: rows
            for key, rows in threads.items()
            if rows and rows[-1].get("status") == "open"
        },
        "world": world,
    }


def _accepted_message_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _admission_evidence(*, source: str, text: str, start: int, end: int) -> dict:
    row = {
        "schema": OCCURRENCE_ADMISSION_SCHEMA,
        "source": source,
        "message_fingerprint": _accepted_message_fingerprint(text),
        "start": start,
        "end": end,
        "accepted": True,
        "code_sealed": True,
    }
    row["fingerprint"] = _fingerprint(
        OCCURRENCE_ADMISSION_FINGERPRINT_DOMAIN, row,
    )
    return validate_occurrence_admission_evidence(row)


def _recognized_evidence(*, kind: str, text: str, start: int, end: int) -> dict:
    row = {
        "schema": EVIDENCE_SCHEMA,
        "kind": kind,
        "message_fingerprint": _accepted_message_fingerprint(text),
        "start": start,
        "end": end,
        "accepted": True,
        "code_sealed": True,
    }
    row["fingerprint"] = _fingerprint(EVIDENCE_FINGERPRINT_DOMAIN, row)
    return _evidence(row, kind=kind)


def scope_chat_memory_ops(
    ops: object,
    *,
    character_actor_id: str,
    persona_actor_id: str,
) -> list[dict]:
    """Replace untrusted memory audience hints with exact admitted Chat actors."""
    if not isinstance(ops, list):
        raise ValueError("Chat extraction operations must be a list")
    character = _identifier(character_actor_id, field="character_actor_id")
    persona = _identifier(persona_actor_id, field="persona_actor_id")
    out: list[dict] = []
    for raw in ops:
        if not isinstance(raw, Mapping):
            out.append(raw)
            continue
        row = deepcopy(dict(raw))
        if row.get("op") != "memory_event":
            out.append(row)
            continue
        # A deferred Chat memory is Character-side continuity extracted from the
        # accepted Character/Persona exchange. The model may suggest content, but
        # it cannot choose either the occurrence actors or the retrieval audience.
        row["participants"] = sorted({character, persona})
        row["visibility"] = "actor_scoped"
        row["scoped_actors"] = [character]
        out.append(row)
    return out


def _subclaim_context_graph(
    *,
    base_graph: Mapping[str, object],
    text: str,
    start: int,
    end: int,
    kind: str,
    status: str,
    content_start: int,
) -> dict | None:
    """Give one exact subclaim its own semantic anchor in the complete role message."""
    from .semantic_occurrence import OccurrenceAnchor, build_occurrence_graph

    anchors: list[OccurrenceAnchor] = []
    seen: set[tuple[str, str, int, int, str]] = set()
    for node in base_graph.get("occurrences") or ():
        if not isinstance(node, Mapping):
            return None
        for field, anchor_kind in (
            ("actors", "actor"),
            ("targets", "target"),
            ("capabilities", "capability"),
            ("actions", "action"),
        ):
            for binding in node.get(field) or ():
                if not isinstance(binding, Mapping):
                    return None
                span = binding.get("span")
                if not isinstance(span, list) or len(span) != 2:
                    return None
                anchor_key = (
                    anchor_kind,
                    str(binding.get("identity") or ""),
                    int(span[0]),
                    int(span[1]),
                    str(binding.get("source") or ""),
                )
                if not anchor_key[1] or anchor_key in seen:
                    continue
                seen.add(anchor_key)
                anchors.append(OccurrenceAnchor(*anchor_key))

    owned_start, owned_end = start, end
    while owned_start < owned_end and text[owned_start].isspace():
        owned_start += 1
    while owned_end > owned_start and (
        text[owned_end - 1].isspace() or text[owned_end - 1] in ".!?;"
    ):
        owned_end -= 1
    actor_match = re.search(r"\bI\b", text[owned_start:owned_end], re.IGNORECASE)
    if actor_match is None or owned_end <= owned_start:
        return None
    actor_start = owned_start + actor_match.start()
    actor_end = owned_start + actor_match.end()
    anchors.extend((
        OccurrenceAnchor(
            "actor",
            "chat_role_speaker",
            actor_start,
            actor_end,
            "chat_role",
        ),
        OccurrenceAnchor(
            "action",
            f"chat_subclaim_{kind}_{status}",
            owned_start,
            owned_end,
            "chat_social_subclaim",
        ),
    ))

    authority = base_graph.get("authority")
    if not isinstance(authority, Mapping):
        return None
    try:
        return build_occurrence_graph(
            text,
            detection_text=(
                " " * content_start + text[content_start:]
                if content_start
                else text
            ),
            anchors=anchors,
            issuer=str(authority.get("issuer") or ""),
            channel=str(authority.get("channel") or ""),
            lifecycle_phase=str(authority.get("lifecycle_phase") or ""),
            grammar_version=str(authority.get("grammar_version") or ""),
            operation_family=str(authority.get("operation_family") or ""),
        )
    except (TypeError, ValueError):
        return None


def _seal_social_subclaims(
    *,
    raw: Mapping[str, object],
    source_name: str,
    text: str,
    parent_span: tuple[int, int],
    action: str,
    subject_actor_id: str,
    context_frame: Mapping[str, object],
) -> tuple[list[dict], list[dict], list[dict]] | None:
    from .semantic_fabric import recognize_chat_social_subclaim
    from .semantic_occurrence import chat_context_allows_actual_span

    output: dict[str, list[dict]] = {
        "voluntariness": [],
        "consent": [],
        "disclosure": [],
    }
    specs = (
        ("voluntariness", "voluntariness", {"voluntary", "coerced", "assaulted", "unknown"}),
        ("consent", "consent", {"granted", "refused", "unknown"}),
        ("disclosure", "disclosure", {"timely", "late", "unknown"}),
    )
    for raw_key, kind, statuses in specs:
        values = raw.get(raw_key, [])
        if not isinstance(values, list) or len(values) > 64:
            return None
        for value in values:
            if not isinstance(value, Mapping) or value.get("status") not in statuses:
                return None
            status = str(value["status"])
            try:
                participant = validate_participant_ref(
                    value.get("participant"),
                    context=(
                        "voluntariness"
                        if kind == "voluntariness"
                        else "consent"
                        if kind == "consent"
                        else "agreement party"
                    ),
                )
            except ValueError:
                return None
            if participant != {
                "kind": "actor",
                "actor_id": subject_actor_id,
            }:
                return None
            evidence = None
            if status != "unknown":
                span = value.get("source_span")
                if not isinstance(span, Mapping) \
                        or str(span.get("source") or "") != source_name:
                    return None
                start, end = span.get("start"), span.get("end")
                if isinstance(start, bool) or not isinstance(start, int) or start < 0 \
                        or isinstance(end, bool) or not isinstance(end, int) \
                        or end <= start or end > len(text) \
                        or (start, end) == parent_span:
                    return None
                if not recognize_chat_social_subclaim(
                    text[start:end],
                    kind=kind,
                    status=status,
                ):
                    return None
                base_graph = context_frame.get("context_graph")
                if not isinstance(base_graph, Mapping):
                    return None
                content_start = int(
                    context_frame.get("context_content_start") or 0
                )
                graph = _subclaim_context_graph(
                    base_graph=base_graph,
                    text=text,
                    start=start,
                    end=end,
                    kind=kind,
                    status=status,
                    content_start=content_start,
                )
                if graph is None:
                    return None
                if not chat_context_allows_actual_span(
                    graph,
                    text,
                    start,
                    end,
                    content_start=content_start,
                    expected_speaker_label=str(
                        context_frame.get("expected_speaker_label") or ""
                    ),
                ):
                    return None
                evidence = _recognized_evidence(
                    kind=kind,
                    text=text,
                    start=start,
                    end=end,
                )
            if kind == "voluntariness":
                output[kind].append({
                    "participant": participant,
                    "status": status,
                    "evidence": evidence,
                })
            elif kind == "consent":
                if value.get("act") != action \
                        or value.get("channel") != "in_fiction":
                    return None
                output[kind].append({
                    "participant": participant,
                    "act": action,
                    "status": status,
                    "channel": "in_fiction",
                    "evidence": evidence,
                })
            else:
                try:
                    agreement_id = _identifier(
                        value.get("agreement_id"),
                        field="disclosure.agreement_id",
                    )
                except ValueError:
                    return None
                output[kind].append({
                    "agreement_id": agreement_id,
                    "participant": participant,
                    "status": status,
                    "evidence": evidence,
                })
    return (
        output["voluntariness"],
        output["consent"],
        output["disclosure"],
    )


def _proposal_scope(
    *,
    source_segment: str,
    subject_actor_id: str,
    counterpart_actor_id: str,
    participants: list[dict],
) -> tuple[str, list[str]]:
    participant_actors = sorted(_actor_ref_ids(participants))
    if source_segment == "offscreen_third_party":
        return "hidden", []
    if source_segment == "direct_dialogue":
        return "actor_scoped", sorted({
            subject_actor_id, counterpart_actor_id, *participant_actors,
        })
    if source_segment == "shared_observation":
        return "actor_scoped", sorted({
            subject_actor_id, counterpart_actor_id, *participant_actors,
        })
    if source_segment == "private_action_or_thought":
        return "actor_scoped", sorted({
            subject_actor_id, *participant_actors,
        })
    raise ValueError("automatic social proposal source_segment is unsupported")


def _code_owned_social_participants(
    recognized: Mapping[str, object],
    proposed: object,
    *,
    counterpart_actor_id: str,
    continuity_state: Mapping[str, Any] | None,
) -> list[dict] | None:
    """Bind only one grammatically targeted, already-admitted actor."""
    if not isinstance(proposed, list) or len(proposed) != 1 \
            or not isinstance(proposed[0], Mapping):
        return None
    binding = recognized.get("participant_binding")
    if not isinstance(binding, Mapping):
        return None
    raw = proposed[0]
    if binding.get("kind") == "counterpart":
        if raw.get("kind") != "actor" \
                or str(raw.get("actor_id") or "") != counterpart_actor_id:
            return None
        return [{"kind": "actor", "actor_id": counterpart_actor_id}]
    if binding.get("kind") != "named" or raw.get("kind") != "actor" \
            or not isinstance(continuity_state, Mapping):
        return None
    label = str(binding.get("label") or "").strip()
    if not label:
        return None
    from .state import resolve_unique_entity_ref

    actor_id = resolve_unique_entity_ref(dict(continuity_state), label)
    entity = (continuity_state.get("entities") or {}).get(actor_id) \
        if actor_id else None
    if not actor_id \
            or str(raw.get("actor_id") or "") != actor_id \
            or not isinstance(entity, Mapping) \
            or entity.get("kind") not in {"actor", "character", "persona"}:
        return None
    return [{"kind": "actor", "actor_id": actor_id}]


def _automatic_agreement_transition(
    *,
    action: str,
    recognized: Mapping[str, object],
    text: str,
    start: int,
    end: int,
    session_id: str,
    branch_id: str,
    turn: int,
    subject_actor_id: str,
    counterpart_actor_id: str,
    lifecycle_source: str,
    response_occurrence_id: str,
    continuity_state: Mapping[str, Any] | None,
) -> dict | None:
    agreement_action = action.removeprefix("agreement_")
    intent = str(recognized.get("agreement_intent") or "")
    terms = recognized.get("agreement_terms")
    if agreement_action in {"create", "amend"} and (
        intent not in {"propose", "accept"} or not isinstance(terms, Mapping)
    ):
        return None
    parties = [
        {"kind": "actor", "actor_id": subject_actor_id},
        {"kind": "actor", "actor_id": counterpart_actor_id},
    ]
    wanted = {subject_actor_id, counterpart_actor_id}
    latest_rows: list[dict] = []
    if isinstance(continuity_state, Mapping):
        for revisions in (
            continuity_state.get("relationship_agreements") or {}
        ).values():
            if not isinstance(revisions, list) or not revisions:
                continue
            latest = revisions[-1]
            if isinstance(latest, Mapping) \
                    and _actor_ref_ids(latest.get("parties")) == wanted:
                latest_rows.append(dict(latest))

    current_evidence = _recognized_evidence(
        kind="assent",
        text=text,
        start=start,
        end=end,
    )
    if agreement_action == "create" and intent == "propose":
        if latest_rows:
            return None
        material = {
            "schema": "aetherstate-auto-agreement-key/1",
            "session_id": session_id,
            "branch_id": branch_id,
            "parties": sorted(wanted),
        }
        record: dict[str, Any] = {
            "schema": AGREEMENT_SCHEMA,
            "agreement_id": "agreement.auto." + hashlib.sha256(
                _canonical(material)
            ).hexdigest(),
            "revision": 1,
            "action": "create",
            "parties": parties,
            "exclusivity": terms.get("exclusivity"),
            "allowed_outside_acts": deepcopy(
                terms.get("allowed_outside_acts") or []
            ),
            "requires_disclosure": terms.get("requires_disclosure"),
            "disclosure_deadline": terms.get("disclosure_deadline"),
            "effective_turn": turn,
            "assent": [{
                "party": {"kind": "actor", "actor_id": subject_actor_id},
                "status": "proposed",
                "evidence": current_evidence,
            }],
        }
    elif agreement_action == "create" and intent == "accept":
        if len(latest_rows) != 1:
            return None
        latest = latest_rows[0]
        prior_assent = latest.get("assent") or []
        if len(prior_assent) != 1 \
                or prior_assent[0].get("status") != "proposed" \
                or (prior_assent[0].get("party") or {}).get("actor_id") \
                != counterpart_actor_id:
            return None
        exact_terms = {
            key: latest.get(key)
            for key in (
                "exclusivity",
                "allowed_outside_acts",
                "requires_disclosure",
                "disclosure_deadline",
            )
        }
        if dict(terms) != exact_terms:
            return None
        record = {
            key: deepcopy(latest[key])
            for key in (
                "schema", "agreement_id", "parties", "exclusivity",
                "allowed_outside_acts", "requires_disclosure",
                "disclosure_deadline",
            )
        }
        record.update({
            "revision": int(latest.get("revision", 0)) + 1,
            "action": "amend",
            "effective_turn": turn,
            "supersedes_fingerprint": latest.get("fingerprint"),
            "assent": [{
                "party": deepcopy(prior_assent[0]["party"]),
                "status": "accepted",
                "evidence": deepcopy(prior_assent[0]["evidence"]),
            }, {
                "party": {"kind": "actor", "actor_id": subject_actor_id},
                "status": "accepted",
                "evidence": current_evidence,
            }],
        })
    elif agreement_action == "amend" and intent == "propose":
        if len(latest_rows) != 1:
            return None
        latest = latest_rows[0]
        if latest.get("exclusivity") == "ended" \
                or not automatic_agreement_has_exact_assent(latest):
            return None
        record = {
            "schema": latest["schema"],
            "agreement_id": latest["agreement_id"],
            "revision": int(latest.get("revision", 0)) + 1,
            "action": "amend",
            "parties": deepcopy(latest["parties"]),
            "exclusivity": terms.get("exclusivity"),
            "allowed_outside_acts": deepcopy(
                terms.get("allowed_outside_acts") or []
            ),
            "requires_disclosure": terms.get("requires_disclosure"),
            "disclosure_deadline": terms.get("disclosure_deadline"),
            "effective_turn": turn,
            "supersedes_fingerprint": latest.get("fingerprint"),
            "assent": [{
                "party": {"kind": "actor", "actor_id": subject_actor_id},
                "status": "proposed",
                "evidence": current_evidence,
            }],
        }
    elif agreement_action == "amend" and intent == "accept":
        if len(latest_rows) != 1:
            return None
        latest = latest_rows[0]
        prior_assent = latest.get("assent") or []
        if latest.get("action") != "amend" \
                or len(prior_assent) != 1 \
                or prior_assent[0].get("status") != "proposed" \
                or (prior_assent[0].get("party") or {}).get("actor_id") \
                != counterpart_actor_id:
            return None
        exact_terms = {
            key: latest.get(key)
            for key in (
                "exclusivity",
                "allowed_outside_acts",
                "requires_disclosure",
                "disclosure_deadline",
            )
        }
        if dict(terms) != exact_terms:
            return None
        record = {
            key: deepcopy(latest[key])
            for key in (
                "schema", "agreement_id", "parties", "exclusivity",
                "allowed_outside_acts", "requires_disclosure",
                "disclosure_deadline",
            )
        }
        record.update({
            "revision": int(latest.get("revision", 0)) + 1,
            "action": "amend",
            "effective_turn": turn,
            "supersedes_fingerprint": latest.get("fingerprint"),
            "assent": [{
                "party": deepcopy(prior_assent[0]["party"]),
                "status": "accepted",
                "evidence": deepcopy(prior_assent[0]["evidence"]),
            }, {
                "party": {"kind": "actor", "actor_id": subject_actor_id},
                "status": "accepted",
                "evidence": current_evidence,
            }],
        })
    else:
        if len(latest_rows) != 1:
            return None
        latest = latest_rows[0]
        record = {
            key: deepcopy(latest[key])
            for key in (
                "schema", "agreement_id", "parties", "exclusivity",
                "allowed_outside_acts", "requires_disclosure",
                "disclosure_deadline",
            )
        }
        record.update({
            "revision": int(latest.get("revision", 0)) + 1,
            "action": agreement_action,
            "effective_turn": turn,
            "supersedes_fingerprint": latest.get("fingerprint"),
        })
        if agreement_action in {"withdraw", "release", "end"}:
            record["exclusivity"] = "ended"

    record["lifecycle_source"] = lifecycle_source
    record["response_occurrence_id"] = response_occurrence_id
    if agreement_action not in {"create", "amend"}:
        record["assent"] = deepcopy(latest_rows[0].get("assent") or [])
        record["acting_party"] = {
            "kind": "actor",
            "actor_id": subject_actor_id,
        }
        record["evidence"] = _recognized_evidence(
            kind="agreement_transition",
            text=text,
            start=start,
            end=end,
        )
    try:
        validate_agreement_revision(record)
    except ValueError:
        return None
    return {"op": "relationship_agreement_revision", "record": record}


def _verified_motive_claim_ref(
    value: object,
    *,
    claim_records: object,
    session_id: str,
    branch_id: str,
    turn: int,
    lifecycle_source: str,
    response_occurrence_id: str,
    subject_actor_id: str,
    player_actor_id: str,
    occurrence_scoped_actors: list[str],
    action: str,
    recognized: Mapping[str, object],
    source_text: str,
    continuity_state: Mapping[str, Any] | None,
) -> dict | None:
    if not isinstance(value, Mapping) or not isinstance(claim_records, list):
        return None
    claim_id = str(value.get("claim_id") or "")
    fingerprint = str(value.get("fingerprint") or "")
    try:
        from .claim_frame import (
            validate_claim_frame_against_source,
            validate_claim_record,
        )
        from .knowledge import record_visible_to

        matches = [
            validate_claim_record(row)
            for row in claim_records
            if isinstance(row, Mapping)
            and row.get("claim_id") == claim_id
            and row.get("fingerprint") == fingerprint
        ]
    except (TypeError, ValueError):
        return None
    if len(matches) != 1:
        return None
    claim = matches[0]
    scope = (
        continuity_state.get("knowledge_record_scope")
        if isinstance(continuity_state, Mapping)
        else None
    )
    allowed_branches = {branch_id}
    if isinstance(scope, Mapping) \
            and str(scope.get("branch_id") or "") == branch_id \
            and str(scope.get("session_id") or "") == session_id:
        allowed_branches.update(
            str(value)
            for value in scope.get("source_branch_ids") or []
            if str(value)
        )
    claim_branch = str(claim.get("branch_id") or "")
    if claim_branch not in allowed_branches \
            or (
                claim_branch == branch_id
                and str(claim.get("session_id") or "") != session_id
            ) \
            or int(claim.get("turn", -1)) != turn:
        return None
    lifecycle = str(claim.get("lifecycle_source") or "")
    response_id = str(claim.get("response_occurrence_id") or "")
    if lifecycle != lifecycle_source:
        return None
    if lifecycle in {"assistant_response", "deferred_extraction"} \
            and response_id != response_occurrence_id:
        return None
    if lifecycle == "user_text" and response_id:
        return None
    try:
        frame = validate_claim_frame_against_source(
            claim.get("frame"),
            source_text,
        )
    except (TypeError, ValueError):
        return None
    if frame.get("speaker") != subject_actor_id \
            or frame.get("claim_class") != "assertion" \
            or frame.get("proposition_polarity") != "positive" \
            or frame.get("modality") != "asserted":
        return None
    roles = frame.get("proposition_roles")
    if not isinstance(roles, list):
        return None
    subject_values = [
        str(row.get("value") or "").strip()
        for row in roles
        if isinstance(row, Mapping)
        and row.get("role") == "subject_candidate"
        and row.get("status") == "resolved"
    ]
    predicate_values = [
        str(row.get("value") or "").strip()
        for row in roles
        if isinstance(row, Mapping)
        and row.get("role") == "predicate"
        and row.get("status") == "resolved"
    ]
    if len(subject_values) != 1 or subject_values[0].casefold() != "i" \
            or len(predicate_values) != 1:
        return None
    binding = recognized.get("participant_binding")
    target_label = (
        "you"
        if isinstance(binding, Mapping) and binding.get("kind") == "counterpart"
        else str(binding.get("label") or "")
        if isinstance(binding, Mapping)
        else ""
    )
    predicate = predicate_values[0]
    target_surface = re.escape(target_label)
    concrete_act = str(recognized.get("concrete_act") or "")
    act_patterns = {
        "kiss": r"kiss(?:ed)?",
        "hug": r"hug(?:ged)?",
        "embrace": r"embrac(?:e|ed)",
        "cuddle": r"cuddl(?:e|ed)",
        "caress": r"caress(?:ed)?",
        "sex_with": r"(?:have|had)\s+sex\s+with",
        "sleep_with": r"(?:sleep|slept)\s+with",
        "touch_sexually": r"(?:touch|touched)",
    }
    act_pattern = act_patterns.get(concrete_act)
    if action not in {"romantic_contact", "sexual_contact"} or not act_pattern:
        return None
    predicate_pattern = (
        rf"{act_pattern}\s+{target_surface}"
        + (r"\s+sexually" if concrete_act == "touch_sexually" else "")
        + r"\s+because\s+\S(?:.*\S)?"
    )
    if not target_label or re.fullmatch(
        predicate_pattern,
        predicate,
        re.IGNORECASE,
    ) is None:
        return None
    if not record_visible_to(
        claim,
        viewer_actor_id=subject_actor_id,
        player_actor_id=player_actor_id,
        blank_visibility="deny",
    ):
        return None
    claim_scope = {
        str(actor) for actor in claim.get("scoped_actors") or [] if str(actor)
    }
    if claim_scope and not claim_scope.issubset(set(occurrence_scoped_actors)):
        return None
    return {"claim_id": claim_id, "fingerprint": fingerprint}


def _complete_message_support_spans(
    *,
    proposals: list[object],
    user_text: str,
    assistant_text: str,
    session_id: str,
    branch_id: str,
    turn: int,
    character_actor_id: str,
    persona_actor_id: str,
    response_occurrence_id: str,
    character_display_name: str,
    continuity_state: Mapping[str, Any] | None,
    claim_records: object,
) -> dict[str, set[tuple[int, int]]]:
    """Classify exact action, typed-support, and Claim nodes before scope admission."""
    from .semantic_fabric import (
        recognize_chat_social_frame,
        recognize_chat_social_subclaim,
    )

    supports: dict[str, set[tuple[int, int]]] = {
        "user_text": set(),
        "assistant_text": set(),
    }
    source_contract = {
        "user_text": (
            user_text,
            persona_actor_id,
            character_actor_id,
            "",
        ),
        "assistant_text": (
            assistant_text,
            character_actor_id,
            persona_actor_id,
            character_display_name,
        ),
    }
    for raw in proposals:
        if not isinstance(raw, Mapping):
            continue
        span = raw.get("source_span")
        if not isinstance(span, Mapping):
            continue
        source_name = str(span.get("source") or "")
        contract = source_contract.get(source_name)
        if contract is None:
            continue
        text, expected_subject, counterpart, speaker_label = contract
        start, end = span.get("start"), span.get("end")
        action = str(raw.get("action_code") or "")
        if isinstance(start, bool) or not isinstance(start, int) \
                or isinstance(end, bool) or not isinstance(end, int) \
                or not 0 <= start < end <= len(text) \
                or raw.get("subject_actor_id") != expected_subject \
                or action not in OCCURRENCE_ACTIONS | SOCIAL_SPEECH_ACTS \
                or raw.get("polarity") != "positive" \
                or raw.get("modality") != "actual":
            continue
        try:
            recognized = recognize_chat_social_frame(
                text,
                action,
                start=start,
                end=end,
                source_role=source_name,
                expected_speaker_label=speaker_label,
                require_actual_context=False,
            )
            participants = _code_owned_social_participants(
                recognized or {},
                raw.get("participants"),
                counterpart_actor_id=counterpart,
                continuity_state=continuity_state,
            )
        except (TypeError, ValueError):
            continue
        if recognized is None or participants is None:
            continue
        supports[source_name].add((start, end))
        for raw_key, kind, statuses in (
            (
                "voluntariness",
                "voluntariness",
                {"voluntary", "coerced", "assaulted"},
            ),
            ("consent", "consent", {"granted", "refused"}),
            ("disclosure", "disclosure", {"timely", "late"}),
        ):
            values = raw.get(raw_key, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, Mapping) \
                        or value.get("status") not in statuses \
                        or value.get("participant") != {
                            "kind": "actor",
                            "actor_id": expected_subject,
                        }:
                    continue
                evidence_span = value.get("source_span")
                if not isinstance(evidence_span, Mapping) \
                        or evidence_span.get("source") != source_name:
                    continue
                support_start = evidence_span.get("start")
                support_end = evidence_span.get("end")
                if isinstance(support_start, bool) \
                        or not isinstance(support_start, int) \
                        or isinstance(support_end, bool) \
                        or not isinstance(support_end, int) \
                        or not 0 <= support_start < support_end <= len(text):
                    continue
                if recognize_chat_social_subclaim(
                    text[support_start:support_end],
                    kind=kind,
                    status=value.get("status"),
                ):
                    supports[source_name].add((support_start, support_end))

    if not isinstance(claim_records, list):
        return supports
    allowed_branches = {branch_id}
    scope = (
        continuity_state.get("knowledge_record_scope")
        if isinstance(continuity_state, Mapping)
        else None
    )
    if isinstance(scope, Mapping) \
            and scope.get("branch_id") == branch_id \
            and scope.get("session_id") == session_id:
        allowed_branches.update(
            str(value)
            for value in scope.get("source_branch_ids") or ()
            if str(value)
        )
    from .claim_frame import (
        validate_claim_frame_against_source,
        validate_claim_record,
    )

    for raw_claim in claim_records:
        if not isinstance(raw_claim, Mapping):
            continue
        try:
            claim = validate_claim_record(raw_claim)
            lifecycle = str(claim.get("lifecycle_source") or "")
            source_name = (
                "user_text"
                if lifecycle == "user_text"
                else "assistant_text"
                if lifecycle == "assistant_response"
                else ""
            )
            if not source_name \
                    or str(claim.get("branch_id") or "") not in allowed_branches \
                    or int(claim.get("turn", -1)) != turn \
                    or (
                        lifecycle == "assistant_response"
                        and claim.get("response_occurrence_id")
                        != response_occurrence_id
                    ):
                continue
            text = source_contract[source_name][0]
            frame = validate_claim_frame_against_source(
                claim.get("frame"),
                text,
            )
            governor = frame.get("governor_span")
            if isinstance(governor, Mapping):
                support_start = governor.get("start")
                support_end = governor.get("end")
                if isinstance(support_start, int) \
                        and not isinstance(support_start, bool) \
                        and isinstance(support_end, int) \
                        and not isinstance(support_end, bool) \
                        and 0 <= support_start < support_end <= len(text):
                    supports[source_name].add((support_start, support_end))
        except (TypeError, ValueError):
            continue
    return supports


def seal_accepted_chat_proposals(
    *,
    user_text: str,
    assistant_text: str,
    proposals: object,
    session_id: str,
    branch_id: str,
    turn_index: int,
    character_actor_id: str,
    persona_actor_id: str,
    response_occurrence_id: str,
    continuity_state: Mapping[str, Any] | None = None,
    character_display_name: str = "",
    claim_records: object = None,
) -> dict[str, list[dict]]:
    """Seal conservative social self-actions to one accepted Chat response.

    The caller receives lifecycle partitions, never authority-bearing model JSON.
    Unsupported, hypothetical, negative, third-party, or span-inexact rows abstain.
    """
    _identifier(session_id, field="session_id")
    _identifier(branch_id, field="branch_id")
    turn = _turn(turn_index, field="turn_index")
    character = _identifier(character_actor_id, field="character_actor_id")
    persona = _identifier(persona_actor_id, field="persona_actor_id")
    if re.fullmatch(r"response:[0-9a-f]{64}", str(response_occurrence_id or "")) is None:
        raise ValueError("social proposal requires one exact accepted response occurrence")
    if not isinstance(user_text, str) or not isinstance(assistant_text, str):
        raise ValueError("accepted Chat source texts must be strings")
    if not isinstance(proposals, list) or len(proposals) > 64:
        raise ValueError("social proposals must be a bounded list")

    sealed: dict[str, list[dict]] = {
        "user_text": [],
        "assistant_response": [],
        "deferred_extraction": [],
    }
    complete_supports = _complete_message_support_spans(
        proposals=proposals,
        user_text=user_text,
        assistant_text=assistant_text,
        session_id=session_id,
        branch_id=branch_id,
        turn=turn,
        character_actor_id=character,
        persona_actor_id=persona,
        response_occurrence_id=response_occurrence_id,
        character_display_name=character_display_name,
        continuity_state=continuity_state,
        claim_records=claim_records,
    )
    for raw in proposals:
        if not isinstance(raw, Mapping):
            continue
        span = raw.get("source_span")
        if not isinstance(span, Mapping):
            continue
        source_name = str(span.get("source") or "")
        if source_name == "assistant_text":
            text = assistant_text
            lifecycle = "assistant_response"
            expected_subject = character
            counterpart = persona
            admission_source = "accepted_response"
        elif source_name == "user_text":
            text = user_text
            lifecycle = "user_text"
            expected_subject = persona
            counterpart = character
            admission_source = "user_text"
        else:
            continue
        start, end = span.get("start"), span.get("end")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0 \
                or isinstance(end, bool) or not isinstance(end, int) \
                or end <= start or end > len(text):
            continue
        evidence_text = text[start:end]
        subject = str(raw.get("subject_actor_id") or "")
        action = str(raw.get("action_code") or "")
        if subject != expected_subject \
                or action not in OCCURRENCE_ACTIONS | SOCIAL_SPEECH_ACTS \
                or raw.get("polarity") != "positive" \
                or raw.get("modality") != "actual":
            continue

        try:
            from .semantic_fabric import recognize_chat_social_frame

            recognized = recognize_chat_social_frame(
                text,
                action,
                start=start,
                end=end,
                source_role=source_name,
                expected_speaker_label=(
                    character_display_name
                    if source_name == "assistant_text"
                    else ""
                ),
            )
        except Exception:
            recognized = None
        if recognized is None \
                or recognized["action_code"] != action \
                or recognized["polarity"] != raw.get("polarity") \
                or recognized["modality"] != raw.get("modality"):
            continue
        segment = str(recognized["source_segment"])

        participants_value = raw.get("participants")
        try:
            participants = _code_owned_social_participants(
                recognized,
                participants_value,
                counterpart_actor_id=counterpart,
                continuity_state=continuity_state,
            )
            if participants is None:
                continue
            visibility, scoped = _proposal_scope(
                source_segment=segment,
                subject_actor_id=subject,
                counterpart_actor_id=counterpart,
                participants=participants,
            )
        except ValueError:
            continue
        subclaims = _seal_social_subclaims(
            raw=raw,
            source_name=source_name,
            text=text,
            parent_span=(start, end),
            action=action,
            subject_actor_id=subject,
            context_frame=recognized,
        )
        if subclaims is None:
            continue
        voluntariness, consent, disclosures = subclaims
        try:
            from .semantic_occurrence import chat_context_allows_actual_span

            if not chat_context_allows_actual_span(
                recognized["context_graph"],
                text,
                start,
                end,
                content_start=int(
                    recognized.get("context_content_start") or 0
                ),
                expected_speaker_label=str(
                    recognized.get("expected_speaker_label") or ""
                ),
                complete_support_spans=complete_supports[source_name],
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if not voluntariness:
            voluntariness = [{
                "participant": {"kind": "actor", "actor_id": subject},
                "status": "unknown",
                "evidence": None,
            }]
        material = {
            "schema": "aetherstate-auto-social-occurrence-key/1",
            "session_id": session_id,
            "branch_id": branch_id,
            "turn_index": turn,
            "response_occurrence_id": (
                response_occurrence_id
                if lifecycle == "assistant_response"
                else ""
            ),
            "source_message_fingerprint": _accepted_message_fingerprint(text),
            "lifecycle": lifecycle,
            "action": action,
            "source_span": [start, end],
            "subject_actor_id": subject,
        }
        occurrence_id = "occurrence.auto." + hashlib.sha256(
            _canonical(material)
        ).hexdigest()
        motive_ref = _verified_motive_claim_ref(
            raw.get("motive_claim_ref"),
            claim_records=claim_records,
            session_id=session_id,
            branch_id=branch_id,
            turn=turn,
            lifecycle_source=lifecycle,
            response_occurrence_id=(
                response_occurrence_id
                if lifecycle == "assistant_response"
                else ""
            ),
            subject_actor_id=subject,
            player_actor_id=persona,
            occurrence_scoped_actors=scoped,
            action=action,
            recognized=recognized,
            source_text=text,
            continuity_state=continuity_state,
        )
        record = {
            "schema": OCCURRENCE_SCHEMA,
            "occurrence_id": occurrence_id,
            "revision": 1,
            "action": "admit",
            "occurred_turn": turn,
            "act": action,
            "agreement_actor": {"kind": "actor", "actor_id": subject},
            "outside_participants": participants,
            "voluntariness": voluntariness,
            "consent": consent,
            "disclosures": disclosures,
            "motive_claim_ref": motive_ref,
            "summary": evidence_text.strip(),
            "admission_evidence": _admission_evidence(
                source=admission_source,
                text=text,
                start=start,
                end=end,
            ),
            "source_segment": segment,
            "visibility": visibility,
            "scoped_actors": scoped,
        }
        try:
            validate_social_occurrence(record)
        except ValueError:
            continue
        cause_record = deepcopy(record)
        cause_record["lifecycle_source"] = lifecycle
        cause_record["response_occurrence_id"] = (
            response_occurrence_id
            if lifecycle == "assistant_response"
            else ""
        )
        try:
            cause_fingerprint = _record_fingerprint(
                validate_social_occurrence(cause_record),
            )
        except ValueError:
            continue
        group_id = "social-group:" + hashlib.sha256(
            _canonical({
                "schema": "aetherstate-atomic-social-group/1",
                "occurrence_id": occurrence_id,
                "lifecycle": lifecycle,
            })
        ).hexdigest()
        group_start = len(sealed[lifecycle])
        sealed[lifecycle].append({
            "op": "social_occurrence_admit",
            "record": record,
            "_social_group_id": group_id,
        })
        participant_rows = [
            {"kind": "actor", "actor_id": subject},
            deepcopy(participants[0]),
        ]
        target_actor_id = str(participants[0].get("actor_id") or "")
        if action == "promise_make":
            thread_material = {
                "schema": "aetherstate-auto-promise-thread-key/1",
                "session_id": session_id,
                "branch_id": branch_id,
                "occurrence_id": occurrence_id,
                "subject": subject,
                "counterpart": target_actor_id,
                "summary": evidence_text.strip().casefold(),
            }
            thread_record = {
                "schema": THREAD_TRANSITION_SCHEMA,
                "thread_id": "thread.auto." + hashlib.sha256(
                    _canonical(thread_material)
                ).hexdigest(),
                "revision": 1,
                "action": "create",
                "kind": "promise",
                "summary": evidence_text.strip(),
                "participants": participant_rows,
                "promisor_actor_id": subject,
                "promisee_actor_id": target_actor_id,
                "status": "open",
            }
            promise_terms = recognized.get("promise_terms")
            if isinstance(promise_terms, Mapping):
                thread_record["promise_terms"] = deepcopy(dict(promise_terms))
            sealed[lifecycle].append({
                "op": "continuity_thread_transition",
                "record": thread_record,
                "_social_group_id": group_id,
            })
        elif action in {"promise_fulfill", "promise_violate"} \
                and isinstance(continuity_state, Mapping):
            event_terms = recognized.get("promise_event_terms")
            candidates = []
            if isinstance(event_terms, Mapping):
                for thread_id, revisions in (
                    continuity_state.get("continuity_threads") or {}
                ).items():
                    if not isinstance(revisions, list) or not revisions:
                        continue
                    latest = revisions[-1]
                    if not isinstance(latest, Mapping) \
                            or latest.get("kind") != "promise" \
                            or latest.get("status") != "open" \
                            or str(latest.get("promisor_actor_id") or "") != subject:
                        continue
                    terms = latest.get("promise_terms")
                    wanted_polarity = (
                        "perform"
                        if action == "promise_fulfill"
                        else "refrain"
                    )
                    if not isinstance(terms, Mapping) \
                            or terms.get("polarity") != wanted_polarity \
                            or terms.get("predicate") != event_terms.get("predicate"):
                        continue
                    candidates.append((
                        str(thread_id),
                        latest,
                        len(revisions) + 1,
                    ))
            if len(candidates) == 1:
                thread_id, latest, revision = candidates[0]
                status = (
                    "fulfilled"
                    if action == "promise_fulfill"
                    else "violated"
                )
                sealed[lifecycle].append({
                    "op": "continuity_thread_transition",
                    "record": {
                        "schema": THREAD_TRANSITION_SCHEMA,
                        "thread_id": thread_id,
                        "revision": revision,
                        "action": "resolve",
                        "kind": "promise",
                        "summary": evidence_text.strip(),
                        "participants": deepcopy(latest["participants"]),
                        "promisor_actor_id": latest["promisor_actor_id"],
                        "promisee_actor_id": latest["promisee_actor_id"],
                        "promise_terms": deepcopy(latest["promise_terms"]),
                        "status": status,
                        "supersedes_fingerprint": latest.get("fingerprint"),
                        "cause_ref": {
                            "kind": "social_occurrence",
                            "fingerprint": cause_fingerprint,
                        },
                    },
                    "_social_group_id": group_id,
                })
        elif action in {
            "promise_withdraw",
            "promise_release",
            "thread_resolve",
        } and isinstance(continuity_state, Mapping):
            candidates = []
            for thread_id, revisions in (
                continuity_state.get("continuity_threads") or {}
            ).items():
                if not isinstance(revisions, list) or not revisions:
                    continue
                latest = revisions[-1]
                if not isinstance(latest, Mapping) \
                        or latest.get("kind") != "promise" \
                        or latest.get("status") != "open":
                    continue
                promisor = str(latest.get("promisor_actor_id") or "")
                promisee = str(latest.get("promisee_actor_id") or "")
                if action == "promise_withdraw" \
                        and (subject, target_actor_id) != (promisor, promisee):
                    continue
                if action == "promise_release" \
                        and (subject, target_actor_id) != (promisee, promisor):
                    continue
                if action == "thread_resolve" \
                        and (subject, target_actor_id) != (promisor, promisee):
                    continue
                candidates.append((str(thread_id), latest, len(revisions) + 1))
            if len(candidates) == 1:
                thread_id, latest, revision = candidates[0]
                resolved = action in {"promise_release", "thread_resolve"}
                sealed[lifecycle].append({
                    "op": "continuity_thread_transition",
                    "record": {
                        "schema": THREAD_TRANSITION_SCHEMA,
                        "thread_id": thread_id,
                        "revision": revision,
                        "action": "resolve" if resolved else "abandon",
                        "kind": "promise",
                        "summary": evidence_text.strip(),
                        "participants": deepcopy(latest["participants"]),
                        "promisor_actor_id": latest["promisor_actor_id"],
                        "promisee_actor_id": latest["promisee_actor_id"],
                        "status": "resolved" if resolved else "abandoned",
                        "supersedes_fingerprint": latest.get("fingerprint"),
                        **(
                            {
                                "promise_terms": deepcopy(
                                    latest["promise_terms"],
                                ),
                            }
                            if isinstance(
                                latest.get("promise_terms"),
                                Mapping,
                            )
                            else {}
                        ),
                        "cause_ref": {
                            "kind": "social_occurrence",
                            "fingerprint": cause_fingerprint,
                        },
                    },
                    "_social_group_id": group_id,
                })
        elif action.startswith("agreement_"):
            agreement_op = _automatic_agreement_transition(
                action=action,
                recognized=recognized,
                text=text,
                start=start,
                end=end,
                session_id=session_id,
                branch_id=branch_id,
                turn=turn,
                subject_actor_id=subject,
                counterpart_actor_id=target_actor_id,
                lifecycle_source=lifecycle,
                response_occurrence_id=(
                    response_occurrence_id
                    if lifecycle == "assistant_response"
                    else ""
                ),
                continuity_state=continuity_state,
            )
            if agreement_op is not None:
                agreement_op["_social_group_id"] = group_id
                sealed[lifecycle].append(agreement_op)
        elif action == "disclosure":
            statement = str(
                recognized.get("disclosure_statement") or ""
            ).strip()
            if statement:
                sealed[lifecycle].append({
                    "op": "belief_acquire",
                    "holder": target_actor_id,
                    "statement": statement,
                    "stance": "was_told",
                    "source": "disclosed",
                    "teller": subject,
                    "visibility": "actor_scoped",
                    "scoped_actors": [target_actor_id],
                    "source_occurrence_id": occurrence_id,
                    "evidence_ref": {
                        "kind": "accepted_chat_disclosure",
                        "fingerprint": record["admission_evidence"][
                            "fingerprint"
                        ],
                    },
                    "_social_group_id": group_id,
                })
        if action in SOCIAL_SPEECH_ACTS \
                and len(sealed[lifecycle]) == group_start + 1:
            # A typed speech act without its required durable transition is not
            # a partial occurrence. The complete proposal abstains.
            sealed[lifecycle].pop()
    return sealed
