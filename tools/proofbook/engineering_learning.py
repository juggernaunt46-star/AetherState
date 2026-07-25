"""Validate and query AetherState's developer-only engineering lesson ledger."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LEARNING_RELATIVE = Path("proofbook")
CORE_PATH = Path(__file__).resolve().parent / "engineering_learning_core.py"
GENESIS_SCHEMA = "aetherstate/proofbook-genesis/1"
GENESIS_RECORD_COUNT = 37
GENESIS_PREFIX_SHA256 = (
    "sha256:11625153c0263c050e9e1d6873d6347f125c4b979df48f9e23c273aba76208da"
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class CliError(ValueError):
    """Raised when a CLI-owned contract is malformed."""


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CliError("input contains duplicate JSON object key")
        result[key] = value
    return result


@dataclass(frozen=True)
class BriefQuery:
    task: str
    paths: tuple[str, ...] = ()
    regression_nodes: tuple[str, ...] = ()
    owner_symbols: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    domain: str | None = None
    boundary: str | None = None
    failure_class: str | None = None


@dataclass(frozen=True)
class Match:
    record: dict[str, object]
    rank: tuple[int, int, int, int, int]
    reason: str


_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*:[a-z0-9][a-z0-9_]*$")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_GENERIC_TASK_TOKENS = frozenset(
    {
        "add",
        "change",
        "fix",
        "implement",
        "linux",
        "posix",
        "repair",
        "update",
        "windows",
    }
)
_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_DOMAINS = frozenset(
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
_CUBE_BOUNDARIES = frozenset(
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
_CUBE_FAILURE_CLASSES = frozenset(
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


def _normalized_phrase(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().strip().split())


def tokenize(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens = set(_TOKEN_RE.findall(normalized)) - _GENERIC_TASK_TOKENS
    return frozenset(sorted(tokens)[:64])


def _normalized_path(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/")
    return PurePosixPath(normalized).parts


def _brief_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CliError("brief path is invalid")
    normalized = unicodedata.normalize("NFKC", value).replace("\\", "/")
    if (
        normalized.startswith("/")
        or _DRIVE_RE.match(normalized)
        or "\x00" in normalized
    ):
        raise CliError("brief path is invalid")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CliError("brief path is invalid")
    return "/".join(parts)


def _brief_selector(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise CliError(f"brief {field} is invalid")
    return unicodedata.normalize("NFKC", value)


def validate_brief_query(
    query: BriefQuery,
    *,
    allowed_tags: frozenset[str],
) -> BriefQuery:
    """Return one bounded, normalized, non-persistent briefing query."""
    if (
        not isinstance(query.task, str)
        or not query.task.strip()
        or len(query.task.encode("utf-8")) > 4096
        or "\x00" in query.task
    ):
        raise CliError("brief task is invalid")
    tags = tuple(_brief_selector(tag, field="tag") for tag in query.tags)
    if any(tag not in allowed_tags for tag in tags):
        raise CliError("brief tag is not reviewed")
    if query.domain is not None and query.domain not in _DOMAINS:
        raise CliError("brief domain is invalid")
    if query.boundary is not None and query.boundary not in _CUBE_BOUNDARIES:
        raise CliError("brief boundary is invalid")
    if (
        query.failure_class is not None
        and query.failure_class not in _CUBE_FAILURE_CLASSES
    ):
        raise CliError("brief failure class is invalid")
    return BriefQuery(
        task=unicodedata.normalize("NFKC", query.task),
        paths=tuple(_brief_path(path) for path in query.paths),
        regression_nodes=tuple(
            _brief_selector(value, field="regression node")
            for value in query.regression_nodes
        ),
        owner_symbols=tuple(
            _brief_selector(value, field="owner symbol")
            for value in query.owner_symbols
        ),
        tags=tags,
        domain=query.domain,
        boundary=query.boundary,
        failure_class=query.failure_class,
    )


def _path_relation(owner: str, query: str) -> int:
    owner_parts = _normalized_path(owner)
    query_parts = _normalized_path(query)
    if owner_parts == query_parts:
        return 2
    shorter = min(len(owner_parts), len(query_parts))
    if shorter and owner_parts[:shorter] == query_parts[:shorter]:
        return 1
    return 0


def rank_lesson(
    record: dict[str, object],
    query: BriefQuery,
    *,
    aliases: dict[str, tuple[str, ...]],
) -> Match | None:
    owners = tuple(record.get("owners", ()))
    regressions = tuple(record.get("regressions", ()))
    exact_regression = any(
        isinstance(row, dict)
        and (
            str(row.get("node", "")) in query.regression_nodes
            or (
                f"{row.get('path', '')}::{row.get('node', '')}"
                in query.regression_nodes
            )
        )
        for row in regressions
    )
    exact_symbol = any(
        isinstance(row, dict) and str(row.get("symbol", "")) in query.owner_symbols
        for row in owners
    )
    exact_identity = int(exact_regression or exact_symbol)

    path_relation = 0
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        owner_path = owner.get("path")
        if not isinstance(owner_path, str):
            continue
        for query_path in query.paths:
            path_relation = max(
                path_relation,
                _path_relation(owner_path, query_path),
            )

    record_tags = {str(tag) for tag in record.get("tags", ()) if isinstance(tag, str)}
    normalized_task = _normalized_phrase(query.task)
    query_tags = set(query.tags)
    for tag, phrases in aliases.items():
        if any(
            phrase == normalized_task or f" {phrase} " in f" {normalized_task} "
            for phrase in phrases
        ):
            query_tags.add(tag)
    tag_count = min(8, len(record_tags & query_tags))

    diagnosis = record.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    classification_count = sum(
        (
            int(query.domain is not None and record.get("domain") == query.domain),
            int(
                query.boundary is not None
                and diagnosis.get("boundary") == query.boundary
            ),
            int(
                query.failure_class is not None
                and diagnosis.get("failure_class") == query.failure_class
            ),
        )
    )

    text_fields = [
        str(record.get("repair_rule", "")),
        str(record.get("symptom", "")),
        str(record.get("cause", "")),
    ]
    scope = record.get("scope")
    if isinstance(scope, dict):
        not_supported = scope.get("not_supported")
        if isinstance(not_supported, str):
            text_fields.append(not_supported)
    record_tokens = set()
    for field in text_fields:
        record_tokens.update(tokenize(field))
    token_count = min(32, len(tokenize(query.task) & record_tokens))

    rank = (
        exact_identity,
        path_relation,
        tag_count,
        classification_count,
        token_count,
    )
    if not any(rank):
        return None
    if exact_regression:
        reason = "exact regression node"
    elif exact_symbol:
        reason = "exact owner symbol"
    elif path_relation == 2:
        reason = "exact owner path"
    elif path_relation == 1:
        reason = "owner path component prefix"
    elif tag_count:
        reason = "reviewed tag or alias"
    elif classification_count:
        reason = "exact classification"
    else:
        reason = "bounded token overlap"
    return Match(record=record, rank=rank, reason=reason)


def parse_tag_registry(
    value: Any,
) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    """Return reviewed tag IDs and aliases from one strict registry."""
    if not isinstance(value, dict) or set(value) != {"schema", "tags"}:
        raise CliError("tag registry fields are invalid")
    if value["schema"] != "aetherstate/engineering-tags/1":
        raise CliError("tag registry schema is invalid")
    if not isinstance(value["tags"], list):
        raise CliError("tag registry tags must be a list")

    ids: set[str] = set()
    aliases: dict[str, tuple[str, ...]] = {}
    alias_owner: dict[str, str] = {}
    for index, row in enumerate(value["tags"]):
        if not isinstance(row, dict) or set(row) != {"id", "aliases"}:
            raise CliError(f"tag row {index} fields are invalid")
        tag_id = row["id"]
        raw_aliases = row["aliases"]
        if not isinstance(tag_id, str) or _TAG_RE.fullmatch(tag_id) is None:
            raise CliError(f"tag row {index} id is invalid")
        if unicodedata.normalize("NFKC", tag_id) != tag_id:
            raise CliError(f"tag row {index} id is not NFKC")
        if tag_id in ids:
            raise CliError(f"duplicate tag id {tag_id!r}")
        if not isinstance(raw_aliases, list) or not all(
            isinstance(alias, str) and _normalized_phrase(alias)
            for alias in raw_aliases
        ):
            raise CliError(f"tag {tag_id!r} aliases are invalid")
        if any(unicodedata.normalize("NFKC", alias) != alias for alias in raw_aliases):
            raise CliError(f"tag {tag_id!r} alias is not NFKC")

        normalized = tuple(_normalized_phrase(alias) for alias in raw_aliases)
        if len(set(normalized)) != len(normalized):
            raise CliError(f"tag {tag_id!r} has duplicate alias")
        for alias in normalized:
            owner = alias_owner.get(alias)
            if owner is not None:
                raise CliError(f"alias {alias!r} is shared by {owner!r} and {tag_id!r}")
            alias_owner[alias] = tag_id
        ids.add(tag_id)
        aliases[tag_id] = normalized

    for tag_id, normalized in aliases.items():
        for alias in normalized:
            if alias in ids:
                raise CliError(f"alias {alias!r} for {tag_id!r} collides with a tag id")

    return frozenset(ids), dict(sorted(aliases.items()))


def _load_core():
    name = "aetherstate_engineering_learning_core"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, CORE_PATH)
    if spec is None or spec.loader is None:
        raise CliError("could not load engineering-learning core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_tag_registry(
    path: Path,
) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read tag registry: {path}") from exc
    return parse_tag_registry(value)


def load_lesson_input(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError("cannot read lesson input") from exc
    try:
        value = json.loads(text, object_pairs_hook=_strict_json_pairs)
    except json.JSONDecodeError as exc:
        raise CliError("lesson input is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CliError("lesson input must be one JSON object")
    return value


def validate_genesis_prefix(ledger_path: Path, genesis_path: Path) -> None:
    try:
        genesis = json.loads(
            genesis_path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_pairs,
        )
        ledger = ledger_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("cannot read Proofbook genesis") from exc
    if not isinstance(genesis, dict) or set(genesis) != {
        "schema",
        "record_count",
        "prefix_sha256",
    }:
        raise CliError("Proofbook genesis fields are invalid")
    count = genesis["record_count"]
    digest = genesis["prefix_sha256"]
    if (
        genesis["schema"] != GENESIS_SCHEMA
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != GENESIS_RECORD_COUNT
        or not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or digest != GENESIS_PREFIX_SHA256
    ):
        raise CliError("Proofbook genesis prefix values are invalid")
    lines = ledger.splitlines(keepends=True)
    if len(lines) < count:
        raise CliError("Proofbook genesis prefix is missing")
    actual = "sha256:" + hashlib.sha256(b"".join(lines[:count])).hexdigest()
    if actual != digest:
        raise CliError("Proofbook genesis prefix does not match")


def build_brief(
    view: Any,
    query: BriefQuery,
    *,
    aliases: dict[str, tuple[str, ...]],
    limit: int = 5,
) -> str:
    matches = [
        match
        for record in view.active
        if (match := rank_lesson(record, query, aliases=aliases)) is not None
    ]
    matches.sort(
        key=lambda match: (
            *(-value for value in match.rank),
            str(match.record["lesson_key"]),
            int(match.record["revision"]),
        )
    )
    matches = matches[: max(0, min(limit, 5))]
    if not matches:
        lines = [
            "ENGINEERING LEARNING BRIEF",
            "No verified current lesson matched this task.",
        ]
    else:
        lines = ["ENGINEERING LEARNING BRIEF", f"Matches: {len(matches)}"]
        for index, match in enumerate(matches, start=1):
            record = match.record
            diagnosis = record["diagnosis"]
            scope = record["scope"]
            owners = sorted(
                (
                    str(row["path"])
                    + (f"::{row['symbol']}" if row.get("symbol") else "")
                    for row in record["owners"]
                )
            )
            regressions = sorted(
                f"{row['runner']}:{row['path']}::{row['node']}"
                for row in record["regressions"]
            )
            evidence = sorted(
                f"{row['kind']}:{row['path']}:{row['sha256']}"
                for row in record["evidence"]
            )
            lines.extend(
                [
                    "",
                    f"{index}. {record['lesson_key']} r{record['revision']}",
                    f"Matched: {match.reason}",
                    (
                        f"Boundary: {diagnosis['boundary']} / "
                        f"{diagnosis.get('failure_class') or 'not_applicable'}"
                    ),
                    f"Cause: {record['cause']}",
                    f"Rule: {record['repair_rule']}",
                    "Supported: " + str(scope["supported"]),
                    "Not supported: " + str(scope["not_supported"]),
                    "Owners: " + "; ".join(owners),
                    "Regressions: " + "; ".join(regressions),
                    "Evidence: " + "; ".join(evidence),
                    "Currentness: verified",
                ]
            )

    stale_ids = {
        str(issue.record_id)
        for issue in view.stale
        if getattr(issue, "record_id", None)
    }
    superseded_ids = {str(record["record_id"]) for record in view.superseded}
    historical: list[tuple[Match, str]] = []
    seen_ids: set[str] = set()
    for record in view.records:
        record_id = str(record.get("record_id", ""))
        if record_id in seen_ids:
            continue
        if record_id in stale_ids:
            status = "stale"
        elif record_id in superseded_ids:
            status = "superseded"
        else:
            continue
        match = rank_lesson(record, query, aliases=aliases)
        if match is None or not any(match.rank[:4]):
            continue
        seen_ids.add(record_id)
        historical.append((match, status))
    historical.sort(
        key=lambda item: (
            *(-value for value in item[0].rank),
            str(item[0].record["lesson_key"]),
            int(item[0].record["revision"]),
            item[1],
        )
    )
    if historical:
        lines.extend(["", "Historical warnings:"])
        for match, status in historical[:3]:
            lines.append(
                f"- {match.record['lesson_key']} r{match.record['revision']} "
                f"[{status}; {match.reason}]"
            )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=WORKSPACE_ROOT,
        help="AetherState workspace root",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate the complete canonical ledger")
    commands.add_parser("status", help="summarize current lesson lifecycle state")
    add = commands.add_parser("add", help="append one explicit JSON lesson")
    add.add_argument("--input", type=Path, required=True)
    brief = commands.add_parser("brief", help="render relevant verified lessons")
    brief.add_argument("--task", required=True)
    brief.add_argument("--path", dest="paths", action="append", default=[])
    brief.add_argument(
        "--regression-node",
        dest="regression_nodes",
        action="append",
        default=[],
    )
    brief.add_argument(
        "--owner-symbol",
        dest="owner_symbols",
        action="append",
        default=[],
    )
    brief.add_argument("--tag", dest="tags", action="append", default=[])
    brief.add_argument("--domain")
    brief.add_argument("--boundary")
    brief.add_argument("--failure-class")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.workspace_root.resolve()
    learning_root = root / LEARNING_RELATIVE
    ledger_path = learning_root / "LEDGER.jsonl"
    genesis_path = learning_root / "GENESIS.json"
    tags_path = learning_root / "TAGS.json"
    core = _load_core()
    try:
        allowed_tags, _aliases = load_tag_registry(tags_path)
        view = core.load_ledger(
            ledger_path,
            root=root,
            allowed_tags=allowed_tags,
        )
        validate_genesis_prefix(ledger_path, genesis_path)
        if args.command == "add":
            value = load_lesson_input(args.input)
            if "record_id" not in value:
                value["record_id"] = core.compute_record_id(value)
            appended = core.append_record(
                ledger_path,
                value,
                root=root,
                allowed_tags=allowed_tags,
            )
            print(appended["record_id"])
            return 0

        if args.command == "validate":
            if view.stale:
                for issue in view.stale:
                    print(
                        f"{issue.record_id} {issue.field}: {issue.detail}",
                        file=sys.stderr,
                    )
                return 1
            print(f"valid records={len(view.records)}")
            return 0
        if args.command == "status":
            stale_records = {
                str(issue.record_id)
                for issue in view.stale
                if getattr(issue, "record_id", None)
            }
            print(
                f"records={len(view.records)} "
                f"active={len(view.active)} "
                f"candidates={len(view.candidates)} "
                f"superseded={len(view.superseded)} "
                f"invalidated={len(view.invalidated)} "
                f"stale={len(stale_records)}"
            )
            return 0
        if args.command == "brief":
            query = validate_brief_query(
                BriefQuery(
                    task=args.task,
                    paths=tuple(args.paths),
                    regression_nodes=tuple(args.regression_nodes),
                    owner_symbols=tuple(args.owner_symbols),
                    tags=tuple(args.tags),
                    domain=args.domain,
                    boundary=args.boundary,
                    failure_class=args.failure_class,
                ),
                allowed_tags=allowed_tags,
            )
            print(build_brief(view, query, aliases=_aliases), end="")
            return 0
        raise CliError(f"unsupported command {args.command!r}")
    except (
        CliError,
        core.LedgerError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
