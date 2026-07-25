from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_CLI = (
    ROOT / "tools" / "proofbook" / "publication_attestations.py"
)
ATTESTATIONS = ROOT / "proofbook" / "ATTESTATIONS.jsonl"
LEDGER = ROOT / "proofbook" / "LEDGER.jsonl"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _content_id(value: dict[str, Any], field: str) -> str:
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    return _sha256(_canonical(payload).encode("utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _run_cli(
    root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(
                root
                / "tools"
                / "proofbook"
                / "publication_attestations.py"
            ),
            "--workspace-root",
            str(root),
            *args,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _load_attestation_module() -> ModuleType:
    module_name = "_proofbook_publication_attestations_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ATTESTATION_CLI,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    tools_path = str(ATTESTATION_CLI.parent)
    if tools_path not in sys.path:
        sys.path.insert(0, tools_path)
    spec.loader.exec_module(module)
    return module


def _copy_public_tree(target: Path) -> None:
    shutil.copytree(ROOT / "proofbook", target / "proofbook")
    shutil.copytree(
        ROOT / "tools" / "proofbook",
        target / "tools" / "proofbook",
    )
    for record in _jsonl(LEDGER):
        for group in ("owners", "regressions", "evidence"):
            for reference in record[group]:
                relative = Path(reference["path"])
                destination = target / relative
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)


def _append_synthetic_verified_record(
    root: Path,
    *,
    suffix: str = "attestation_suffix",
) -> str:
    ledger = root / "proofbook" / "LEDGER.jsonl"
    records = _jsonl(ledger)
    record = copy.deepcopy(records[-1])
    record["lesson_key"] = f"tooling.proofbook.synthetic_{suffix}"
    record["revision"] = 1
    record["supersedes"] = None
    record["symptom"] = "A synthetic public suffix awaits a publication seal."
    record["cause"] = "The test appends one valid record after the reviewed head."
    record["repair_rule"] = "Attest the exact complete ledger before publication."
    record["rationale"] = "Synthetic clean-tree rolling-attestation regression."
    record["record_id"] = _content_id(record, "record_id")
    with ledger.open("ab") as stream:
        stream.write((_canonical(record) + "\n").encode("utf-8"))
    return str(record["record_id"])


def _write_review(
    root: Path,
    *,
    name: str,
    engineering_status: str = "approved",
) -> Path:
    ledger = root / "proofbook" / "LEDGER.jsonl"
    review = {
        "schema": "aetherstate/proofbook-publication-review/1",
        "ledger_record_count": len(_jsonl(ledger)),
        "ledger_sha256": _sha256(ledger.read_bytes()),
        "public_artifact": f"https://example.com/proofbook/reviews/{name}",
        "engineering": {
            "status": engineering_status,
            "reviewer": "public-maintainer",
        },
        "privacy": {
            "status": "approved",
            "reviewer": "public-maintainer",
        },
    }
    path = root / "proofbook" / "reviews" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _canonical(review) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_bootstrap_attestation_seals_complete_ledger_and_public_review() -> None:
    assert ATTESTATION_CLI.is_file()
    assert ATTESTATIONS.is_file()
    assert (ROOT / "proofbook" / "ATTESTATIONS.md").is_file()

    result = _run_cli(ROOT, "validate")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "valid attestations=1 records=40"

    raw = ATTESTATIONS.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw
    attestations = _jsonl(ATTESTATIONS)
    assert len(attestations) == 1
    attestation = attestations[0]
    assert set(attestation) == {
        "schema",
        "sequence",
        "attestation_id",
        "previous_attestation_id",
        "ledger_record_count",
        "ledger_sha256",
        "added_record_ids",
        "review_artifact",
    }
    assert attestation["schema"] == (
        "aetherstate/proofbook-publication-attestation/1"
    )
    assert attestation["sequence"] == 1
    assert attestation["previous_attestation_id"] is None
    assert attestation["ledger_record_count"] == 40
    assert attestation["ledger_record_count"] == len(_jsonl(LEDGER))
    assert attestation["ledger_sha256"] == _sha256(LEDGER.read_bytes())
    assert attestation["added_record_ids"] == [
        record["record_id"] for record in _jsonl(LEDGER)
    ]
    assert attestation["attestation_id"] == _content_id(
        attestation,
        "attestation_id",
    )
    assert raw == (_canonical(attestation) + "\n").encode("utf-8")

    review_reference = attestation["review_artifact"]
    review_path = ROOT / Path(review_reference["path"])
    assert review_path.is_file()
    assert review_reference["sha256"] == _sha256(review_path.read_bytes())
    review = json.loads(review_path.read_text(encoding="utf-8"))
    assert review["ledger_record_count"] == 40
    assert review["ledger_sha256"] == attestation["ledger_sha256"]
    assert review["engineering"] == {
        "status": "approved",
        "reviewer": "public-maintainer",
    }
    assert review["privacy"] == {
        "status": "approved",
        "reviewer": "public-maintainer",
    }
    assert review["public_artifact"].startswith("https://")


@pytest.mark.parametrize(
    "mutation",
    ("delete", "reorder", "rewrite", "unsealed_append"),
)
def test_attestation_rejects_post_genesis_history_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    clone = tmp_path / mutation
    clone.mkdir()
    _copy_public_tree(clone)
    ledger = clone / "proofbook" / "LEDGER.jsonl"
    lines = ledger.read_bytes().splitlines(keepends=True)
    if mutation == "delete":
        del lines[37]
    elif mutation == "reorder":
        lines[37], lines[38] = lines[38], lines[37]
    elif mutation == "rewrite":
        record = json.loads(lines[37])
        record["rationale"] = "Internally valid but unreviewed rewritten history."
        record["record_id"] = _content_id(record, "record_id")
        lines[37] = (_canonical(record) + "\n").encode("utf-8")
    else:
        _append_synthetic_verified_record(clone)
        lines = []
    if lines:
        ledger.write_bytes(b"".join(lines))

    result = _run_cli(clone, "validate")
    assert result.returncode == 1
    assert "publication attestation" in result.stderr


def test_attest_appends_exact_suffix_and_chain_link_in_clean_tree(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "rolling"
    clone.mkdir()
    _copy_public_tree(clone)
    new_record_id = _append_synthetic_verified_record(clone)
    review_path = _write_review(clone, name="rolling-review.json")

    result = _run_cli(clone, "attest", "--input", str(review_path))
    assert result.returncode == 0, result.stderr
    new_attestation_id = result.stdout.strip()

    attestations_path = clone / "proofbook" / "ATTESTATIONS.jsonl"
    attestations = _jsonl(attestations_path)
    assert len(attestations) == 2
    first, second = attestations
    assert second["sequence"] == 2
    assert second["previous_attestation_id"] == first["attestation_id"]
    assert second["attestation_id"] == new_attestation_id
    assert second["attestation_id"] == _content_id(
        second,
        "attestation_id",
    )
    assert second["added_record_ids"] == [new_record_id]
    assert second["ledger_record_count"] == 41
    ledger = clone / "proofbook" / "LEDGER.jsonl"
    assert second["ledger_sha256"] == _sha256(ledger.read_bytes())

    validated = _run_cli(clone, "validate")
    assert validated.returncode == 0, validated.stderr
    assert validated.stdout.strip() == "valid attestations=2 records=41"

    second["previous_attestation_id"] = "sha256:" + ("0" * 64)
    second["attestation_id"] = _content_id(second, "attestation_id")
    attestations_path.write_text(
        "\n".join(_canonical(item) for item in (first, second)) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    broken = _run_cli(clone, "validate")
    assert broken.returncode == 1
    assert "previous publication attestation" in broken.stderr


def test_attest_rejects_unapproved_review_without_writing(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "unapproved"
    clone.mkdir()
    _copy_public_tree(clone)
    _append_synthetic_verified_record(clone)
    review_path = _write_review(
        clone,
        name="pending-review.json",
        engineering_status="pending",
    )
    attestations_path = clone / "proofbook" / "ATTESTATIONS.jsonl"
    before = attestations_path.read_bytes()

    result = _run_cli(clone, "attest", "--input", str(review_path))
    assert result.returncode == 1
    assert "must be approved" in result.stderr
    assert attestations_path.read_bytes() == before


def test_review_artifact_change_breaks_the_bound_attestation(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "review-change"
    clone.mkdir()
    _copy_public_tree(clone)
    attestation = _jsonl(
        clone / "proofbook" / "ATTESTATIONS.jsonl"
    )[0]
    review_path = clone / Path(attestation["review_artifact"]["path"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["privacy"]["reviewer"] = "different-maintainer"
    review_path.write_text(
        _canonical(review) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _run_cli(clone, "validate")
    assert result.returncode == 1
    assert "review artifact hash does not match" in result.stderr


def test_attest_reloads_the_ledger_before_reporting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "racing-ledger"
    clone.mkdir()
    _copy_public_tree(clone)
    _append_synthetic_verified_record(clone)
    review_path = _write_review(clone, name="racing-ledger-review.json")
    module = _load_attestation_module()
    real_write = module.os.write
    raced = False

    def append_while_attesting(
        descriptor: int,
        payload: bytes | memoryview,
    ) -> int:
        nonlocal raced
        written = real_write(descriptor, payload)
        if not raced:
            raced = True
            _append_synthetic_verified_record(
                clone,
                suffix="concurrent_tail",
            )
        return written

    monkeypatch.setattr(module.os, "write", append_while_attesting)
    with pytest.raises(
        module.AttestationError,
        match="does not cover complete ledger",
    ):
        module._append_attestation(clone, review_path)

    assert not (
        clone / "proofbook" / "LEDGER.jsonl.append-lock"
    ).exists()
    assert not (
        clone / "proofbook" / "ATTESTATIONS.jsonl.append-lock"
    ).exists()


def test_validate_refuses_to_race_the_shared_ledger_append_lock(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "held-ledger-lock"
    clone.mkdir()
    _copy_public_tree(clone)
    lock = clone / "proofbook" / "LEDGER.jsonl.append-lock"
    lock.mkdir()

    result = _run_cli(clone, "validate")
    assert result.returncode == 1
    assert "public ledger append lock is already held" in result.stderr


def test_attest_retries_a_short_write_and_preserves_a_valid_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "short-write"
    clone.mkdir()
    _copy_public_tree(clone)
    _append_synthetic_verified_record(clone)
    review_path = _write_review(clone, name="short-write-review.json")
    module = _load_attestation_module()
    real_write = module.os.write
    shortened = False

    def short_write(
        descriptor: int,
        payload: bytes | memoryview,
    ) -> int:
        nonlocal shortened
        if not shortened and len(payload) > 1:
            shortened = True
            return real_write(descriptor, payload[: len(payload) // 2])
        return real_write(descriptor, payload)

    monkeypatch.setattr(module.os, "write", short_write)
    module._append_attestation(clone, review_path)

    result = _run_cli(clone, "validate")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "valid attestations=2 records=41"


def test_attest_rolls_back_a_partial_write_that_cannot_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "failed-short-write"
    clone.mkdir()
    _copy_public_tree(clone)
    _append_synthetic_verified_record(clone)
    review_path = _write_review(
        clone,
        name="failed-short-write-review.json",
    )
    attestations = clone / "proofbook" / "ATTESTATIONS.jsonl"
    before = attestations.read_bytes()
    module = _load_attestation_module()
    real_write = module.os.write
    calls = 0

    def stalled_write(
        descriptor: int,
        payload: bytes | memoryview,
    ) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(descriptor, payload[: len(payload) // 2])
        return 0

    monkeypatch.setattr(module.os, "write", stalled_write)
    with pytest.raises(
        module.AttestationError,
        match="append write made no progress",
    ):
        module._append_attestation(clone, review_path)

    assert attestations.read_bytes() == before


def test_chain_path_must_not_be_a_symlink(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "linked-chain"
    clone.mkdir()
    _copy_public_tree(clone)
    attestations = clone / "proofbook" / "ATTESTATIONS.jsonl"
    outside = tmp_path / "outside-attestations.jsonl"
    outside.write_bytes(attestations.read_bytes())
    attestations.unlink()
    try:
        attestations.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable for this test account")

    before = outside.read_bytes()
    validation = _run_cli(clone, "validate")
    assert validation.returncode == 1
    assert "symlink or reparse point" in validation.stderr

    _append_synthetic_verified_record(clone)
    review_path = _write_review(clone, name="linked-chain-review.json")
    append = _run_cli(clone, "attest", "--input", str(review_path))
    assert append.returncode == 1
    assert "symlink or reparse point" in append.stderr
    assert outside.read_bytes() == before


def test_chain_path_reparse_detection_is_enforced_without_os_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clone = tmp_path / "simulated-reparse-chain"
    clone.mkdir()
    _copy_public_tree(clone)
    chain = clone / "proofbook" / "ATTESTATIONS.jsonl"
    module = _load_attestation_module()
    real_is_reparse = module.core._is_reparse

    def simulated_reparse(path: Path) -> bool:
        if path == chain:
            return True
        return real_is_reparse(path)

    monkeypatch.setattr(
        module.core,
        "_is_reparse",
        simulated_reparse,
    )
    with pytest.raises(
        module.AttestationError,
        match="symlink or reparse point",
    ):
        module.validate_publication(clone)
