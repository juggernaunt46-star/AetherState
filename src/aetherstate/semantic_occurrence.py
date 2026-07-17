"""Deterministic source-bounded occurrence construction for semantic interpretation.

The graph is recognition/construction evidence only.  It never authorizes a mechanic and it never
copies a role from one occurrence to another.  Every field binding must cite a source span wholly
inside the node that owns it; missing evidence stays explicit instead of being borrowed from a
neighboring clause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .capability_glossary import content_fingerprint


OCCURRENCE_GRAPH_SCHEMA = "semantic-occurrence-graph/1"
OCCURRENCE_AUTHORITY_SCHEMA = "semantic-issuer-authority/1"

_ANCHOR_KINDS = frozenset({"actor", "target", "capability", "action"})
_POLARITIES = frozenset({"affirmative", "negated", "unknown"})
_ACTUALITIES = frozenset({"actual", "quoted", "hypothetical", "metaphorical", "unknown"})
_RELATIONS = frozenset({"sequence", "coordination", "contrast", "overlap"})
_STABLE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(r"[a-z][a-z0-9._-]*(?:/[a-z0-9._-]+)*\Z")
_STRUCTURED_COMMAND_RE = re.compile(
    r"\(\(\s*(?:aether\.[a-z_]+[^)]*?|roll\s+[^)]*?)\s*\)\)",
    re.IGNORECASE,
)
_LEGACY_QUOTE_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|“[^”]*”|‘[^’]*’|(?<!\w)\'[^\'\r\n]+\'(?!\w)',
    re.DOTALL,
)
_QUOTE_RE = re.compile(
    # Code samples and Markdown quotations are quoted language too.  They cannot become a
    # performed action merely because they use a different presentation delimiter than dialogue.
    r"```.*?```|~~~.*?~~~|`[^`\r\n]+`|"
    r"(?m:^[ \t]*>[^\r\n]*(?:\r?\n[ \t]*>[^\r\n]*)*)|"
    r'"(?:\\.|[^"\\])*"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|'
    r"(?<!\w)'[^'\r\n]+'(?!\w)",
    re.DOTALL,
)
_HARD_BOUNDARY_RE = re.compile(
    r"[.!?;\n]+|"
    # End on ``then`` itself. A following structured command is length-masked to spaces; a
    # greedy trailing whitespace match would otherwise swallow that command's source span and
    # strand its capability anchor outside the renewed occurrence.
    r",\s*(?:and\s+)?then\b|\band\s+then\b|"
    r"(?:,\s*)?\band\s+now\s+(?=(?:i|we)\b)|"
    r"(?:,\s*)?\b(?:but|yet|whereas)\b|"
    r"(?:,\s*)?\band\s+(?=(?:i|we)\b)|"
    r"(?:,\s*)?\b(?:while|as)\s+(?=(?:i|we|he|she|they|it)\b|"
    r"(?-i:[A-Z])[a-z0-9'-]*\s+)",
    re.IGNORECASE,
)
_COORDINATION_RE = re.compile(r"(?:,\s*)?\b(?:and|then|but|yet|whereas)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(
    r"(?:\b(?:do(?:es)?|did|will|would|can|could|should|may|might|must|"
    r"am|is|are|was|were)\s+not\b|"
    r"\b(?:don't|doesn't|didn't|won't|wouldn't|can't|couldn't|shouldn't|mustn't|"
    r"isn't|aren't|wasn't|weren't)\b|"
    r"\b(?:choose|chooses|chose|chosen|choosing|decide|decides|decided|deciding|"
    r"try|tries|tried|trying)\s+not\s+to\b|"
    r"\bnot(?:\s+to)?\s*$|"
    r"\bnever\b)[^.!?;\n]*$|"
    # Negative governors bind the immediately following event.  Keeping this anchored at the
    # semantic event boundary avoids treating an unrelated earlier refusal as global negation.
    r"\b(?:refus(?:e|es|ed|ing)|declin(?:e|es|ed|ing))\s+to\b[^.!?;\n]*$|"
    r"\b(?:avoid(?:s|ed|ing)?|skip(?:s|ped|ping)?|forgo(?:es|ne|ing)?|"
    r"refrain(?:s|ed|ing)?\s+from)\b[^.!?;\n]*$|"
    r"\b(?:without|except|rather\s+than|instead\s+of)\b[^.!?;\n]*$|"
    r"\b(?:plan|plans|planned|planning|intend|intends|intended|intending)\s+not\s+to\b"
    r"[^.!?;\n]*$|"
    # Inability and a failed attempt are explicit evidence that the governed event did not occur.
    # These are kept event-local by the end anchor, just like the negative governors above.
    r"\b(?:cannot|(?:am|is|are|was|were)\s+unable\s+to|"
    r"fail(?:s|ed|ing)?\s+to|far\s+from)\b[^.!?;\n]*$|"
    r"^\s*neither\b[^.!?;\n]*$",
    re.IGNORECASE,
)
_EXPLICIT_NEGATOR_RE = re.compile(
    r"\b(?:not|never)\b|"
    r"\b(?:don't|doesn't|didn't|won't|wouldn't|can't|couldn't|shouldn't|mustn't|"
    r"isn't|aren't|wasn't|weren't)\b|"
    r"\b(?:cannot|unable|fail(?:s|ed|ing)?|refus(?:e|es|ed|ing)|"
    r"declin(?:e|es|ed|ing)|avoid(?:s|ed|ing)?|neither)\b",
    re.IGNORECASE,
)
_ADDITIVE_NOT_RE = re.compile(
    # Mask the additive operator wherever it appears in this one event-local prefix.  The
    # embedding classifier separately keeps ``not merely considering [action]`` non-performing,
    # while this broader mask also handles a structured declaration whose companion action is
    # the only visible natural-language event.
    r"\bnot\s+(?:only|merely|just|simply)\b",
    re.IGNORECASE,
)
_CANNOT_HELP_BUT_RE = re.compile(r"\bcannot\s+help\s+but\b", re.IGNORECASE)
_NEITHER_TARGET_SCOPE_RE = re.compile(
    r"^\s*(?:(?:at|on|onto|against|into|upon|through|toward|towards|to|of)\s+)?"
    r"neither\b[^.!?;\n]*\bnor\b",
    re.IGNORECASE,
)
_TARGET_COORDINATION_BRIDGE_RE = re.compile(
    r"""
    ^\s*(?:
        (?P<comma>,)\s*
        |
        ,?\s*(?:
            (?P<ambiguous>
                and\s+then
                |or\s+else
                |along\s+with
                |together\s+with
                |alongside
            )
            |(?P<strong>
                &
                |and\s*/\s*or
                |and\s*-\s*or
                |and(?:\s+also)?
                |or
                |nor
                |but\s+also
            )
            |(?P<guarded>
                but
                |as\s+well\s+as
                |in\s+addition\s+to
                |plus
                |\+
                |also
                |additionally
            )
        )\s+(?:(?:the|a|an)\s+)?
    )\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def target_coordination_bridge_kind(value: str) -> str | None:
    """Classify the exact bridge joining two possible grammatical patient slots.

    ``strong`` connectors ordinarily join nominal targets. ``guarded`` connectors require a
    closed right-hand nominal arm. ``ambiguous`` and comma bridges preserve a possible second arm
    only as unresolved evidence unless a caller has stronger grammar. This distinction prevents
    accompaniment, event sequencing, and comma-spliced subjects from being asserted as patients.
    """
    match = _TARGET_COORDINATION_BRIDGE_RE.fullmatch(str(value or ""))
    if match is None:
        return None
    for kind in ("strong", "guarded", "ambiguous", "comma"):
        if match.group(kind) is not None:
            return kind
    return None


