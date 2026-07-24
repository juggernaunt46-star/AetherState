from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from tools import capture_schema_contract

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/hardening/post-1.24/database-schema-inventory.json"
SCHEMA_DIR = ROOT / "tests/fixtures/hardening/schema-history"
BASELINES = {
    "1.0.0-release-2cd07ef",
    "1.1.0-release-ed63e38",
    "1.22.0-release-9091614",
    "1.23.0-release-1f4aad0",
    "1.23.0-final-34dfe8f",
    "1.24.0-release-fdf71e2",
}


def test_every_changelog_release_has_an_evidence_backed_disposition() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert inventory["schema"] == "aetherstate-database-schema-inventory/1"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = {
        match.group(1)
        for match in re.finditer(r"^##\s+([0-9]+\.[0-9]+\.[0-9]+)\b", changelog, re.MULTILINE)
    }
    assert set(inventory["release_versions"]) == versions
    assert set(inventory["baselines"]) == BASELINES
    for row in inventory["release_versions"].values():
        assert row["status"] in {"PASS", "HOLD"}
        if row["status"] == "PASS":
            assert row["disposition"] in {
                "schema-baseline",
                "proven-identical-alias",
                "not-separately-built",
            }
            assert row["reason_code"] in {
                "captured_distinct_schema",
                "identical_core_and_full_start",
                "no_separate_public_build",
            }
        else:
            assert row["disposition"] == "unreconstructed-claimed-build"
            assert row["reason_code"] in {
                "claimed_public_build_unreconstructed",
                "additional_public_build_requires_capture",
            }


def test_inventory_has_no_unresolved_support_hold() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert not [
        version
        for version, row in inventory["release_versions"].items()
        if row["status"] == "HOLD"
    ]


def test_distinct_schema_fixtures_are_content_free_and_recreatable(tmp_path: Path) -> None:
    for baseline in sorted(BASELINES):
        for shape in ("core", "full-start"):
            fixture_path = SCHEMA_DIR / baseline / f"{shape}.schema.json"
            sql_path = SCHEMA_DIR / baseline / f"{shape}.schema.sql"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            assert fixture["schema"] == "aetherstate-sqlite-schema/1"
            assert fixture["baseline_id"] == baseline
            assert fixture["shape"] == shape
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", fixture["fingerprint"])
            assert fixture["objects"]
            assert fixture["tables"]
            assert sql_path.read_text(encoding="utf-8").strip()
            db = sqlite3.connect(tmp_path / f"{baseline}-{shape}.db")
            for kind in ("table", "index", "trigger", "view"):
                for row in fixture["objects"]:
                    if row["type"] == kind:
                        db.execute(row["sql"])
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            db.close()


def test_aliases_name_an_existing_distinct_baseline() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    for row in inventory["release_versions"].values():
        if row["status"] == "PASS":
            assert row["baseline_id"] in BASELINES
    for alias in inventory["aliases"]:
        assert alias["baseline_id"] in BASELINES
        assert len(alias["commit"]) == 40


