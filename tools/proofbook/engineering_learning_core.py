from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SCHEMA = "aetherstate/engineering-lesson/1"
LIFECYCLES = frozenset({"candidate", "verified", "invalidated"})
DOMAINS = frozenset(
    {
        "semantic_cube",
        "worldlex",
        "rpg",
        "enemies",
        "narrator",
        "lifecycle",
        "persistence",
        "retrieval",
        "ui",
        "environment",
        "tooling",
        "project_continuity",
    }
)
CUBE_BOUNDARIES = frozenset(
    {
        "recognition",
        "binding",
        "world_alignment",
        "admission",
        "complete_settlement",
        "narrator_transfer",
        "model_compliance",
        "hud_visibility",
    }
)
CUBE_FAILURE_CLASSES = frozenset(
    {
        "coverage",
        "construction",
        "authority_oracle",
        "ordering_wiring",
        "admission",
        "settlement",
        "delivery",
        "model_compliance",
        "visibility",
    }
)
EVIDENCE_KINDS = frozenset(
    {"audit", "pin", "test", "reproduction", "contract", "commit"}
)
VERIFICATION_MODES = frozenset({"focused", "sealed", "independent"})
PUBLIC_PROVENANCE = "public_contract"
PRIVACY_STATUSES = frozenset({"pending", "approved"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "lesson_key",
        "revision",
        "record_id",
        "lifecycle",
        "domain",
        "diagnosis",
        "symptom",
        "cause",
        "repair_rule",
        "scope",
        "owners",
        "regressions",
        "evidence",
        "tags",
        "verification",
        "provenance",
        "privacy_review",
        "supersedes",
        "rationale",
    }
)
_CUBE_PAIRS = {
    "recognition": frozenset({"coverage"}),
    "binding": frozenset({"construction", "ordering_wiring"}),
    "world_alignment": frozenset({"authority_oracle", "ordering_wiring"}),
    "admission": frozenset({"admission", "ordering_wiring"}),
    "complete_settlement": frozenset({"settlement", "ordering_wiring"}),
    "narrator_transfer": frozenset({"delivery", "ordering_wiring"}),
    "model_compliance": frozenset({"model_compliance"}),
    "hud_visibility": frozenset({"visibility"}),
}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LESSON_KEY_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")
_BOUNDARY_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_.:#-]{1,128}\Z")
_SUPERSESSION_REASON_PREFIXES = (
    ("Corrects ", "corrected"),
    ("Narrows ", "narrowed"),
    ("Expands ", "expanded"),
    ("Invalidates ", "invalidated"),
)
_PRIVATE_PATTERNS = (
    re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|client[_-]?secret|(?:secret|private)[_-]?key|
        (?:access|refresh|session|auth)[_-]?token|token|password|credential)
        \s*["']?\s*[:=]
        """
    ),
    re.compile(
        r"""(?ix)
        \bauthorization\s*["']?\s*[:=]\s*["']?\s*[^\s"']+
        """
    ),
    re.compile(r"(?i)\braw\s+(?:model\s+)?(?:prompt|reply|analysis|reasoning)\b"),
    re.compile(r"(?i)\bpersonal\s+(?:chat|campaign)\b"),
    re.compile(r"(?i)traceback\s*\(most recent call last\)"),
    re.compile(r"(?i)https?://"),
    re.compile(r"(?i)(?<![a-z0-9_])[a-z]:[\\/]"),
    re.compile(r"(?i)(?<![a-z0-9_])/(?:home|users|root)/[^\s]+"),
    re.compile(r"\\\\[^\s]+\\[^\s]+"),
)
_FORBIDDEN_PUBLIC_PATH_COMPONENTS = frozenset(
    {
        ".codex",
        ".git",
        ".worktrees",
        "aetherstate-personal",
        "local-only",
        "knowledge",
        "tooling",
    }
)


class LedgerError(ValueError):
    pass


def _fail(message: str) -> LedgerError:
    return LedgerError(message)


def _is_nfkc(value: str) -> bool:
    return unicodedata.normalize("NFKC", value) == value


def _require_utf8(value: str, *, field: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _fail(f"{field} must be strict UTF-8") from exc


def _validate_json_tree(
    value: object, *, field: str = "$", seen: set[int] | None = None
) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, float):
        raise _fail(f"{field} contains a float")
    if isinstance(value, str):
        _require_utf8(value, field=field)
        if not _is_nfkc(value):
            raise _fail(f"{field} must be Unicode NFKC")
        return
    if type(value) not in {list, dict}:
        raise _fail(f"{field} contains a non-JSON value")

    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        raise _fail(f"{field} contains a cycle")
    seen.add(identity)
    try:
        if isinstance(value, list):
            for index, item in enumerate(value):
                _validate_json_tree(item, field=f"{field}[{index}]", seen=seen)
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise _fail(f"{field} contains a non-string object key")
            _require_utf8(key, field=f"{field} object key")
            if not _is_nfkc(key):
                raise _fail(f"{field} contains a non-NFKC object key")
            _validate_json_tree(item, field=f"{field}.{key}", seen=seen)
    finally:
        seen.remove(identity)


def canonical_json(value: object) -> str:
    _validate_json_tree(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail(f"value is not canonical JSON: {exc}") from exc


def compute_record_id(record: Mapping[str, object]) -> str:
    if not isinstance(record, Mapping):
        raise _fail("record must be a mapping")
    payload = dict(record)
    payload.pop("record_id", None)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def _mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _fail(f"{field} must be an object")
    return value


def _exact_keys(
    value: dict[str, object],
    expected: frozenset[str],
    *,
    field: str,
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    details = []
    if unexpected:
        details.append("unexpected=" + ",".join(unexpected))
    if missing:
        details.append("missing=" + ",".join(missing))
    raise _fail(f"{field} fields invalid ({'; '.join(details)})")


def _closed_string(value: object, allowed: frozenset[str], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(f"{field} must be one of {sorted(allowed)}")
    return value


def _supersession_reason(rationale: str) -> str:
    for prefix, reason in _SUPERSESSION_REASON_PREFIXES:
        if rationale.startswith(prefix):
            return reason
    raise _fail(
        "supersession rationale must begin with Corrects, Narrows, Expands, "
        "or Invalidates"
    )


def _bounded_identifier(value: object, *, field: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise _fail(f"{field} must be a bounded nonempty string")
    if not _is_nfkc(value) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise _fail(f"{field} must be a stable NFKC identifier")
    return value


def _bounded_prose(
    value: object,
    *,
    field: str,
    maximum_bytes: int = 2048,
    maximum_lines: int = 8,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _fail(f"{field} must be bounded prose")
    if not _is_nfkc(value):
        raise _fail(f"{field} must be Unicode NFKC")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise _fail(f"{field} exceeds its UTF-8 byte limit")
    if value.count("\n") + 1 > maximum_lines or "\r" in value or "\x00" in value:
        raise _fail(f"{field} exceeds its line or character limit")
    for pattern in _PRIVATE_PATTERNS:
        if pattern.search(value):
            raise _fail("record contains private content")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _fail(f"{field} must be a lowercase sha256 identity")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _resolved_root(root: Path) -> Path:
    declared = Path(root)
    if not declared.is_dir():
        raise _fail("workspace root must be an existing directory")
    if _is_reparse(declared):
        raise _fail("workspace root must not be a symlink or reparse point")
    return declared.resolve(strict=True)


def _relative_path(value: object, *, root: Path, field: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise _fail(f"{field} must be a bounded workspace-relative path")
    if not _is_nfkc(value):
        raise _fail(f"{field} must be Unicode NFKC")
    if "\\" in value or "\x00" in value:
        raise _fail(f"{field} must use slash-separated workspace-relative syntax")
    if (
        value.startswith("/")
        or value.startswith("//")
        or re.match(r"(?i)^[a-z]:", value)
    ):
        raise _fail(f"{field} must not be absolute, UNC, or drive-relative")

    pure = PurePosixPath(value)
    parts = value.split("/")
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail(f"{field} contains an empty, dot, or traversal segment")
    if any(
        part.casefold() in _FORBIDDEN_PUBLIC_PATH_COMPONENTS for part in parts
    ):
        raise _fail("reference must use a public repository path")

    resolved_root = _resolved_root(root)
    cursor = resolved_root
    for part in parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if _is_reparse(cursor):
                raise _fail(f"{field} traverses a symlink or reparse point")

    candidate = cursor.resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise _fail(f"{field} escapes the workspace root") from exc
    return value


def _validate_diagnosis(value: object, *, domain: str) -> None:
    diagnosis = _mapping(value, field="diagnosis")
    if domain == "semantic_cube":
        _exact_keys(
            diagnosis,
            frozenset({"boundary", "failure_class"}),
            field="diagnosis",
        )
        boundary = _closed_string(
            diagnosis["boundary"], CUBE_BOUNDARIES, field="diagnosis.boundary"
        )
        failure_class = _closed_string(
            diagnosis["failure_class"],
            CUBE_FAILURE_CLASSES,
            field="diagnosis.failure_class",
        )
        if failure_class not in _CUBE_PAIRS[boundary]:
            raise _fail("diagnosis boundary and failure_class are not a valid pair")
        return

    _exact_keys(diagnosis, frozenset({"boundary"}), field="diagnosis")
    boundary = diagnosis["boundary"]
    if not isinstance(boundary, str) or _BOUNDARY_RE.fullmatch(boundary) is None:
        raise _fail("diagnosis.boundary must be an exact domain boundary")


def _validate_scope(value: object) -> None:
    scope = _mapping(value, field="scope")
    _exact_keys(scope, frozenset({"supported", "not_supported"}), field="scope")
    _bounded_prose(scope["supported"], field="scope.supported")
    _bounded_prose(scope["not_supported"], field="scope.not_supported")


def _validate_owners(value: object, *, root: Path) -> None:
    if not isinstance(value, list) or not value:
        raise _fail("owners must be a nonempty array")
    seen: set[tuple[str, str | None]] = set()
    for index, raw in enumerate(value):
        field = f"owners[{index}]"
        owner = _mapping(raw, field=field)
        expected = frozenset({"path", "sha256"})
        if "symbol" in owner:
            expected |= {"symbol"}
        _exact_keys(owner, expected, field=field)
        path = _relative_path(owner["path"], root=root, field=f"{field}.path")
        digest = _sha256(owner["sha256"], field=f"{field}.sha256")
        symbol = None
        if "symbol" in owner:
            symbol = _bounded_identifier(owner["symbol"], field=f"{field}.symbol")
        identity = (path, symbol)
        if identity in seen:
            raise _fail(f"{field} duplicates an owner reference")
        seen.add(identity)
        if not digest:
            raise AssertionError("unreachable")


def _validate_regressions(value: object, *, root: Path) -> None:
    if not isinstance(value, list) or not value:
        raise _fail("regressions must be a nonempty array")
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        field = f"regressions[{index}]"
        regression = _mapping(raw, field=field)
        expected = frozenset({"runner", "path", "node", "sha256"})
        _exact_keys(regression, expected, field=field)
        _bounded_prose(
            regression["runner"],
            field=f"{field}.runner",
            maximum_bytes=256,
            maximum_lines=1,
        )
        path = _relative_path(regression["path"], root=root, field=f"{field}.path")
        node = _bounded_identifier(regression["node"], field=f"{field}.node")
        _sha256(regression["sha256"], field=f"{field}.sha256")
        identity = (path, node)
        if identity in seen:
            raise _fail(f"{field} duplicates a regression reference")
        seen.add(identity)


def _validate_evidence(value: object, *, root: Path) -> None:
    if not isinstance(value, list) or not value:
        raise _fail("evidence must be a nonempty array")
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        field = f"evidence[{index}]"
        evidence = _mapping(raw, field=field)
        expected = frozenset({"kind", "path", "sha256"})
        _exact_keys(evidence, expected, field=field)
        kind = _closed_string(evidence["kind"], EVIDENCE_KINDS, field=f"{field}.kind")
        path = _relative_path(evidence["path"], root=root, field=f"{field}.path")
        _sha256(evidence["sha256"], field=f"{field}.sha256")
        identity = (kind, path)
        if identity in seen:
            raise _fail(f"{field} duplicates an evidence reference")
        seen.add(identity)


def _validate_tags(value: object, *, allowed_tags: frozenset[str]) -> None:
    if not isinstance(value, list):
        raise _fail("tags must be an array")
    seen: set[str] = set()
    for index, tag in enumerate(value):
        if not isinstance(tag, str) or not _is_nfkc(tag) or tag not in allowed_tags:
            raise _fail(f"tags[{index}] is not a reviewed tag")
        if tag in seen:
            raise _fail("tags must not contain duplicates")
        seen.add(tag)


def _validate_verification(value: object) -> None:
    verification = _mapping(value, field="verification")
    _exact_keys(
        verification,
        frozenset({"evidence_class", "mode"}),
        field="verification",
    )
    _closed_string(
        verification["evidence_class"],
        EVIDENCE_KINDS,
        field="verification.evidence_class",
    )
    _closed_string(verification["mode"], VERIFICATION_MODES, field="verification.mode")


def _validate_privacy_review(value: object, *, lifecycle: str) -> None:
    review = _mapping(value, field="privacy_review")
    _exact_keys(
        review,
        frozenset({"status", "reviewer", "evidence"}),
        field="privacy_review",
    )
    status_value = _closed_string(
        review["status"], PRIVACY_STATUSES, field="privacy_review.status"
    )
    _bounded_identifier(review["reviewer"], field="privacy_review.reviewer", maximum=64)
    _sha256(review["evidence"], field="privacy_review.evidence")
    if lifecycle != "candidate" and status_value != "approved":
        raise _fail("privacy_review must be approved for a non-candidate record")


def validate_record(
    record: Mapping[str, object],
    *,
    root: Path,
    allowed_tags: frozenset[str],
) -> dict[str, object]:
    if not isinstance(record, Mapping):
        raise _fail("record must be a mapping")
    checked = copy.deepcopy(dict(record))
    _validate_json_tree(checked)
    _exact_keys(checked, _TOP_LEVEL_FIELDS, field="record")

    if checked["schema"] != SCHEMA:
        raise _fail("schema is not supported")
    lesson_key = checked["lesson_key"]
    if not isinstance(lesson_key, str) or _LESSON_KEY_RE.fullmatch(lesson_key) is None:
        raise _fail("lesson_key must be a namespaced lowercase key")
    revision = checked["revision"]
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision <= 0
    ):
        raise _fail("revision must be a positive non-boolean integer")
    lifecycle = _closed_string(checked["lifecycle"], LIFECYCLES, field="lifecycle")
    domain = _closed_string(checked["domain"], DOMAINS, field="domain")
    _validate_diagnosis(checked["diagnosis"], domain=domain)

    _bounded_prose(checked["symptom"], field="symptom")
    _bounded_prose(checked["cause"], field="cause")
    _bounded_prose(checked["repair_rule"], field="repair_rule")
    _validate_scope(checked["scope"])
    _validate_owners(checked["owners"], root=root)
    _validate_regressions(checked["regressions"], root=root)
    _validate_evidence(checked["evidence"], root=root)
    _validate_tags(checked["tags"], allowed_tags=allowed_tags)
    _validate_verification(checked["verification"])
    if checked["provenance"] != PUBLIC_PROVENANCE:
        raise _fail("record must use public provenance")
    _validate_privacy_review(checked["privacy_review"], lifecycle=lifecycle)

    supersedes = checked["supersedes"]
    if supersedes is not None:
        _sha256(supersedes, field="supersedes")
    rationale = _bounded_prose(
        checked["rationale"],
        field="rationale",
        maximum_bytes=512,
        maximum_lines=4,
        allow_empty=True,
    )
    if supersedes is not None:
        _supersession_reason(rationale)

    record_id = checked["record_id"]
    _sha256(record_id, field="record_id")
    if record_id != compute_record_id(checked):
        raise _fail("record_id does not match canonical record bytes")
    return checked


@dataclass(frozen=True)
class ReferenceIssue:
    record_id: str
    field: str
    detail: str


@dataclass(frozen=True)
class LedgerView:
    records: tuple[dict[str, object], ...]
    active: tuple[dict[str, object], ...]
    candidates: tuple[dict[str, object], ...]
    superseded: tuple[dict[str, object], ...]
    invalidated: tuple[dict[str, object], ...]
    stale: tuple[ReferenceIssue, ...]


def _storage_path(path: Path, *, root: Path) -> Path:
    resolved_root = _resolved_root(root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise _fail("ledger path escapes the workspace root") from exc
    if not relative.parts:
        raise _fail("ledger path must name a file")
    _relative_path(relative.as_posix(), root=resolved_root, field="ledger path")
    if candidate.exists() and not candidate.is_file():
        raise _fail("ledger path must be a regular file")
    return candidate


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_float(value: str) -> object:
    raise _fail(f"JSON floats are forbidden: {value}")


def _reject_json_constant(value: str) -> object:
    raise _fail(f"non-finite JSON values are forbidden: {value}")


def _parse_line(line: bytes, *, line_number: int) -> dict[str, object]:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(f"ledger line {line_number} is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_pairs,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except LedgerError:
        raise
    except json.JSONDecodeError as exc:
        raise _fail(f"ledger line {line_number} is malformed JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise _fail(f"ledger line {line_number} must be a JSON object")
    return value


def _reference_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _record_reference_issues(
    record: dict[str, object],
    *,
    root: Path,
) -> tuple[ReferenceIssue, ...]:
    record_id = str(record["record_id"])
    issues: list[ReferenceIssue] = []
    groups = (
        ("owners", record["owners"]),
        ("regressions", record["regressions"]),
        ("evidence", record["evidence"]),
    )
    for group_name, raw_items in groups:
        if not isinstance(raw_items, list):
            raise AssertionError("validated reference group changed type")
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                raise AssertionError("validated reference changed type")
            path_value = raw_item["path"]
            if not isinstance(path_value, str):
                raise AssertionError("validated path changed type")
            path = _resolved_root(root) / PurePosixPath(path_value)
            path_field = f"{group_name}[{index}].path"
            content = _reference_bytes(path)
            if content is None:
                issues.append(ReferenceIssue(record_id, path_field, "missing file"))
                continue
            expected = raw_item["sha256"]
            hash_field = f"{group_name}[{index}].sha256"
            actual = "sha256:" + hashlib.sha256(content).hexdigest()
            if actual != expected:
                issues.append(
                    ReferenceIssue(record_id, hash_field, "content hash changed")
                )
            if group_name == "owners" and "symbol" in raw_item:
                symbol = str(raw_item["symbol"])
                try:
                    contains_symbol = symbol in content.decode("utf-8")
                except UnicodeError:
                    contains_symbol = False
                if not contains_symbol:
                    issues.append(
                        ReferenceIssue(
                            record_id,
                            f"{group_name}[{index}].symbol",
                            "symbol not found",
                        )
                    )
            if group_name == "regressions":
                node = str(raw_item["node"])
                try:
                    contains_node = node in content.decode("utf-8")
                except UnicodeError:
                    contains_node = False
                if not contains_node:
                    issues.append(
                        ReferenceIssue(
                            record_id,
                            f"{group_name}[{index}].node",
                            "test node not found",
                        )
                    )
    return tuple(
        sorted(issues, key=lambda issue: (issue.record_id, issue.field, issue.detail))
    )


def _fold_records(
    records: list[dict[str, object]],
    *,
    root: Path,
) -> LedgerView:
    seen_ids: set[str] = set()
    seen_key_revisions: set[tuple[str, int]] = set()
    latest_by_key: dict[str, dict[str, object]] = {}
    grouped: dict[str, list[dict[str, object]]] = {}

    for record in records:
        record_id = str(record["record_id"])
        lesson_key = str(record["lesson_key"])
        revision = int(record["revision"])
        if record_id in seen_ids:
            raise _fail(f"duplicate record_id: {record_id}")
        key_revision = (lesson_key, revision)
        if key_revision in seen_key_revisions:
            raise _fail(f"duplicate lesson key and revision: {lesson_key}#{revision}")
        seen_ids.add(record_id)
        seen_key_revisions.add(key_revision)

        previous = latest_by_key.get(lesson_key)
        if previous is None:
            if revision != 1:
                raise _fail(f"revision for {lesson_key} must begin at 1")
            if record["supersedes"] is not None:
                raise _fail(
                    f"revision 1 for {lesson_key} must not supersede another record"
                )
        else:
            expected_revision = int(previous["revision"]) + 1
            if revision != expected_revision:
                raise _fail(
                    f"revision for {lesson_key} must be exactly {expected_revision}"
                )
            if record["supersedes"] != previous["record_id"]:
                raise _fail(
                    f"supersedes for {lesson_key} must name its immediately preceding record"
                )
        latest_by_key[lesson_key] = record
        grouped.setdefault(lesson_key, []).append(record)

    ordered_records = tuple(
        sorted(records, key=lambda row: (str(row["lesson_key"]), int(row["revision"])))
    )
    superseded: list[dict[str, object]] = []
    active: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    invalidated: list[dict[str, object]] = []
    stale: list[ReferenceIssue] = []

    for lesson_key in sorted(grouped):
        revisions = grouped[lesson_key]
        superseded.extend(revisions[:-1])
        current = revisions[-1]
        lifecycle = current["lifecycle"]
        if lifecycle in {"candidate", "verified"}:
            current_issues = _record_reference_issues(current, root=root)
            if current_issues:
                stale.extend(current_issues)
            elif lifecycle == "candidate":
                candidates.append(current)
            else:
                active.append(current)
        elif lifecycle == "invalidated":
            invalidated.append(current)
        else:
            raise AssertionError("validated lifecycle changed value")

    def sort_key(row: dict[str, object]) -> tuple[str, int]:
        return str(row["lesson_key"]), int(row["revision"])

    return LedgerView(
        records=ordered_records,
        active=tuple(sorted(active, key=sort_key)),
        candidates=tuple(sorted(candidates, key=sort_key)),
        superseded=tuple(sorted(superseded, key=sort_key)),
        invalidated=tuple(sorted(invalidated, key=sort_key)),
        stale=tuple(
            sorted(
                stale, key=lambda issue: (issue.record_id, issue.field, issue.detail)
            )
        ),
    )


def _read_ledger(
    path: Path,
    *,
    root: Path,
    allowed_tags: frozenset[str],
    allow_missing: bool,
) -> LedgerView:
    ledger = _storage_path(path, root=root)
    if not ledger.exists():
        if allow_missing:
            return _fold_records([], root=root)
        raise _fail("ledger file does not exist")
    try:
        payload = ledger.read_bytes()
    except OSError as exc:
        raise _fail(f"could not read ledger: {exc}") from exc
    if payload and not payload.endswith(b"\n"):
        raise _fail("ledger tail is corrupt because it is not LF-terminated")

    records: list[dict[str, object]] = []
    for line_number, line in enumerate(
        payload[:-1].split(b"\n") if payload else (), start=1
    ):
        if not line:
            raise _fail(f"blank ledger line {line_number} is not canonical")
        parsed = _parse_line(line, line_number=line_number)
        checked = validate_record(parsed, root=root, allowed_tags=allowed_tags)
        if line != canonical_json(checked).encode("utf-8"):
            raise _fail(f"ledger line {line_number} is not exact canonical JSON")
        records.append(checked)
    return _fold_records(records, root=root)


def load_ledger(
    path: Path,
    *,
    root: Path,
    allowed_tags: frozenset[str],
) -> LedgerView:
    return _read_ledger(
        path,
        root=root,
        allowed_tags=allowed_tags,
        allow_missing=False,
    )


def _append_bytes(path: Path, payload: bytes) -> None:
    with path.open("ab", buffering=0) as stream:
        remaining = memoryview(payload)
        while remaining:
            written = stream.write(remaining)
            if written is None or written <= 0:
                raise OSError("append write made no progress")
            remaining = remaining[written:]
        stream.flush()
        os.fsync(stream.fileno())


def _same_file_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino) == identity


def _lock_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise _fail(f"could not read append lock identity: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(path):
        raise _fail("append lock is not an owned ordinary directory")
    return metadata.st_dev, metadata.st_ino


def _write_lock_marker(marker: Path, token: str) -> None:
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise _fail(f"could not establish append lock ownership marker: {exc}") from exc


def _verify_lock_marker(marker: Path, token: str) -> None:
    if not marker.is_file() or _is_reparse(marker):
        raise _fail("owned append lock marker is missing or changed")
    try:
        marker_token = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _fail(f"could not verify append lock ownership marker: {exc}") from exc
    if marker_token != token + "\n":
        raise _fail("owned append lock marker content changed")


def _restore_unowned_release_path(release: Path, lock: Path) -> None:
    if not release.exists() or lock.exists():
        return
    try:
        release.rename(lock)
    except OSError as exc:
        raise _fail(f"could not restore a replaced append lock: {exc}") from exc


def _release_owned_lock(
    lock: Path,
    marker: Path,
    token: str,
    expected_identity: tuple[int, int] | None,
) -> None:
    current_identity = _lock_identity(lock)
    if expected_identity is not None and current_identity != expected_identity:
        raise _fail("owned append lock identity changed before cleanup")
    _verify_lock_marker(marker, token)

    release = lock.with_name(lock.name + ".release-" + token)
    if release.exists():
        raise _fail("private append lock release path already exists")
    try:
        lock.rename(release)
    except OSError as exc:
        raise _fail(f"could not isolate owned append lock for release: {exc}") from exc

    release_marker = release / marker.name
    try:
        isolated_identity = _lock_identity(release)
        if isolated_identity != current_identity:
            raise _fail("isolated append lock identity changed")
        _verify_lock_marker(release_marker, token)
    except BaseException:
        _restore_unowned_release_path(release, lock)
        raise

    try:
        entries = list(release.iterdir())
    except OSError as exc:
        raise _fail(f"could not inspect isolated append lock: {exc}") from exc
    if entries != [release_marker]:
        raise _fail("isolated append lock contains an unowned entry")

    try:
        release_marker.unlink()
    except OSError as exc:
        raise _fail(f"could not remove owned append lock marker: {exc}") from exc
    if not _same_file_identity(release, isolated_identity):
        raise _fail("isolated append lock identity changed after marker removal")
    try:
        has_entries = any(release.iterdir())
    except OSError as exc:
        raise _fail(f"could not inspect owned append lock: {exc}") from exc
    if has_entries:
        raise _fail("owned append lock is not empty at cleanup")
    if not _same_file_identity(release, isolated_identity):
        raise _fail("isolated append lock identity changed during cleanup")
    try:
        release.rmdir()
    except OSError as exc:
        raise _fail(f"could not release owned append lock: {exc}") from exc


def _release_created_lock_without_marker(
    lock: Path,
    marker: Path,
    token: str,
    expected_identity: tuple[int, int] | None,
) -> None:
    current_identity = _lock_identity(lock)
    if expected_identity is not None and current_identity != expected_identity:
        raise _fail("created append lock identity changed before cleanup")

    release = lock.with_name(lock.name + ".release-" + token)
    if release.exists():
        raise _fail("private append lock release path already exists")
    try:
        lock.rename(release)
    except OSError as exc:
        raise _fail(
            f"could not isolate created append lock for release: {exc}"
        ) from exc

    release_marker = release / marker.name
    try:
        isolated_identity = _lock_identity(release)
        if isolated_identity != current_identity:
            raise _fail("isolated append lock identity changed")
        try:
            entries = list(release.iterdir())
        except OSError as exc:
            raise _fail(f"could not inspect isolated append lock: {exc}") from exc
        if len(entries) > 1 or (entries and entries[0] != release_marker):
            raise _fail("isolated append lock contains an unowned entry")
        if entries:
            try:
                marker_metadata = os.lstat(release_marker)
            except OSError as exc:
                raise _fail(
                    f"could not inspect incomplete append lock marker: {exc}"
                ) from exc
            if not stat.S_ISREG(marker_metadata.st_mode) or _is_reparse(release_marker):
                raise _fail("incomplete append lock marker is not an ordinary file")
            try:
                release_marker.unlink()
            except OSError as exc:
                raise _fail(
                    f"could not remove incomplete append lock marker: {exc}"
                ) from exc
        if not _same_file_identity(release, isolated_identity):
            raise _fail("isolated append lock identity changed during cleanup")
        try:
            release.rmdir()
        except OSError as exc:
            raise _fail(f"could not release created append lock: {exc}") from exc
    except BaseException:
        _restore_unowned_release_path(release, lock)
        raise


def append_record(
    path: Path,
    record: Mapping[str, object],
    *,
    root: Path,
    allowed_tags: frozenset[str],
) -> dict[str, object]:
    ledger = _storage_path(path, root=root)
    if not ledger.parent.is_dir():
        raise _fail("ledger parent directory does not exist")
    lock = ledger.with_name(ledger.name + ".append-lock")
    try:
        token = secrets.token_hex(16)
    except Exception as exc:
        raise _fail(f"could not create append lock token: {exc}") from exc
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise _fail("ledger append lock is already held") from exc
    except OSError as exc:
        raise _fail(f"could not acquire ledger append lock: {exc}") from exc

    marker = lock / ("owner-" + token)
    lock_identity: tuple[int, int] | None = None
    marker_created = False
    primary_error: BaseException | None = None
    try:
        lock_identity = _lock_identity(lock)
        _write_lock_marker(marker, token)
        marker_created = True
        current = _read_ledger(
            ledger,
            root=root,
            allowed_tags=allowed_tags,
            allow_missing=True,
        )
        checked = validate_record(record, root=root, allowed_tags=allowed_tags)
        combined = [copy.deepcopy(row) for row in current.records]
        combined.append(checked)
        _fold_records(combined, root=root)

        if checked["lifecycle"] in {"candidate", "verified"}:
            issues = _record_reference_issues(checked, root=root)
            if issues:
                raise _fail("new record has unavailable public references")

        line = (canonical_json(checked) + "\n").encode("utf-8")
        try:
            _append_bytes(ledger, line)
        except OSError as exc:
            raise _fail(f"append failed: {exc}") from exc

        verified = load_ledger(ledger, root=root, allowed_tags=allowed_tags)
        try:
            tail_matches = ledger.read_bytes().endswith(line)
        except OSError as exc:
            raise _fail(f"could not verify appended ledger tail: {exc}") from exc
        if not tail_matches or not any(
            row["record_id"] == checked["record_id"] for row in verified.records
        ):
            raise _fail("appended ledger tail did not verify")
        return copy.deepcopy(checked)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if marker_created:
                _release_owned_lock(lock, marker, token, lock_identity)
            else:
                _release_created_lock_without_marker(lock, marker, token, lock_identity)
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            try:
                primary_error.add_note(f"append lock cleanup failed: {cleanup_error}")
            except AttributeError:
                pass
