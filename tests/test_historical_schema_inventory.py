from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

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