def _assert_inventory_relationships(inventory: dict[str, object]) -> None:
    assert set(inventory["baselines"]) == BASELINES
    assert inventory["personal_data_read"] is False
    hash_pattern = re.compile(r"[0-9a-f]{40}")
    fingerprint_pattern = re.compile(r"sha256:[0-9a-f]{64}")
    version_pattern = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

    def assert_hash(value: object) -> None:
        assert isinstance(value, str)
        assert hash_pattern.fullmatch(value)

    def assert_fingerprint(value: object) -> None:
        assert isinstance(value, str)
        assert fingerprint_pattern.fullmatch(value)

    fixture_contracts = {}
    for baseline_id, baseline in inventory["baselines"].items():
        assert_hash(baseline["commit"])
        assert version_pattern.fullmatch(baseline["version"])
        fixtures = {}
        for shape in ("core", "full-start"):
            fixture_path = SCHEMA_DIR / baseline_id / f"{shape}.schema.json"
            sql_path = SCHEMA_DIR / baseline_id / f"{shape}.schema.sql"
            evidence = baseline["fixtures"][shape]
            assert evidence["path"] == fixture_path.relative_to(ROOT).as_posix()
            assert evidence["sql_path"] == sql_path.relative_to(ROOT).as_posix()
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            assert fixture["baseline_id"] == baseline_id
            assert fixture["shape"] == shape
            assert_hash(fixture["source_commit"])
            assert_fingerprint(fixture["fingerprint"])
            assert_fingerprint(evidence["fingerprint"])
            assert evidence["fingerprint"] == fixture["fingerprint"]
            assert baseline["commit"] == fixture["source_commit"]
            assert baseline["source_blobs"] == fixture["source_blobs"]
            for source in baseline["source_blobs"]:
                assert set(source) == {"path", "blob"}
                assert source["path"]
                assert not Path(source["path"]).is_absolute()
                assert "\\" not in source["path"]
                assert ".." not in Path(source["path"]).parts
                assert_hash(source["blob"])
            fixtures[shape] = fixture
        assert fixtures["core"]["source_blobs"] == fixtures["full-start"][
            "source_blobs"
        ]
        fixture_contracts[baseline_id] = fixtures

    aliases_by_version = {}
    for alias in inventory["aliases"]:
        assert alias["version"] not in aliases_by_version
        aliases_by_version[alias["version"]] = alias
        assert alias["baseline_id"] in BASELINES
        assert_hash(alias["commit"])
        assert_fingerprint(alias["core_fingerprint"])
        assert_fingerprint(alias["full_start_fingerprint"])
        baseline_fixtures = fixture_contracts[alias["baseline_id"]]
        assert alias["core_fingerprint"] == baseline_fixtures["core"]["fingerprint"]
        assert (
            alias["full_start_fingerprint"]
            == baseline_fixtures["full-start"]["fingerprint"]
        )

    disposition_reasons = {
        "schema-baseline": "captured_distinct_schema",
        "proven-identical-alias": "identical_core_and_full_start",
        "not-separately-built": "no_separate_public_build",
        "unreconstructed-claimed-build": {
            "claimed_public_build_unreconstructed",
            "additional_public_build_requires_capture",
        },
    }
    referenced_baselines = set()
    for version, release in inventory["release_versions"].items():
        disposition = release["disposition"]
        expected_reason = disposition_reasons[disposition]
        if isinstance(expected_reason, set):
            assert release["reason_code"] in expected_reason
        else:
            assert release["reason_code"] == expected_reason
        assert release["baseline_id"] in BASELINES
        referenced_baselines.add(release["baseline_id"])

        if disposition == "schema-baseline":
            assert "commit" in release
            assert_hash(release["commit"])
            baseline = inventory["baselines"][release["baseline_id"]]
            assert release["commit"] == baseline["commit"]
        elif disposition == "proven-identical-alias":
            assert "commit" in release
            assert_hash(release["commit"])
            alias = aliases_by_version[version]
            assert release["commit"] == alias["commit"]
            assert release["baseline_id"] == alias["baseline_id"]
        elif disposition == "not-separately-built":
            assert "evidence" in release
            evidence = release["evidence"]
            assert set(evidence) == {
                "owning_cumulative_commit",
                "owning_pyproject_version",
                "matching_public_pyproject_version",
                "remote_tag",
                "github_release",
            }
            assert_hash(evidence["owning_cumulative_commit"])
            assert version_pattern.fullmatch(evidence["owning_pyproject_version"])
            assert evidence["matching_public_pyproject_version"] is False
            assert evidence["remote_tag"] is None
            assert evidence["github_release"] is None
            owner_alias = aliases_by_version[evidence["owning_pyproject_version"]]
            assert evidence["owning_cumulative_commit"] == owner_alias["commit"]
            assert release["baseline_id"] == owner_alias["baseline_id"]

        for additional in release.get("additional_baseline_ids", []):
            assert additional in BASELINES
            referenced_baselines.add(additional)

    assert referenced_baselines == BASELINES

    special_aliases = {
        "1.22.0": ("final_public_commit", "1.22.0-final"),
        "1.24.0": ("current_public_commit", "1.24.0-current"),
    }
    for version, (field, alias_version) in special_aliases.items():
        release = inventory["release_versions"][version]
        alias = aliases_by_version[alias_version]
        assert_hash(release[field])
        assert release[field] == alias["commit"]
        assert release["baseline_id"] == alias["baseline_id"]

    release_123 = inventory["release_versions"]["1.23.0"]
    assert set(release_123["additional_baseline_ids"]) == {
        "1.23.0-release-1f4aad0"
    }
    for shape in ("core", "full-start"):
        release_fingerprint = fixture_contracts["1.23.0-release-1f4aad0"][shape][
            "fingerprint"
        ]
        final_fingerprint = fixture_contracts["1.23.0-final-34dfe8f"][shape][
            "fingerprint"
        ]
        assert release_fingerprint != final_fingerprint

    public_evidence = inventory["public_evidence"]
    assert public_evidence["remote_tags"] == []
    releases = public_evidence["github_releases"]
    assert releases["endpoint"] == (
        "https://api.github.com/repos/juggernaunt46-star/"
        "AetherState/releases?per_page=100"
    )
    assert releases["http_status"] == 200
    assert releases["count"] == 0
    assert_fingerprint(releases["response_sha256"])
    for commit in public_evidence["archive_heads"].values():
        assert_hash(commit)


