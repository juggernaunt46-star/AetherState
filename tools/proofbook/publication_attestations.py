"""Seal and validate reviewed snapshots of the complete public Proofbook ledger."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import engineering_learning as lesson_cli
import engineering_learning_core as core


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
ATTESTATION_SCHEMA = "aetherstate/proofbook-publication-attestation/1"
REVIEW_SCHEMA = "aetherstate/proofbook-publication-review/1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ATTESTATION_FIELDS = frozenset(
    {
        "schema",
        "sequence",
        "attestation_id",
        "previous_attestation_id",
        "ledger_record_count",
        "ledger_sha256",
        "added_record_ids",
        "review_artifact",
    }
)
_REVIEW_FIELDS = frozenset(
    {
        "schema",
        "ledger_record_count",
        "ledger_sha256",
        "public_artifact",
        "engineering",
        "privacy",
    }
)


class AttestationError(ValueError):
    """Raised when a publication attestation contract is not satisfied."""


@dataclass(frozen=True)
class LedgerSnapshot:
    payload: bytes
    lines: tuple[bytes, ...]
    record_ids: tuple[str, ...]


def _fail(message: str) -> AttestationError:
    return AttestationError(message)


def _strict_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("publication attestation contains duplicate JSON key")
        result[key] = value
    return result


def _reject_float(value: str) -> object:
    raise _fail(f"publication attestation JSON floats are forbidden: {value}")


def _reject_constant(value: str) -> object:
    raise _fail(f"publication attestation JSON constant is forbidden: {value}")


def _parse_object(payload: bytes, *, field: str) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(f"{field} is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_json_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except AttestationError:
        raise
    except json.JSONDecodeError as exc:
        raise _fail(f"{field} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise _fail(f"{field} must be one JSON object")
    return value


def _exact_fields(
    value: dict[str, object],
    expected: frozenset[str],
    *,
    field: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise _fail(f"{field} fields are invalid")


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _fail(f"{field} must be a lowercase sha256 identity")
    return value


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _content_id(value: dict[str, object], *, field: str) -> str:
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    return _digest(core.canonical_json(payload).encode("utf-8"))


def _canonical_object_file(path: Path, *, field: str) -> dict[str, object]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read {field}") from exc
    if not payload.endswith(b"\n"):
        raise _fail(f"{field} must end with one LF")
    value = _parse_object(payload[:-1], field=field)
    expected = (core.canonical_json(value) + "\n").encode("utf-8")
    if payload != expected:
        raise _fail(f"{field} is not canonical JSON")
    return value


def _positive_count(value: object, *, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise _fail(f"{field} must be a positive integer")
    return value


def _approved(value: object, *, field: str) -> None:
    if not isinstance(value, dict):
        raise _fail(f"{field} must be an approval object")
    _exact_fields(
        value,
        frozenset({"status", "reviewer"}),
        field=field,
    )
    if value["status"] != "approved":
        raise _fail(f"{field} must be approved")
    try:
        core._bounded_identifier(  # noqa: SLF001
            value["reviewer"],
            field=f"{field}.reviewer",
            maximum=64,
        )
    except core.LedgerError as exc:
        raise _fail(f"{field}.reviewer is invalid") from exc


def _public_artifact(value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 2048
        or any(character.isspace() for character in value)
    ):
        raise _fail("public review artifact must be a bounded HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _fail("public review artifact must be a bounded HTTPS URL")


def _review_path(root: Path, value: object) -> tuple[str, Path]:
    try:
        relative = core._relative_path(  # noqa: SLF001
            value,
            root=root,
            field="review_artifact.path",
        )
    except core.LedgerError as exc:
        raise _fail("publication review artifact path is invalid") from exc
    pure = PurePosixPath(relative)
    if (
        pure.parts[:2] != ("proofbook", "reviews")
        or pure.suffix != ".json"
    ):
        raise _fail("publication review artifact must be under proofbook/reviews")
    path = root / pure
    if not path.is_file():
        raise _fail("publication review artifact is unavailable")
    return relative, path


def _validate_review(
    value: dict[str, object],
    *,
    record_count: int,
    ledger_sha256: str,
) -> None:
    _exact_fields(value, _REVIEW_FIELDS, field="publication review")
    if value["schema"] != REVIEW_SCHEMA:
        raise _fail("publication review schema is not supported")
    if value["ledger_record_count"] != record_count:
        raise _fail("publication review record count does not match")
    if value["ledger_sha256"] != ledger_sha256:
        raise _fail("publication review ledger hash does not match")
    _sha256(value["ledger_sha256"], field="publication review ledger_sha256")
    _public_artifact(value["public_artifact"])
    _approved(value["engineering"], field="engineering review")
    _approved(value["privacy"], field="privacy review")


def _load_ledger(root: Path) -> LedgerSnapshot:
    proofbook = root / "proofbook"
    ledger_path = proofbook / "LEDGER.jsonl"
    allowed_tags, _aliases = lesson_cli.load_tag_registry(
        proofbook / "TAGS.json"
    )
    view = core.load_ledger(
        ledger_path,
        root=root,
        allowed_tags=allowed_tags,
    )
    lesson_cli.validate_genesis_prefix(
        ledger_path,
        proofbook / "GENESIS.json",
    )
    if view.stale:
        raise _fail("publication attestation cannot cover stale lessons")
    if view.candidates:
        raise _fail("publication attestation cannot cover candidate lessons")
    payload = ledger_path.read_bytes()
    lines = tuple(payload.splitlines(keepends=True))
    record_ids: list[str] = []
    for index, line in enumerate(lines, start=1):
        value = _parse_object(
            line[:-1],
            field=f"ledger line {index}",
        )
        record_id = value.get("record_id")
        _sha256(record_id, field=f"ledger line {index} record_id")
        record_ids.append(str(record_id))
    if len(record_ids) != len(view.records):
        raise _fail("publication attestation ledger count is inconsistent")
    return LedgerSnapshot(
        payload=payload,
        lines=lines,
        record_ids=tuple(record_ids),
    )


def _ledger_storage_path(root: Path) -> Path:
    try:
        return core._storage_path(  # noqa: SLF001
            root / "proofbook" / "LEDGER.jsonl",
            root=root,
        )
    except core.LedgerError as exc:
        raise _fail(f"public ledger storage path is invalid: {exc}") from exc


def _attestation_storage_path(root: Path) -> Path:
    try:
        return core._storage_path(  # noqa: SLF001
            root / "proofbook" / "ATTESTATIONS.jsonl",
            root=root,
        )
    except core.LedgerError as exc:
        raise _fail(
            f"publication attestation chain path is invalid: {exc}"
        ) from exc


@contextmanager
def _owned_append_lock(storage: Path, *, field: str) -> Iterator[None]:
    if not storage.parent.is_dir():
        raise _fail(f"{field} parent directory does not exist")
    lock = storage.with_name(storage.name + ".append-lock")
    try:
        token = secrets.token_hex(16)
    except Exception as exc:
        raise _fail(f"could not create {field} append lock token") from exc
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise _fail(f"{field} append lock is already held") from exc
    except OSError as exc:
        raise _fail(f"cannot acquire {field} append lock") from exc

    marker = lock / ("owner-" + token)
    lock_identity: tuple[int, int] | None = None
    marker_created = False
    primary_error: BaseException | None = None
    try:
        lock_identity = core._lock_identity(lock)  # noqa: SLF001
        core._write_lock_marker(marker, token)  # noqa: SLF001
        marker_created = True
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if marker_created:
                core._release_owned_lock(  # noqa: SLF001
                    lock,
                    marker,
                    token,
                    lock_identity,
                )
            else:
                core._release_created_lock_without_marker(  # noqa: SLF001
                    lock,
                    marker,
                    token,
                    lock_identity,
                )
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            try:
                primary_error.add_note(
                    f"{field} append lock cleanup failed: {cleanup_error}"
                )
            except AttributeError:
                pass


def _append_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    starting_size = os.fstat(descriptor).st_size
    identity = (
        os.fstat(descriptor).st_dev,
        os.fstat(descriptor).st_ino,
    )
    primary_error: BaseException | None = None
    try:
        if not core._same_file_identity(path, identity):  # noqa: SLF001
            raise _fail(
                "publication attestation chain identity changed before append"
            )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written is None or written <= 0:
                raise _fail(
                    "publication attestation append write made no progress"
                )
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException as exc:
        primary_error = exc
        try:
            os.ftruncate(descriptor, starting_size)
            os.fsync(descriptor)
        except OSError as rollback_error:
            try:
                primary_error.add_note(
                    "publication attestation partial-write rollback failed: "
                    f"{rollback_error}"
                )
            except AttributeError:
                pass
        raise
    finally:
        os.close(descriptor)

    if not core._same_file_identity(path, identity):  # noqa: SLF001
        raise _fail(
            "publication attestation chain identity changed after append"
        )
    if path.stat().st_size != starting_size + len(payload):
        raise _fail("publication attestation append size did not verify")


def _read_attestations(
    root: Path,
    snapshot: LedgerSnapshot,
    *,
    require_complete: bool,
    allow_missing: bool = False,
) -> tuple[dict[str, object], ...]:
    path = _attestation_storage_path(root)
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        if allow_missing:
            return ()
        raise _fail("publication attestation chain is missing")
    except OSError as exc:
        raise _fail("cannot read publication attestation chain") from exc
    if not payload:
        if allow_missing:
            return ()
        raise _fail("publication attestation chain is empty")
    if not payload.endswith(b"\n"):
        raise _fail("publication attestation chain must end with LF")

    rows: list[dict[str, object]] = []
    previous_id: str | None = None
    previous_count = 0
    for sequence, line in enumerate(payload.splitlines(keepends=True), start=1):
        if line == b"\n":
            raise _fail("publication attestation chain contains a blank line")
        value = _parse_object(
            line[:-1],
            field=f"publication attestation line {sequence}",
        )
        if line != (core.canonical_json(value) + "\n").encode("utf-8"):
            raise _fail("publication attestation line is not canonical JSON")
        _exact_fields(
            value,
            _ATTESTATION_FIELDS,
            field="publication attestation",
        )
        if value["schema"] != ATTESTATION_SCHEMA:
            raise _fail("publication attestation schema is not supported")
        if value["sequence"] != sequence:
            raise _fail("publication attestation sequence is not contiguous")
        attestation_id = _sha256(
            value["attestation_id"],
            field="attestation_id",
        )
        if attestation_id != _content_id(value, field="attestation_id"):
            raise _fail(
                "publication attestation id does not match canonical bytes"
            )
        if value["previous_attestation_id"] != previous_id:
            raise _fail("previous publication attestation does not match")

        record_count = _positive_count(
            value["ledger_record_count"],
            field="ledger_record_count",
        )
        if (
            record_count <= previous_count
            or record_count > len(snapshot.record_ids)
        ):
            raise _fail("publication attestation record count is invalid")
        ledger_sha256 = _sha256(
            value["ledger_sha256"],
            field="ledger_sha256",
        )
        prefix = b"".join(snapshot.lines[:record_count])
        if ledger_sha256 != _digest(prefix):
            raise _fail("publication attestation ledger prefix does not match")

        added = value["added_record_ids"]
        if not isinstance(added, list):
            raise _fail("publication attestation added_record_ids is invalid")
        expected_added = list(
            snapshot.record_ids[previous_count:record_count]
        )
        if added != expected_added:
            raise _fail(
                "publication attestation added_record_ids does not match ledger"
            )

        review_reference = value["review_artifact"]
        if not isinstance(review_reference, dict):
            raise _fail("publication review artifact reference is invalid")
        _exact_fields(
            review_reference,
            frozenset({"path", "sha256"}),
            field="publication review artifact",
        )
        relative, review_path = _review_path(
            root,
            review_reference["path"],
        )
        if review_reference["path"] != relative:
            raise _fail("publication review artifact path is not canonical")
        expected_review_hash = _sha256(
            review_reference["sha256"],
            field="review_artifact.sha256",
        )
        if _digest(review_path.read_bytes()) != expected_review_hash:
            raise _fail("publication review artifact hash does not match")
        review = _canonical_object_file(
            review_path,
            field="publication review artifact",
        )
        _validate_review(
            review,
            record_count=record_count,
            ledger_sha256=ledger_sha256,
        )

        rows.append(value)
        previous_id = attestation_id
        previous_count = record_count

    if require_complete and previous_count != len(snapshot.record_ids):
        raise _fail(
            "latest publication attestation does not cover complete ledger"
        )
    return tuple(rows)


def validate_publication(root: Path) -> tuple[int, int]:
    resolved_root = root.resolve(strict=True)
    ledger = _ledger_storage_path(resolved_root)
    with _owned_append_lock(ledger, field="public ledger"):
        snapshot = _load_ledger(resolved_root)
        attestations = _read_attestations(
            resolved_root,
            snapshot,
            require_complete=True,
        )
        return len(attestations), len(snapshot.record_ids)


def _resolve_review_input(root: Path, value: Path) -> tuple[str, Path]:
    candidate = value if value.is_absolute() else root / value
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError) as exc:
        raise _fail("publication review input must be inside the workspace") from exc
    return _review_path(root, relative)


def _append_attestation(root: Path, review_input: Path) -> str:
    resolved_root = root.resolve(strict=True)
    ledger = _ledger_storage_path(resolved_root)
    path = _attestation_storage_path(resolved_root)
    with _owned_append_lock(ledger, field="public ledger"):
        with _owned_append_lock(path, field="publication attestation"):
            snapshot = _load_ledger(resolved_root)
            existing = _read_attestations(
                resolved_root,
                snapshot,
                require_complete=False,
                allow_missing=True,
            )
            previous_count = (
                int(existing[-1]["ledger_record_count"]) if existing else 0
            )
            if previous_count == len(snapshot.record_ids):
                raise _fail("no unattested ledger records are available")
            review_relative, review_path = _resolve_review_input(
                resolved_root,
                review_input,
            )
            review = _canonical_object_file(
                review_path,
                field="publication review artifact",
            )
            ledger_sha256 = _digest(snapshot.payload)
            _validate_review(
                review,
                record_count=len(snapshot.record_ids),
                ledger_sha256=ledger_sha256,
            )
            attestation: dict[str, Any] = {
                "schema": ATTESTATION_SCHEMA,
                "sequence": len(existing) + 1,
                "attestation_id": "sha256:" + ("0" * 64),
                "previous_attestation_id": (
                    existing[-1]["attestation_id"] if existing else None
                ),
                "ledger_record_count": len(snapshot.record_ids),
                "ledger_sha256": ledger_sha256,
                "added_record_ids": list(
                    snapshot.record_ids[previous_count:]
                ),
                "review_artifact": {
                    "path": review_relative,
                    "sha256": _digest(review_path.read_bytes()),
                },
            }
            attestation["attestation_id"] = _content_id(
                attestation,
                field="attestation_id",
            )
            line = (core.canonical_json(attestation) + "\n").encode("utf-8")
            _append_bytes(path, line)
            current_snapshot = _load_ledger(resolved_root)
            _read_attestations(
                resolved_root,
                current_snapshot,
                require_complete=True,
            )
            return str(attestation["attestation_id"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=WORKSPACE_ROOT,
        help="AetherState workspace root",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "validate",
        help="validate the complete reviewed publication chain",
    )
    attest = commands.add_parser(
        "attest",
        help="append a seal from one canonical public review artifact",
    )
    attest.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = args.workspace_root.resolve(strict=True)
        if args.command == "validate":
            attestations, records = validate_publication(root)
            print(f"valid attestations={attestations} records={records}")
            return 0
        if args.command == "attest":
            print(_append_attestation(root, args.input))
            return 0
        raise _fail(f"unsupported command {args.command!r}")
    except (
        AttestationError,
        lesson_cli.CliError,
        core.LedgerError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