def is_target_coordination_bridge(value: str) -> bool:
    """Return whether exact source text joins two grammatical patient slots.

    This is the one coordination grammar shared by target-anchor construction and occurrence
    cardinality. It accepts additive/list connectors, including a safe comma-only list bridge,
    but deliberately excludes contrast/exclusion forms such as ``but not``, ``rather than``,
    ``except``, and ``instead of``.
    """
    return target_coordination_bridge_kind(value) is not None
_HYPOTHETICAL_RE = re.compile(
    r"^\s*(?:if|unless|suppose|supposing|assuming|imagine|imagining|"
    r"hypothetically|maybe|perhaps|in\s+case)\b|"
    r"^\s*(?:were|had|should)\s+(?:i|we)\b|"
    r"^\s*(?:(?:i|we)\s+|let(?:'s|\s+us)\s+)?"
    r"(?:imagine|suppose|assume|pretend|consider)\b|"
    r"\b(?:would|could|might)\b",
    re.IGNORECASE,
)
_METAPHOR_RE = re.compile(
    r"\b(?:metaphorically|figuratively|symbolically|as\s+(?:a\s+)?metaphor|"
    r"in\s+(?:a\s+)?metaphor(?:ical(?:\s+(?:sense|terms))?)?|"
    r"as\s+(?:a\s+)?figure\s+of\s+speech)\b",
    re.IGNORECASE,
)
_REPORTED_SPEECH_RE = re.compile(
    r"^\s*(?:"
    # Strong surface quotation cues only.  Broader testimony, memory, belief, and speech
    # embeddings keep their richer semantic-binding classification (recognition_only) instead of
    # being flattened into a quote ambiguity here.
    r"(?:i|we)\s+(?:(?:quote|quoted|repeat|repeated|write|wrote)\s*(?::|"
    r"\b(?:that|if|whether)\b|\b(?:i|we|he|she|they|it)\b)|"
    r"(?:say|said|claim|claimed|report|reported|mention|mentioned)\s*:)|"
    r"(?:the\s+)?(?:phrase|sentence|words?)\b)",
    re.IGNORECASE,
)
_WORD_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?", re.IGNORECASE)

# These heads do not authorize the later semantic anchor in the same exact occurrence.  The
# classifier searches only the clause-local prefix ending at that anchor, so arbitrary ordinary
# modifiers cannot hide the governor, while a direct performative remains actual because its own
# head is the anchor and therefore is not present in its prefix.
_REPRESENTATION_GOVERNORS = frozenset({
    "acknowledge", "acknowledged", "acknowledges", "acknowledging",
    "admit", "admits", "admitted", "admitting",
    "announce", "announced", "announces", "announcing",
    "believe", "believed", "believes", "believing",
    "claim", "claimed", "claims", "claiming",
    "confess", "confessed", "confesses", "confessing",
    "consider", "considered", "considers", "considering",
    "declare", "declared", "declares", "declaring",
    "deny", "denied", "denies", "denying",
    "depict", "depicted", "depicting", "depicts",
    "describe", "described", "describes", "describing",
    "discuss", "discussed", "discusses", "discussing",
    "dream", "dreamed", "dreaming", "dreams", "dreamt",
    "explain", "explained", "explaining", "explains",
    "forget", "forgetting", "forgets", "forgot", "forgotten",
    "imagine", "imagined", "imagines", "imagining",
    "know", "knew", "knowing", "known", "knows",
    "mention", "mentioned", "mentioning", "mentions",
    "mime", "mimed", "mimes", "miming",
    "narrate", "narrated", "narrates", "narrating",
    "observe", "observed", "observes", "observing",
    "picture", "pictured", "pictures", "picturing",
    "portray", "portrayed", "portraying", "portrays",
    "pretend", "pretended", "pretending", "pretends",
    "quote", "quoted", "quotes", "quoting",
    "recall", "recalled", "recalling", "recalls",
    "recollect", "recollected", "recollecting", "recollects",
    "recount", "recounted", "recounting", "recounts",
    "relive", "relived", "relives", "reliving",
    "remember", "remembered", "remembering", "remembers",
    "represent", "represented", "representing", "represents",
    "repeat", "repeated", "repeating", "repeats",
    "report", "reported", "reporting", "reports",
    "roleplay", "roleplayed", "roleplaying", "roleplays",
    "say", "said", "saying", "says",
    "suppose", "supposed", "supposes", "supposing",
    "suspect", "suspected", "suspecting", "suspects",
    "simulate", "simulated", "simulates", "simulating",
    "think", "thinking", "thinks", "thought",
    "write", "writes", "writing", "written", "wrote",
})
_DIRECTIVE_GOVERNORS = frozenset({
    "advise", "advised", "advises", "advising",
    "allow", "allowed", "allowing", "allows",
    "ask", "asked", "asking", "asks",
    "cause", "caused", "causes", "causing",
    "command", "commanded", "commanding", "commands",
    "compel", "compelled", "compelling", "compels",
    "encourage", "encouraged", "encourages", "encouraging",
    "forbade", "forbid", "forbidden", "forbidding", "forbids",
    "force", "forced", "forces", "forcing",
    "get", "gets", "getting", "got", "gotten",
    "instruct", "instructed", "instructing", "instructs",
    "let", "lets", "letting",
    "make", "made", "makes", "making",
    "order", "ordered", "ordering", "orders",
    "permit", "permits", "permitted", "permitting",
    "require", "required", "requires", "requiring",
    "request", "requested", "requesting", "requests",
    "suggest", "suggested", "suggesting", "suggests",
    "tell", "telling", "tells", "told",
    "urge", "urged", "urges", "urging",
    "warn", "warned", "warning", "warns",
})
_COMMITMENT_GOVERNORS = frozenset({
    "aim", "aimed", "aiming", "aims",
    "expect", "expected", "expecting", "expects",
    "fix", "fixed", "fixes", "fixing",
    "guarantee", "guaranteed", "guaranteeing", "guarantees",
    "hope", "hoped", "hopes", "hoping",
    "intend", "intended", "intending", "intends",
    "mean", "meaning", "means", "meant",
    "need", "needed", "needing", "needs",
    "plan", "planned", "planning", "plans",
    "pledge", "pledged", "pledges", "pledging",
    "prepare", "prepared", "prepares", "preparing",
    "promise", "promised", "promises", "promising",
    "swear", "swearing", "swears", "swore", "sworn",
    "schedule", "scheduled", "schedules", "scheduling",
    "threaten", "threatened", "threatening", "threatens",
    "vow", "vowed", "vowing", "vows",
    "want", "wanted", "wanting", "wants",
    "wish", "wished", "wishes", "wishing",
})
_NONPERFORMING_NOUNS = frozenset({
    "account", "belief", "command", "description", "discussion", "dream",
    "example", "fiction", "hope", "instruction", "intention", "memory", "mention",
    "oath", "order", "plan", "pledge", "promise", "recollection", "report",
    "request", "scenario", "story", "suggestion", "thought", "threat", "vow",
    "warning", "wish", "word",
})
_INABILITY_GOVERNORS = frozenset({
    "almost", "cannot", "fail", "failed", "failing", "fails", "incapable", "nearly",
    "prevented", "unable",
})
_MODAL_AUXILIARIES = frozenset({
    "can", "could", "may", "might", "must", "ought", "should", "would",
})
_MODAL_MODIFIERS = frozenset({
    "alone", "certainly", "definitely", "even", "just", "maybe", "now", "only",
    "perhaps", "possibly", "probably", "really", "simply", "still",
})
_EVENT_SUBJECTS = frozenset({"i", "we"})
_FUTURE_SCOPE_WORDS = frozenset({
    "after", "before", "dawn", "dusk", "eventually", "hour", "later", "midnight",
    "next", "noon", "once", "someday", "soon", "sunrise", "sunset", "tomorrow",
    "tonight", "when",
})
_PAST_TIME_RE = re.compile(
    r"\b(?:yesterday|earlier|previously|formerly|heretofore)\b|"
    r"\blast\s+(?:night|week|month|year|turn|round|hour|time)\b|"
    r"\b(?:moments?|minutes?|hours?|days?|weeks?|months?|years?|turns?|rounds?)\s+ago\b|"
    # A trailing ``before`` is prior-time evidence. ``before it can pounce`` is instead the
    # current action's bounded completion window and must not retroactively make that action past.
    r"\bbefore\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_COMPLETED_QUESTION_EMBEDDING_RE = re.compile(
    r"\b(?:ask|asks|asked|asking|wonder|wonders|wondered|wondering)\b"
    r"[^.!?;\n]*\b(?:who|what|which|where|when|why|how|whether)\b",
    re.IGNORECASE,
)
# These forms are present-compatible even though their spelling ends in ``ed``. They need an
# independent past auxiliary or time marker before the occurrence may be classified as past.
_PRESENT_COMPATIBLE_ED = frozenset({
    "bleed", "breed", "embed", "exceed", "feed", "heed", "need", "proceed", "seed",
    "shed", "shred", "speed", "succeed",
})
# Intentionally excludes spelling-identical present/past verbs such as cast, cut, hit, hurt, let,
# put, read, set, and spread. Those remain current-capable unless separate past evidence exists.
_UNAMBIGUOUS_IRREGULAR_PAST = frozenset({
    "bled", "blew", "broke", "brought", "built", "bought", "caught", "chose", "came",
    "did", "died", "drew", "drank", "drove", "dug", "fed", "fell", "fled", "flew",
    "fought", "forgot", "found", "froze", "gave", "went", "heard", "held", "hid",
    "kept", "knew", "laid", "led", "left", "lit", "lost", "made", "met", "paid", "ran",
    "rode", "said", "sang", "sank", "sat", "saw", "sent", "shook", "shot", "slept",
    "slew", "slid", "slung", "spoke", "spent", "spun", "stood", "stole", "struck",
    "swam", "swept", "swung", "taught", "thought", "threw", "tied", "told", "took",
    "tore", "used", "woke", "wore", "won", "wrote",
})
_PAST_PREDICATE_FILLERS = frozenset({
    "again", "already", "also", "and", "even", "just", "now", "once", "only",
    "quite", "rather", "really", "simply", "still", "then", "very",
})