def _mutate_inventory(inventory: dict[str, object], mutation: str) -> None:
    aliases = {row["version"]: row for row in inventory["aliases"]}
    releases = inventory["release_versions"]
    baselines = inventory["baselines"]
    if mutation == "disposition_reason_pair":
        releases["1.2.0"]["reason_code"] = "captured_distinct_schema"
    elif mutation == "missing_disposition_commit":
        del releases["1.2.0"]["commit"]
    elif mutation == "alias_core_fingerprint":
        aliases["1.2.0"]["core_fingerprint"] = f"sha256:{'0' * 64}"
    elif mutation == "release_alias_crosslink":
        releases["1.2.0"]["commit"] = "0" * 40
    elif mutation == "fixture_path":
        baseline = baselines["1.0.0-release-2cd07ef"]
        baseline["fixtures"]["core"]["path"] = baseline["fixtures"]["full-start"]["path"]
    elif mutation == "fixture_fingerprint":
        baselines["1.0.0-release-2cd07ef"]["fixtures"]["core"][
            "fingerprint"
        ] = f"sha256:{'0' * 64}"
    elif mutation == "fixture_source_commit":
        baselines["1.0.0-release-2cd07ef"]["commit"] = "0" * 40
    elif mutation == "fixture_source_blob":
        baselines["1.0.0-release-2cd07ef"]["source_blobs"][0]["blob"] = "0" * 40
    elif mutation == "variants_equal":
        release = baselines["1.23.0-release-1f4aad0"]
        final = baselines["1.23.0-final-34dfe8f"]
        release["fixtures"]["core"]["fingerprint"] = final["fixtures"]["core"][
            "fingerprint"
        ]
        release["fixtures"]["full-start"]["fingerprint"] = final["fixtures"][
            "full-start"
        ]["fingerprint"]
    elif mutation == "not_built_exact_evidence":
        del releases["1.9.0"]["evidence"]["remote_tag"]
    elif mutation == "invalid_commit_hex":
        aliases["1.2.0"]["commit"] = "G" * 40
    elif mutation == "invalid_blob_hex":
        baselines["1.0.0-release-2cd07ef"]["source_blobs"][0]["blob"] = "A" * 40
    else:
        raise AssertionError(f"unknown mutation: {mutation}")


@pytest.mark.parametrize(
    "mutation",
    [
        "disposition_reason_pair",
        "missing_disposition_commit",
        "alias_core_fingerprint",
        "release_alias_crosslink",
        "fixture_path",
        "fixture_fingerprint",
        "fixture_source_commit",
        "fixture_source_blob",
        "variants_equal",
        "not_built_exact_evidence",
        "invalid_commit_hex",
        "invalid_blob_hex",
    ],
)
def test_inventory_gate_rejects_broken_evidence_relationships(mutation: str) -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(inventory)
    _mutate_inventory(tampered, mutation)

    with pytest.raises(AssertionError):
        _assert_inventory_relationships(tampered)


def test_inventory_evidence_relationships_are_bound() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    _assert_inventory_relationships(inventory)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("unexpected-contract-field", "outside the approved contract"),
        ("canonical-json", "fingerprint"),
        ("sibling-sql", "SQL projection"),
    ],
)
def test_check_rejects_tampered_expected_fixture(
    tmp_path: Path,
    tamper: str,
    message: str,
) -> None:
    baseline = "1.24.0-release-fdf71e2"
    source_dir = SCHEMA_DIR / baseline
    check_dir = tmp_path / baseline
    shutil.copytree(source_dir, check_dir)
    captures = {
        shape: json.loads(
            (source_dir / f"{shape}.schema.json").read_text(encoding="utf-8")
        )
        for shape in ("core", "full-start")
    }
    core_path = check_dir / "core.schema.json"
    core = json.loads(core_path.read_text(encoding="utf-8"))
    if tamper == "unexpected-contract-field":
        core["unexpected"] = True
        core_path.write_text(json.dumps(core), encoding="utf-8")
    elif tamper == "canonical-json":
        core["objects"][0]["sql"] += " "
        core_path.write_text(json.dumps(core), encoding="utf-8")
    elif tamper == "sibling-sql":
        sql_path = check_dir / "core.schema.sql"
        sql_path.write_text(
            sql_path.read_text(encoding="utf-8") + "-- tampered\n",
            encoding="utf-8",
        )
    else:
        raise AssertionError(f"unknown tamper: {tamper}")

    with pytest.raises(ValueError, match=message):
        capture_schema_contract._check_captures(check_dir, captures)