class OccurrenceGraphError(ValueError):
    """Raised when occurrence evidence is malformed or crosses a node boundary."""


def _authority_decision(
    issuer: str,
    channel: str,
    lifecycle_phase: str,
    grammar_version: str,
    operation_family: str,
) -> tuple[bool, str]:
    """Return the narrow directional authority for Player semantic construction."""
    expected_channel = {
        "player": "player_input",
        "narrator": "narrator_reply",
        "genesis": "genesis_batch",
        "extraction": "extraction_proposal",
        "rule": "rule_internal",
    }.get(issuer)
    if expected_channel is None or channel != expected_channel:
        return False, "issuer_channel_mismatch"
    if lifecycle_phase != "new_action":
        return False, "phase_not_allowed"
    if grammar_version != "tier0-semantic/1":
        return False, "grammar_not_allowed"
    if operation_family != "semantic_interpretation":
        return False, "operation_family_not_allowed"
    if issuer != "player" or channel != "player_input":
        return False, "producer_not_allowed"
    return True, "allowed"


def _authority_payload(
    *,
    issuer: str,
    channel: str,
    lifecycle_phase: str,
    grammar_version: str,
    operation_family: str,
) -> dict[str, Any]:
    for value, label in (
        (issuer, "issuer"), (channel, "channel"),
        (lifecycle_phase, "lifecycle phase"), (operation_family, "operation family"),
    ):
        _stable_id(value, f"occurrence authority {label}")
    if _VERSION_RE.fullmatch(grammar_version) is None:
        raise OccurrenceGraphError("occurrence authority grammar version is invalid")
    allowed, reason = _authority_decision(
        issuer, channel, lifecycle_phase, grammar_version, operation_family,
    )
    return {
        "schema": OCCURRENCE_AUTHORITY_SCHEMA,
        "issuer": issuer,
        "channel": channel,
        "lifecycle_phase": lifecycle_phase,
        "grammar_version": grammar_version,
        "operation_family": operation_family,
        "allowed": allowed,
        "reason": reason,
    }


@dataclass(frozen=True)
class OccurrenceAnchor:
    """One caller-proven field candidate with exact source provenance."""

    kind: str
    identity: str
    start: int
    end: int
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "span": [self.start, self.end],
            "source": self.source,
        }


def _stable_id(value: object, label: str) -> str:
    text = str(value or "")
    if _STABLE_ID_RE.fullmatch(text) is None:
        raise OccurrenceGraphError(f"{label} must be a stable identifier")
    return text


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and (text[start].isspace() or text[start] == ","):
        start += 1
    while end > start and (text[end - 1].isspace() or text[end - 1] == ","):
        end -= 1
    return start, end


def _canonical_anchors(
    anchors: Iterable[OccurrenceAnchor | Mapping[str, Any]],
    source_length: int,
) -> list[OccurrenceAnchor]:
    unique: dict[tuple[str, str, int, int, str], OccurrenceAnchor] = {}
    for raw in anchors:
        if isinstance(raw, OccurrenceAnchor):
            anchor = raw
        elif isinstance(raw, Mapping):
            span = raw.get("span")
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                raise OccurrenceGraphError("occurrence anchor span must contain two offsets")
            anchor = OccurrenceAnchor(
                kind=str(raw.get("kind") or ""),
                identity=str(raw.get("identity") or ""),
                start=span[0],
                end=span[1],
                source=str(raw.get("source") or ""),
            )
        else:
            raise OccurrenceGraphError("occurrence anchor must be typed evidence")
        if anchor.kind not in _ANCHOR_KINDS:
            raise OccurrenceGraphError("occurrence anchor kind is unsupported")
        _stable_id(anchor.identity, "occurrence anchor identity")
        _stable_id(anchor.source, "occurrence anchor source")
        if isinstance(anchor.start, bool) or isinstance(anchor.end, bool) \
                or not isinstance(anchor.start, int) or not isinstance(anchor.end, int) \
                or anchor.start < 0 or anchor.end <= anchor.start \
                or anchor.end > source_length:
            raise OccurrenceGraphError("occurrence anchor span is outside the source")
        key = (anchor.kind, anchor.identity, anchor.start, anchor.end, anchor.source)
        unique[key] = anchor
    return [unique[key] for key in sorted(unique, key=lambda row: (row[2], row[3], *row[:2], row[4]))]


def _coordination_cuts(
    source_text: str,
    detection_text: str,
    start: int,
    end: int,
    anchors: list[OccurrenceAnchor],
) -> list[tuple[int, int]]:
    """Return evidence-backed inner boundaries between distinct semantic anchors."""
    contained = [anchor for anchor in anchors if start <= anchor.start and anchor.end <= end]
    capabilities = sorted({(row.start, row.end, row.identity, row.source)
                           for row in contained if row.kind == "capability"})
    authored = [row for row in capabilities if row[3] != "candidate_inferred"]
    # One authored capability can legitimately contain several action verbs (a named maneuver
    # that sends, wrenches, then drives).  Split only between multiple authored capabilities, or
    # between inferred-only recognitions when no authored anchor governs the construction.
    pivots = authored if len(authored) >= 2 \
        else capabilities if not authored and len(capabilities) >= 2 else []
    cuts: list[tuple[int, int]] = []

    # Adjacent structured declarations are independently authored mechanics even when the
    # Player omits punctuation or a connector between them.  The final declaration may still
    # govern the natural-language action that follows it, so cut at the start of each later
    # command rather than separating every command from its companion prose.
    explicit_commands = [
        command
        for command in _STRUCTURED_COMMAND_RE.finditer(source_text, start, end)
        if any(
            anchor.source == "candidate_explicit"
            and command.start() <= anchor.start
            and anchor.end <= command.end()
            for anchor in contained
        )
    ]
    for left, right in zip(explicit_commands, explicit_commands[1:]):
        connectors = list(
            _COORDINATION_RE.finditer(detection_text, left.end(), right.start())
        )
        if connectors:
            connector = connectors[-1]
            cuts.append((connector.start(), connector.end()))
        else:
            cuts.append((right.start(), right.start()))

    for left, right in zip(pivots, pivots[1:]):
        if left[1] > right[0]:
            continue
        # Two source hits for the same inferred capability can be one coordinated realization
        # (``touch the shard and trace its echo``).  A repeated Player subject or a sequencing
        # word is already a hard boundary; plain coordination alone must not mint a second roll.
        # Explicit commands remain separate through the command-boundary rule above.
        if left[2] == right[2] and left[3] == right[3] \
                and left[3] != "candidate_explicit":
            continue
        connectors = list(
            _COORDINATION_RE.finditer(detection_text, left[1], right[0])
        )
        if connectors:
            connector = connectors[-1]
            cuts.append((connector.start(), connector.end()))
    return sorted(set(cuts))


def _outside_segments(
    text: str,
    detector: str,
    start: int,
    end: int,
    anchors: list[OccurrenceAnchor],
    clause_seed: int,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    cursor = start
    clause = clause_seed
    # Boundary detection runs on the length-preserving mechanical view.  A period or connector
    # inside a masked structured command is syntax, not a natural-language event boundary.
    raw_boundaries = list(_HARD_BOUNDARY_RE.finditer(detector, start, end))
    boundaries = []
    prior_boundary_end = start
    for index, boundary in enumerate(raw_boundaries):
        next_boundary_start = raw_boundaries[index + 1].start() \
            if index + 1 < len(raw_boundaries) else end
        left_targets = [
            anchor for anchor in anchors
            if anchor.kind == "target" and anchor.end <= boundary.start()
        ]
        right_targets = [
            anchor for anchor in anchors
            if anchor.kind == "target"
            and boundary.end() <= anchor.start < next_boundary_start
        ]
        target_list_boundary = any(
            is_target_coordination_bridge(detector[left.end:right.start])
            and not any(
                semantic.kind in {"capability", "action"}
                and left.end <= semantic.start
                and semantic.end <= right.start
                for semantic in anchors
            )
            for left in left_targets
            for right in right_targets
        )
        additive_target_but = (
            re.search(r"\bbut\b", boundary.group(0), re.IGNORECASE) is not None
            and re.search(
                r"\bnot\s+(?:only|merely|just|simply)\b",
                detector[prior_boundary_end:boundary.start()],
                re.IGNORECASE,
            ) is not None
            and not any(
                anchor.kind in {"capability", "action"}
                and boundary.end() <= anchor.start < next_boundary_start
                for anchor in anchors
            )
        )
        cannot_help_but = (
            re.search(r"\bbut\b", boundary.group(0), re.IGNORECASE) is not None
            and re.search(
                r"\bcannot\s+help\s*$",
                detector[prior_boundary_end:boundary.start()],
                re.IGNORECASE,
            ) is not None
        )
        if target_list_boundary or additive_target_but or cannot_help_but:
            continue
        boundaries.append(boundary)
        prior_boundary_end = boundary.end()
    for boundary in [*boundaries, None]:
        stop = boundary.start() if boundary is not None else end
        base_start, base_end = _trim(text, cursor, stop)
        if base_end > base_start:
            cuts = _coordination_cuts(
                text, detector, base_start, base_end, anchors,
            )
            inner = base_start
            for cut_start, cut_end in [*cuts, (base_end, base_end)]:
                seg_start, seg_end = _trim(text, inner, cut_start)
                if seg_end > seg_start:
                    rows.append({
                        "span": (seg_start, seg_end),
                        "clause_span": (base_start, base_end),
                        "clause_index": clause,
                        "quoted": False,
                    })
                inner = cut_end
        clause += 1
        cursor = boundary.end() if boundary is not None else end
    return rows, clause


def _segments(
    text: str,
    detector: str,
    anchors: list[OccurrenceAnchor],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = 0
    clause = 0
    for quote in _QUOTE_RE.finditer(text):
        outside, clause = _outside_segments(
            text, detector, cursor, quote.start(), anchors, clause,
        )
        rows.extend(outside)
        quote_start, quote_end = _trim(text, quote.start(), quote.end())
        if quote_end > quote_start:
            rows.append({
                "span": (quote_start, quote_end),
                "clause_span": (quote_start, quote_end),
                "clause_index": clause,
                "quoted": True,
            })
            clause += 1
        cursor = quote.end()
    outside, _clause = _outside_segments(
        text, detector, cursor, len(text), anchors, clause,
    )
    rows.extend(outside)
    return sorted(rows, key=lambda row: (row["span"][0], row["span"][1]))


def _prefix_words(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD_TOKEN_RE.finditer(text))


def _skip_modal_modifiers(words: tuple[str, ...], index: int) -> int:
    while index < len(words) and (
        words[index] in _MODAL_MODIFIERS or words[index].endswith("ly")
    ):
        index += 1
    return index


def _modal_prefix_actuality(words: tuple[str, ...]) -> str | None:
    """Classify nonactual modality from the exact prefix that owns one semantic event."""
    if not words:
        return None
    if any(word in _FUTURE_SCOPE_WORDS for word in words):
        return "unknown"
    if words[0] in {"if", "unless", "suppose", "supposing", "assuming", "imagine",
                    "imagining", "hypothetically", "maybe", "perhaps"}:
        return "hypothetical"
    if words[:2] == ("in", "case"):
        return "hypothetical"
    if len(words) >= 2 and words[0] in {"were", "had", "should"} \
            and words[1] in _EVENT_SUBJECTS:
        return "hypothetical"

    for index, word in enumerate(words):
        if word not in _EVENT_SUBJECTS:
            continue
        cursor = _skip_modal_modifiers(words, index + 1)
        if cursor < len(words) and words[cursor] in _MODAL_AUXILIARIES:
            return "hypothetical"
        if cursor < len(words) and words[cursor] in {"will", "shall"}:
            return "unknown"
        if cursor < len(words) and words[cursor] == "did":
            return "unknown"
        if cursor < len(words) and words[cursor] in {"am", "are", "is", "m", "was", "were"}:
            if words[cursor] in {"was", "were"}:
                return "unknown"
            cursor = _skip_modal_modifiers(words, cursor + 1)
            if cursor < len(words) and words[cursor] in {"going", "about"}:
                return "unknown"
    if len(words) >= 2 and words[0] in _MODAL_AUXILIARIES \
            and words[1] in _EVENT_SUBJECTS:
        return "hypothetical"
    return None


def _has_nonactual_have(words: tuple[str, ...]) -> bool:
    """Detect perfect, obligation, and causative ``have`` before the event anchor."""
    for word in words:
        if word not in {"have", "has", "had", "having"}:
            continue
        # Perfect aspect (``I have burned``), obligation (``I have to``), and causation
        # (``I have Bo``) are all noncurrent at this boundary. Aspectual begin/start without
        # ``have`` stays actual.
        return True
    return False


def _past_surface_word(word: str) -> bool:
    token = str(word or "").casefold()
    return bool(
        token in _UNAMBIGUOUS_IRREGULAR_PAST
        or (
            len(token) >= 5
            and token.endswith("ed")
            and token not in _PRESENT_COMPATIBLE_ED
        )
    )


def _masked_player_predicate_is_past(words: tuple[str, ...]) -> bool:
    """Recognize a past predicate in a structured command's unanchored prose companion."""
    for index, word in enumerate(words):
        if word not in _EVENT_SUBJECTS:
            continue
        cursor = index + 1
        while cursor < len(words) and (
            words[cursor] in _PAST_PREDICATE_FILLERS or words[cursor].endswith("ly")
        ):
            cursor += 1
        return cursor < len(words) and _past_surface_word(words[cursor])
    return False


def _visible_event_surface_is_past(
    detector: str,
    segment_start: int,
    anchors: Iterable[OccurrenceAnchor],
) -> bool:
    """Require an exact main-predicate surface before treating regular ``-ed`` as past.

    A named ability can itself end in ``-ed``. It is not tense evidence when a different lexical
    predicate precedes it (for example ``I use Burned Earth``). A direct Player predicate or an
    actor-less continuation has no such residual word, so its unambiguous surface is sufficient.
    """
    for anchor in sorted(anchors, key=lambda row: (row.start, row.end, row.kind, row.source)):
        if not detector[anchor.start:anchor.end].strip() or anchor.source == "candidate_explicit":
            continue
        surface_words = _prefix_words(detector[anchor.start:anchor.end])
        if not surface_words or not _past_surface_word(surface_words[0]):
            continue
        prefix_words = _prefix_words(detector[segment_start:anchor.start])
        residual = tuple(
            word for word in prefix_words
            if word not in _EVENT_SUBJECTS
            and word not in _PAST_PREDICATE_FILLERS
            and not word.endswith("ly")
        )
        if not residual:
            return True
    return False


def _embedding_prefix_actuality(prefix: str) -> str | None:
    """Return the nonactual context governing the event at the end of ``prefix``.

    This is deliberately a clause-local embedding classifier rather than a sentence-start regex.
    The caller cuts punctuation and explicit renewed clauses first, and passes only the owned text
    ending at the capability/action anchor.  An arbitrary modifier therefore cannot hide a
    representation, cognition, directive, commitment, inability, or nominalized event owner.
    """
    # ``cannot help but`` is a lexical positive (the action is compelled), not inability. Remove
    # only that exact idiom before the ordinary inability and modality classifier runs.
    cleaned = _CANNOT_HELP_BUT_RE.sub(
        lambda match: " " * len(match.group(0)), str(prefix or ""),
    )
    words = _prefix_words(cleaned)
    modal = _modal_prefix_actuality(words)
    if modal is not None:
        return modal
    if _has_nonactual_have(words):
        return "unknown"
    if _masked_player_predicate_is_past(words):
        return "unknown"
    pairs = set(zip(words, words[1:]))
    if pairs & {("act", "like"), ("act", "as"), ("set", "to"), ("used", "to")}:
        return "unknown"
    heads = (
        _REPRESENTATION_GOVERNORS
        | _DIRECTIVE_GOVERNORS
        | _COMMITMENT_GOVERNORS
        | _NONPERFORMING_NOUNS
        | _INABILITY_GOVERNORS
        | {"according", "allegedly", "convinced", "reportedly", "supposedly"}
    )
    if any(word in heads for word in words):
        return "unknown"
    return None


def _completed_outer_performative(text: str) -> bool:
    """Whether a capability-free left clause completed its own speech/cognition act."""
    words = _prefix_words(text)
    return bool(
        any(word in _EVENT_SUBJECTS for word in words)
        and any(
            word in (
                _REPRESENTATION_GOVERNORS
                | _DIRECTIVE_GOVERNORS
                | _COMMITMENT_GOVERNORS
            )
            for word in words
        )
    )


def _actuality(text: str, quoted: bool, *, semantic_prefix: str = "") -> str:
    if quoted:
        return "quoted"
    if _REPORTED_SPEECH_RE.search(text):
        return "quoted"
    if _HYPOTHETICAL_RE.search(text):
        return "hypothetical"
    if _METAPHOR_RE.search(text):
        return "metaphorical"
    if _PAST_TIME_RE.search(text):
        return "unknown"
    embedded = _embedding_prefix_actuality(semantic_prefix)
    if embedded is not None:
        return embedded
    return "actual"


def _polarity_prefix_start(
    detector: str,
    start: int,
    stop: int,
    anchors: Iterable[OccurrenceAnchor],
) -> int:
    """Renew polarity ownership when the same actor starts a comma-spliced event."""
    actors = sorted(
        (
            anchor for anchor in anchors
            if anchor.kind == "actor"
            and start <= anchor.start < anchor.end <= stop
            and detector[anchor.start:anchor.end].strip()
        ),
        key=lambda anchor: (anchor.start, anchor.end, anchor.identity),
    )
    for index in range(len(actors) - 1, 0, -1):
        actor = actors[index]
        if not any(previous.identity == actor.identity for previous in actors[:index]):
            continue
        if re.search(r",\s*$", detector[start:actor.start]):
            return actor.start
    return start


def _polarity(prefix: str) -> str:
    """Classify one event-local prefix without turning double negation into authority.

    The occurrence segment already supplies the clause boundary.  Mask additive ``not only``
    constructions because they affirm the governed action, then count explicit negators.  Two or
    more negators are structurally ambiguous for mechanical admission (for example ``I don't
    choose not to burn``); they must not be simplified into an executable positive action.
    """
    normalized = str(prefix or "").replace("\u2019", "'")
    normalized = _CANNOT_HELP_BUT_RE.sub(
        lambda match: " " * len(match.group(0)), normalized,
    )

    nonperforming_heads = (
        _REPRESENTATION_GOVERNORS
        | _DIRECTIVE_GOVERNORS
        | _COMMITMENT_GOVERNORS
        | _NONPERFORMING_NOUNS
        | _INABILITY_GOVERNORS
    )

    def mask_additive(match: re.Match[str]) -> str:
        suffix = _prefix_words(normalized[match.end():])
        cursor = _skip_modal_modifiers(suffix, 0)
        # With a visible capability anchor the additive phrase ends the prefix.  A structured
        # declaration instead supplies the whole companion, so mask only when the next lexical
        # head is the performed event rather than a representation such as ``planning to``.
        if cursor < len(suffix) and suffix[cursor] in nonperforming_heads:
            return match.group(0)
        return " " * len(match.group(0))

    scoped = _ADDITIVE_NOT_RE.sub(
        mask_additive, normalized,
    )
    if len(_EXPLICIT_NEGATOR_RE.findall(scoped)) > 1:
        return "unknown"
    return "negated" if _NEGATION_RE.search(scoped) else "affirmative"


def _relation(gap: str) -> str:
    low = gap.casefold()
    if re.search(r"\b(?:but|yet|whereas)\b", low):
        return "contrast"
    if re.search(r"\b(?:while|as)\b", low):
        return "overlap"
    if re.search(r"\b(?:and|then)\b", low):
        return "coordination"
    return "sequence"


def build_occurrence_graph(
    source_text: str,
    *,
    detection_text: str | None = None,
    anchors: Iterable[OccurrenceAnchor | Mapping[str, Any]] = (),
    issuer: str,
    channel: str,
    lifecycle_phase: str,
    grammar_version: str,
    operation_family: str,
) -> dict[str, Any]:
    """Build one ordered content-free graph from exact caller-proven anchors.

    ``detection_text`` must be a length-preserving mechanical view (for example quote masking).
    It is used only for polarity/actuality context; source coordinates always reference
    ``source_text``.
    """
    source = str(source_text or "")
    detector = source if detection_text is None else str(detection_text)
    if not source or len(detector) != len(source):
        raise OccurrenceGraphError("occurrence source and detector must be nonempty and aligned")
    # A caller may already mask control syntax, but the occurrence boundary must remain safe when
    # it supplies only dialogue masking.  Structured syntax selects a capability; its punctuation
    # and words are not the natural-language context that decides whether the Player performed it.
    detector = _STRUCTURED_COMMAND_RE.sub(
        lambda match: " " * len(match.group(0)), detector,
    )
    authority = _authority_payload(
        issuer=issuer,
        channel=channel,
        lifecycle_phase=lifecycle_phase,
        grammar_version=grammar_version,
        operation_family=operation_family,
    )
    canonical = _canonical_anchors(anchors, len(source))
    nodes: list[dict[str, Any]] = []
    masked_semantic_nodes: set[str] = set()
    for order, segment in enumerate(_segments(source, detector, canonical), 1):
        start, end = segment["span"]
        owned = [anchor for anchor in canonical if start <= anchor.start and anchor.end <= end]
        semantic = [anchor for anchor in owned if anchor.kind in ("capability", "action")]
        actions = [anchor for anchor in semantic if anchor.kind == "action"]
        # A structured capability declaration can precede the performed event it governs, as in
        # ``((aether.check melee)) I do not cut the revenant``.  Scope polarity from the first
        # action anchor when one exists; otherwise the declaration would hide the event-local
        # negation merely because its capability token appeared first.  Capability-only
        # occurrences retain their established anchor behavior.
        polarity_anchors = actions or semantic
        visible_polarity_anchors = [
            anchor for anchor in polarity_anchors
            if detector[anchor.start:anchor.end].strip()
        ]
        # An explicit structured capability is intentionally blanked in the mechanical view.
        # When it is the only semantic anchor, its following natural-language companion owns
        # polarity/modality.  Looking only before the blanked anchor let a command receipt turn
        # ``Do not burn`` into a positive roll.
        if polarity_anchors and not visible_polarity_anchors:
            prefix = detector[start:end]
        else:
            first_semantic = min(
                (anchor.start for anchor in visible_polarity_anchors), default=end,
            )
            prefix_start = _polarity_prefix_start(
                detector, start, first_semantic, owned,
            )
            prefix = detector[prefix_start:first_semantic]
        polarity = _polarity(prefix)
        # Classify the context that governs the capability, not the first unrelated action word.
        # This lets an actual speech performative remain actual when it is itself the capability,
        # while ``I mention [burning]`` keeps the represented burn non-performing.  Visible
        # capability evidence is primary; an action anchor is only the fallback for nodes without
        # one.  Fully masked structured declarations retain the whole companion as their context.
        visible_capabilities = [
            anchor for anchor in semantic
            if anchor.kind == "capability" and detector[anchor.start:anchor.end].strip()
        ]
        visible_actions = [
            anchor for anchor in semantic
            if anchor.kind == "action" and detector[anchor.start:anchor.end].strip()
        ]
        masked_capability = any(
            anchor.kind == "capability" and not detector[anchor.start:anchor.end].strip()
            for anchor in semantic
        )
        # A visible outer action such as ``promise`` or ``order`` cannot become the actuality
        # owner of a masked explicit Burn declaration. The entire natural-language companion is
        # the structured command's owned context; direct actions remain actual, embedded actions
        # remain nonactual, and both paths use this same classifier.
        if masked_capability and visible_capabilities:
            first_actuality_anchor = min(anchor.start for anchor in visible_capabilities)
            actuality_prefix = detector[start:first_actuality_anchor]
        elif masked_capability:
            actuality_prefix = detector[start:end]
        else:
            actuality_anchors = visible_capabilities or visible_actions
            first_actuality_anchor = min(
                (anchor.start for anchor in actuality_anchors), default=end,
            )
            actuality_prefix = detector[start:first_actuality_anchor]
        actuality = _actuality(
            detector[start:end],
            bool(segment["quoted"]),
            semantic_prefix=actuality_prefix,
        )
        past_surface = False
        if actuality == "actual" and _visible_event_surface_is_past(
            detector, start, semantic,
        ):
            actuality = "unknown"
            past_surface = True
        by_kind: dict[str, list[dict[str, Any]]] = {kind: [] for kind in _ANCHOR_KINDS}
        for anchor in owned:
            by_kind[anchor.kind].append(anchor.as_dict())
        for kind in by_kind:
            by_kind[kind] = sorted(
                by_kind[kind],
                key=lambda row: (row["span"][0], row["span"][1], row["identity"], row["source"]),
            )

        # ``neither Ana nor Bo`` follows the action anchor rather than preceding it, so the
        # ordinary event-prefix classifier cannot see its negative scope.  Once both exact
        # grounded patients belong to this same occurrence, bind the correlative only when it
        # begins the target corridor after the nearest semantic anchor.  A pre-existing negative
        # makes the compound structurally double-negative and therefore unresolved.
        target_rows = by_kind["target"]
        target_span_sources: dict[tuple[int, int], set[str]] = {}
        for row in target_rows:
            target_span_sources.setdefault(tuple(row["span"]), set()).add(row["source"])
        target_binding_spans = sorted(target_span_sources)
        coordinated_target_bindings = any(
            left[1] <= right[0]
            and (
                "coordinated_patient" in target_span_sources[right]
                or is_target_coordination_bridge(detector[left[1]:right[0]])
            )
            for index, left in enumerate(target_binding_spans)
            for right in target_binding_spans[index + 1:]
        )
        # Cardinality belongs to non-overlapping grammatical arms, not the eventual world
        # identity set. ``Ana or Ana`` is still a coordinated two-patient construction, while one
        # written ``Sentry`` that resolves to two possible entities is one ambiguous target slot.
        # Non-coordinated repeated evidence (``Ana in Ana's ribs``) also describes one patient.
        multiple_target_bindings = coordinated_target_bindings
        if multiple_target_bindings:
            first_target = min(row["span"][0] for row in target_rows)
            last_target = max(row["span"][1] for row in target_rows)
            preceding_semantic = [
                anchor for anchor in semantic if anchor.end <= first_target
            ]
            if preceding_semantic:
                target_anchor = max(
                    preceding_semantic,
                    key=lambda anchor: (anchor.end, anchor.start, anchor.kind, anchor.source),
                )
                if _NEITHER_TARGET_SCOPE_RE.search(
                    detector[target_anchor.end:last_target]
                ):
                    polarity = "negated" if polarity == "affirmative" else "unknown"

        reasons: set[str] = set()
        if semantic:
            if any(
                "ambiguous_target_scope" in sources
                for sources in target_span_sources.values()
            ):
                reasons.add("occurrence.target_scope_unbound")
            if polarity == "negated":
                reasons.add("occurrence.negated")
            elif polarity == "unknown":
                reasons.add("occurrence.polarity_unbound")
            if actuality == "unknown":
                reasons.add("occurrence.actuality_unbound")
            elif actuality != "actual":
                reasons.add(f"occurrence.{actuality}")
            if past_surface:
                reasons.add("occurrence.past_surface")
            for kind, label in (("actor", "actors"), ("target", "targets"),
                                ("capability", "capabilities"), ("action", "actions")):
                identities = {row["identity"] for row in by_kind[kind]}
                if (kind == "target" and multiple_target_bindings) \
                        or (kind != "target" and len(identities) > 1):
                    reasons.add(f"occurrence.multiple_{label}")
            if not by_kind["actor"]:
                reasons.add("occurrence.actor_unbound")
            if not authority["allowed"]:
                reasons.add(f"occurrence.authority.{authority['reason']}")

            # A later action anchor cannot borrow an earlier capability merely because both fit
            # inside one punctuation clause.  A real capability realization has overlapping
            # capability evidence at the action (including every verb of an authored maneuver).
            # When a coordination connector instead separates an unlicensed action, preserve the
            # compound as unresolved rather than constructing e.g. Praise -> weapon_attack.
            capability_spans = [
                (anchor.start, anchor.end) for anchor in owned
                if anchor.kind == "capability"
            ]
            capability_sources = {
                anchor.source for anchor in owned if anchor.kind == "capability"
            }
            for action in (anchor for anchor in owned if anchor.kind == "action"):
                overlaps_capability = any(
                    cap_start < action.end and action.start < cap_end
                    for cap_start, cap_end in capability_spans
                )
                preceding_capabilities = [
                    (cap_start, cap_end) for cap_start, cap_end in capability_spans
                    if cap_end <= action.start
                ]
                if overlaps_capability or not preceding_capabilities:
                    continue
                # Explicit commands and authored maneuvers intentionally project one chosen
                # capability across their code-bound action chain.  The hazardous shape is one
                # lone lexical capability occurrence followed by a different coordinated action.
                if "candidate_explicit" in capability_sources \
                        or len(set(capability_spans)) != 1 \
                        or re.search(
                            r"\b(?:use|using|invoke|invoking|cast|casting|perform|performing|"
                            r"execute|executing|channel|channeling|focus)\b",
                            detector[start:action.start],
                            re.IGNORECASE,
                        ):
                    continue
                _cap_start, cap_end = max(
                    preceding_capabilities, key=lambda span: (span[1], span[0]),
                )
                if _COORDINATION_RE.search(detector[cap_end:action.start]):
                    actuality = "unknown"
                    reasons.update({
                        "occurrence.action_scope_unbound",
                        "occurrence.actuality_unbound",
                    })
                    break

        occurrence_id = f"occurrence.{order}"
        if masked_capability:
            masked_semantic_nodes.add(occurrence_id)
            # Tier-0 may label the structured declaration itself as a command.  Preserve an
            # independently scoped nonactual companion as a structural conflict so that command
            # status cannot erase hypothetical/imagined meaning during admission.
            if actuality not in ("actual", "quoted"):
                reasons.add("occurrence.actuality_unbound")

        nodes.append({
            "occurrence_id": occurrence_id,
            "order": order,
            "source_span": [start, end],
            "clause_span": [*segment["clause_span"]],
            "clause_index": int(segment["clause_index"]),
            "polarity": polarity,
            "actuality": actuality,
            "actors": by_kind["actor"],
            "targets": by_kind["target"],
            "capabilities": by_kind["capability"],
            "actions": by_kind["action"],
            "unresolved_reasons": sorted(reasons),
        })

    edges = []
    for left, right in zip(nodes, nodes[1:]):
        boundary = [left["source_span"][1], right["source_span"][0]]
        relation = _relation(source[boundary[0]:boundary[1]])
        edges.append({
            "from_occurrence": left["occurrence_id"],
            "to_occurrence": right["occurrence_id"],
            "relation": relation,
            "boundary_span": boundary,
        })
        # A masked structured declaration followed immediately by quoted/code-sample content is
        # one explicit-command scope conflict.  The quote cannot lend an executable action back to
        # the declaration; preserve it as quoted and force the same fail-closed ambiguity that a
        # directly quoted capability would carry.
        if left["occurrence_id"] in masked_semantic_nodes \
                and right["actuality"] == "quoted" \
                and not right["capabilities"] and not right["actions"]:
            left["actuality"] = "quoted"
            left["unresolved_reasons"] = sorted(set(left["unresolved_reasons"]) | {
                "occurrence.quoted",
            })
        # A shared modifier before an actor-less coordinated predicate is not local evidence for
        # the second occurrence. A new explicit Player subject renews authority; the synthetic
        # turn-speaker default does not. Propagate negative, represented, hypothetical, and future
        # scope only across the actor-less continuation and keep it explicitly unresolved.
        right_has_explicit_actor = any(
            row.get("source") == "first_person" for row in right["actors"]
        )
        left_text = source[left["source_span"][0]:left["source_span"][1]]
        conditional_continuation = (
            relation == "coordination"
            and left["actuality"] == "hypothetical"
            and re.match(
                r"\s*(?:if|unless|were|had|should)\b", left_text, re.IGNORECASE,
            ) is not None
        )
        completed_outer = (
            not left["capabilities"] and _completed_outer_performative(left_text)
        )
        completed_outer_question = (
            relation == "coordination"
            and re.search(r"\bthen\b", source[boundary[0]:boundary[1]], re.IGNORECASE)
            is not None
            and _COMPLETED_QUESTION_EMBEDDING_RE.search(left_text) is not None
        )
        completed_outer = completed_outer or completed_outer_question
        inherited_scope = relation == "coordination" and (
            (not right_has_explicit_actor and not completed_outer)
            or conditional_continuation
        )
        if inherited_scope and left["polarity"] in {"negated", "unknown"} \
                and right["polarity"] == "affirmative":
            right["polarity"] = "unknown"
            right["unresolved_reasons"] = sorted(set(right["unresolved_reasons"]) | {
                "occurrence.polarity_unbound",
            })
        if inherited_scope and left["actuality"] != "actual" \
                and right["actuality"] == "actual":
            right["actuality"] = "unknown"
            right["unresolved_reasons"] = sorted(set(right["unresolved_reasons"]) | {
                "occurrence.actuality_unbound",
            })
    payload = {
        "schema": OCCURRENCE_GRAPH_SCHEMA,
        "source_fingerprint": content_fingerprint(source),
        "authority": authority,
        "occurrences": nodes,
        "edges": edges,
    }
    return validate_occurrence_graph(
        {**payload, "fingerprint": content_fingerprint(payload)}, source_text=source,
    )


def validate_occurrence_graph(
    value: object,
    *,
    source_text: str | None = None,
) -> dict[str, Any]:
    """Validate canonical ordering and the no-cross-occurrence provenance invariant."""
    fields = {
        "schema", "source_fingerprint", "authority", "occurrences", "edges", "fingerprint",
    }
    if not isinstance(value, dict) or set(value) != fields \
            or value.get("schema") != OCCURRENCE_GRAPH_SCHEMA:
        raise OccurrenceGraphError("occurrence graph fields or schema are invalid")
    for key in ("source_fingerprint", "fingerprint"):
        if not isinstance(value.get(key), str) or _FINGERPRINT_RE.fullmatch(value[key]) is None:
            raise OccurrenceGraphError(f"occurrence graph {key} is invalid")
    if source_text is not None and value["source_fingerprint"] != content_fingerprint(source_text):
        raise OccurrenceGraphError("occurrence graph belongs to a different source")

    authority = value.get("authority")
    authority_fields = {
        "schema", "issuer", "channel", "lifecycle_phase", "grammar_version",
        "operation_family", "allowed", "reason",
    }
    if not isinstance(authority, dict) or set(authority) != authority_fields \
            or authority.get("schema") != OCCURRENCE_AUTHORITY_SCHEMA:
        raise OccurrenceGraphError("occurrence authority fields or schema are invalid")
    expected_authority = _authority_payload(
        issuer=str(authority.get("issuer") or ""),
        channel=str(authority.get("channel") or ""),
        lifecycle_phase=str(authority.get("lifecycle_phase") or ""),
        grammar_version=str(authority.get("grammar_version") or ""),
        operation_family=str(authority.get("operation_family") or ""),
    )
    if authority != expected_authority:
        raise OccurrenceGraphError("occurrence directional authority was not derived")

    nodes = value.get("occurrences")
    if not isinstance(nodes, list) or not nodes:
        raise OccurrenceGraphError("occurrence graph needs ordered nodes")
    node_fields = {
        "occurrence_id", "order", "source_span", "clause_span", "clause_index",
        "polarity", "actuality", "actors", "targets", "capabilities", "actions",
        "unresolved_reasons",
    }
    ids: list[str] = []
    previous_end = 0
    for expected, node in enumerate(nodes, 1):
        if not isinstance(node, dict) or set(node) != node_fields:
            raise OccurrenceGraphError("occurrence node fields are invalid")
        node_id = _stable_id(node.get("occurrence_id"), "occurrence id")
        if node.get("order") != expected or node_id in ids:
            raise OccurrenceGraphError("occurrence node ordering is not canonical")
        ids.append(node_id)
        span = node.get("source_span")
        clause = node.get("clause_span")
        if not isinstance(span, list) or len(span) != 2 \
                or not isinstance(clause, list) or len(clause) != 2 \
                or any(isinstance(item, bool) or not isinstance(item, int)
                       for item in (*span, *clause)) \
                or span[0] < previous_end or span[1] <= span[0] \
                or clause[0] > span[0] or clause[1] < span[1] \
                or clause[1] <= clause[0]:
            raise OccurrenceGraphError("occurrence spans are invalid or overlap")
        previous_end = span[1]
        if isinstance(node.get("clause_index"), bool) \
                or not isinstance(node.get("clause_index"), int) \
                or node["clause_index"] < 0 \
                or node.get("polarity") not in _POLARITIES \
                or node.get("actuality") not in _ACTUALITIES:
            raise OccurrenceGraphError("occurrence context fields are invalid")
        for field in ("actors", "targets", "capabilities", "actions"):
            bindings = node.get(field)
            if not isinstance(bindings, list):
                raise OccurrenceGraphError("occurrence bindings must be lists")
            canonical: list[tuple[int, int, str, str]] = []
            for binding in bindings:
                if not isinstance(binding, dict) or set(binding) != {"identity", "span", "source"}:
                    raise OccurrenceGraphError("occurrence binding fields are invalid")
                _stable_id(binding.get("identity"), "occurrence binding identity")
                _stable_id(binding.get("source"), "occurrence binding source")
                evidence = binding.get("span")
                if not isinstance(evidence, list) or len(evidence) != 2 \
                        or any(isinstance(item, bool) or not isinstance(item, int)
                               for item in evidence) \
                        or evidence[0] < span[0] or evidence[1] > span[1] \
                        or evidence[1] <= evidence[0]:
                    raise OccurrenceGraphError(
                        "occurrence binding crosses its owning source boundary"
                    )
                canonical.append((evidence[0], evidence[1], binding["identity"], binding["source"]))
            if canonical != sorted(set(canonical)):
                raise OccurrenceGraphError("occurrence bindings are not canonical")
        reasons = node.get("unresolved_reasons")
        if not isinstance(reasons, list) or reasons != sorted(set(reasons)) \
                or any(_STABLE_ID_RE.fullmatch(str(reason or "")) is None for reason in reasons):
            raise OccurrenceGraphError("occurrence unresolved reasons are not canonical")

    edges = value.get("edges")
    if not isinstance(edges, list) or len(edges) != max(0, len(nodes) - 1):
        raise OccurrenceGraphError("occurrence graph edges do not match its nodes")
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict) or set(edge) != {
            "from_occurrence", "to_occurrence", "relation", "boundary_span",
        } or edge.get("from_occurrence") != ids[index] \
                or edge.get("to_occurrence") != ids[index + 1] \
                or edge.get("relation") not in _RELATIONS:
            raise OccurrenceGraphError("occurrence graph edge is invalid")
        boundary = edge.get("boundary_span")
        if not isinstance(boundary, list) or len(boundary) != 2 \
                or boundary != [nodes[index]["source_span"][1],
                                nodes[index + 1]["source_span"][0]]:
            raise OccurrenceGraphError("occurrence edge boundary is invalid")

    payload = {key: value[key] for key in value if key != "fingerprint"}
    if value["fingerprint"] != content_fingerprint(payload):
        raise OccurrenceGraphError("occurrence graph fingerprint mismatch")
    return {key: value[key] for key in value}


def occurrence_for_span(graph: Mapping[str, Any], start: int, end: int) -> dict[str, Any] | None:
    """Return the sole node that wholly owns a span; partial/cross-node overlap abstains."""
    if isinstance(start, bool) or isinstance(end, bool) \
            or not isinstance(start, int) or not isinstance(end, int) or end <= start:
        return None
    matches = [
        node for node in graph.get("occurrences") or ()
        if isinstance(node, dict)
        and node.get("source_span", [0, 0])[0] <= start
        and end <= node.get("source_span", [0, 0])[1]
    ]
    return dict(matches[0]) if len(matches) == 1 else None
