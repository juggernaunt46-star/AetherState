from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_ID = "aetherstate-sqlite-schema/1"
SHAPES = ("core", "full-start")
DDL_OWNER_PATTERN = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b",
    re.IGNORECASE,
)
WRITE_SQL_PATTERN = re.compile(r"^(?:INSERT|REPLACE)\b", re.IGNORECASE)
DDL_SQL_PATTERN = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\b",
    re.IGNORECASE,
)


def _run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _resolve_commit(root: Path, git_ref: str) -> str:
    commit = _run_git(root, "rev-parse", "--verify", f"{git_ref}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"Git ref did not resolve to an exact commit object: {git_ref}")
    _run_git(root, "cat-file", "-e", f"{commit}^{{commit}}")
    return commit


def _safe_extract_git_archive(archive_path: Path, export_dir: Path) -> None:
    export_root = export_dir.resolve()
    with tarfile.open(archive_path, mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            posix_path = PurePosixPath(member.name)
            if (
                not member.name
                or posix_path.is_absolute()
                or ".." in posix_path.parts
                or "\\" in member.name
            ):
                raise ValueError(f"Unsafe Git archive member: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Unsupported Git archive member: {member.name!r}")
            destination = (export_dir / Path(*posix_path.parts)).resolve()
            if export_root not in destination.parents and destination != export_root:
                raise ValueError(f"Git archive member escapes export root: {member.name!r}")

        for member in members:
            destination = export_dir / Path(*PurePosixPath(member.name).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Git archive file has no content: {member.name!r}")
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)


def _schema_owner_paths(export_dir: Path) -> list[str]:
    owners: list[str] = []
    source_root = export_dir / "src"
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if DDL_OWNER_PATTERN.search(text):
            owners.append(path.relative_to(export_dir).as_posix())
    if not owners:
        raise ValueError("No public schema-owner source files were found")
    return owners


def _source_blobs(root: Path, commit: str, owner_paths: list[str]) -> list[dict[str, str]]:
    blobs = []
    for path in owner_paths:
        blob = _run_git(root, "rev-parse", "--verify", f"{commit}:{path}")
        if not re.fullmatch(r"[0-9a-f]{40}", blob):
            raise ValueError(f"Schema owner did not resolve to a blob: {path}")
        blobs.append({"path": path, "blob": blob})
    return blobs


def _child_environment(export_dir: Path, private_root: Path) -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "WINDIR",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(
        {
            "HOME": str(private_root),
            "USERPROFILE": str(private_root),
            "LOCALAPPDATA": str(private_root / "local"),
            "APPDATA": str(private_root / "roaming"),
            "TEMP": str(private_root / "temp"),
            "TMP": str(private_root / "temp"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(export_dir / "src"),
        }
    )
    for path in (private_root / "local", private_root / "roaming", private_root / "temp"):
        path.mkdir(parents=True, exist_ok=True)
    return env


def _capture_exported_revision(
    root: Path,
    commit: str,
    baseline_id: str,
) -> dict[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="aetherstate-schema-capture-") as raw_temp:
        temp_root = Path(raw_temp)
        archive_path = temp_root / "revision.tar"
        export_dir = temp_root / "revision"
        capture_dir = temp_root / "capture"
        private_root = temp_root / "private"
        export_dir.mkdir()
        capture_dir.mkdir()
        _run_git(
            root,
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            commit,
        )
        _safe_extract_git_archive(archive_path, export_dir)
        owner_paths = _schema_owner_paths(export_dir)
        source_blobs = _source_blobs(root, commit, owner_paths)

        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                "--capture-dir",
                str(capture_dir),
            ],
            cwd=export_dir,
            env=_child_environment(export_dir, private_root),
            check=True,
        )

        captures: dict[str, dict[str, Any]] = {}
        for shape in SHAPES:
            child_payload = json.loads(
                (capture_dir / f"{shape}.json").read_text(encoding="utf-8")
            )
            payload = {
                "schema": SCHEMA_ID,
                "baseline_id": baseline_id,
                "shape": shape,
                "source_commit": commit,
                "source_blobs": source_blobs,
                "fingerprint": child_payload["fingerprint"],
                "diagnostics": child_payload["diagnostics"],
                "objects": child_payload["objects"],
                "tables": child_payload["tables"],
            }
            _validate_contract(payload)
            captures[shape] = payload
        return captures


def _normalize_sql(sql: str) -> str:
    without_guard = re.sub(
        r"\bIF\s+NOT\s+EXISTS\b",
        "",
        sql,
        flags=re.IGNORECASE,
    )
    return " ".join(without_guard.strip().rstrip(";").split())


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _rows(
    db: sqlite3.Connection,
    pragma: str,
    columns: tuple[str, ...],
) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in db.execute(pragma).fetchall()]


def _canonical_fingerprint(payload: dict[str, Any]) -> str:
    canonical = {
        "objects": payload["objects"],
        "tables": payload["tables"],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sql_projection(payload: dict[str, Any]) -> str:
    rows = [
        row["sql"]
        for kind in ("table", "index", "trigger", "view")
        for row in payload["objects"]
        if row["type"] == kind
    ]
    return ";\n".join(rows) + ";\n"


def _capture_connection(db: sqlite3.Connection) -> dict[str, Any]:
    object_rows = db.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    objects = [
        {
            "type": row[0],
            "name": row[1],
            "table_name": row[2],
            "sql": _normalize_sql(row[3]),
        }
        for row in object_rows
    ]
    objects.sort(
        key=lambda row: (row["type"], row["name"], row["table_name"], row["sql"])
    )

    table_names = sorted(row["name"] for row in objects if row["type"] == "table")
    tables = []
    for table_name in table_names:
        quoted = _quote_identifier(table_name)
        if db.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
            raise ValueError(f"Schema capture refuses nonempty user table: {table_name}")

        columns = _rows(
            db,
            f"PRAGMA table_xinfo({quoted})",
            ("cid", "name", "type", "notnull", "default", "pk", "hidden"),
        )
        columns.sort(key=lambda row: (row["cid"], row["name"]))
        foreign_keys = _rows(
            db,
            f"PRAGMA foreign_key_list({quoted})",
            (
                "id",
                "seq",
                "table",
                "from",
                "to",
                "on_update",
                "on_delete",
                "match",
            ),
        )
        foreign_keys.sort(
            key=lambda row: (
                row["id"],
                row["seq"],
                row["table"],
                row["from"],
                row["to"] or "",
            )
        )
        indexes = _rows(
            db,
            f"PRAGMA index_list({quoted})",
            ("seq", "name", "unique", "origin", "partial"),
        )
        for index in indexes:
            index_quoted = _quote_identifier(index["name"])
            index["columns"] = _rows(
                db,
                f"PRAGMA index_xinfo({index_quoted})",
                ("seqno", "cid", "name", "desc", "coll", "key"),
            )
            index["columns"].sort(
                key=lambda row: (row["seqno"], row["cid"], row["name"] or "")
            )
        indexes.sort(key=lambda row: (row["name"], row["seq"]))
        tables.append(
            {
                "name": table_name,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )

    canonical = {"objects": objects, "tables": tables}
    return {
        "fingerprint": _canonical_fingerprint(canonical),
        "diagnostics": {"sqlite_version": sqlite3.sqlite_version},
        **canonical,
    }


def _capture_child(capture_dir: Path) -> None:
    import aetherstate  # type: ignore[import-untyped]
    from aetherstate.app import create_app  # type: ignore[import-untyped]
    from aetherstate.config import Config  # type: ignore[import-untyped]
    from aetherstate.store import Store  # type: ignore[import-untyped]

    exported_source = (Path.cwd() / "src").resolve()
    import_origin = Path(aetherstate.__file__).resolve()
    if exported_source not in import_origin.parents:
        raise ValueError("Historical capture did not import the exported revision")

    for shape in SHAPES:
        database_path = capture_dir / f"{shape}.db"
        store = Store(database_path)
        try:
            if shape == "full-start":
                create_app(Config(), store=store)
            payload = _capture_connection(store.db)
        finally:
            store.close()
        (capture_dir / f"{shape}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _validate_contract(payload: dict[str, Any]) -> None:
    expected_top = {
        "schema",
        "baseline_id",
        "shape",
        "source_commit",
        "source_blobs",
        "fingerprint",
        "diagnostics",
        "objects",
        "tables",
    }
    if set(payload) != expected_top:
        raise ValueError("Schema contract contains fields outside the approved contract")
    if payload["schema"] != SCHEMA_ID or payload["shape"] not in SHAPES:
        raise ValueError("Invalid schema contract identity")
    if not re.fullmatch(r"[0-9a-f]{40}", payload["source_commit"]):
        raise ValueError("Invalid source commit identity")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", payload["fingerprint"]):
        raise ValueError("Invalid schema fingerprint")
    if set(payload["diagnostics"]) != {"sqlite_version"}:
        raise ValueError("Schema diagnostics contain an unapproved field")
    if not payload["objects"] or not payload["tables"]:
        raise ValueError("Schema capture is empty")
    for source in payload["source_blobs"]:
        if set(source) != {"path", "blob"}:
            raise ValueError("Source blob contains an unapproved field")
        path = PurePosixPath(source["path"])
        if path.is_absolute() or ".." in path.parts or "\\" in source["path"]:
            raise ValueError("Source blob path is not repository-relative")
        if not re.fullmatch(r"[0-9a-f]{40}", source["blob"]):
            raise ValueError("Invalid source blob identity")
    for row in payload["objects"]:
        if set(row) != {"type", "name", "table_name", "sql"}:
            raise ValueError("Schema object contains an unapproved field")
        if row["type"] not in {"table", "index", "trigger", "view"}:
            raise ValueError("Schema object has an unapproved type")
        if WRITE_SQL_PATTERN.search(row["sql"]) or not DDL_SQL_PATTERN.match(row["sql"]):
            raise ValueError("Schema object contains non-DDL SQL")
        if os.path.isabs(row["name"]) or os.path.isabs(row["table_name"]):
            raise ValueError("Schema object contains an absolute path")
    for table in payload["tables"]:
        if set(table) != {"name", "columns", "foreign_keys", "indexes"}:
            raise ValueError("Table metadata contains an unapproved field")
        for column in table["columns"]:
            if set(column) != {
                "cid",
                "name",
                "type",
                "notnull",
                "default",
                "pk",
                "hidden",
            }:
                raise ValueError("Column metadata contains an unapproved field")
        for foreign_key in table["foreign_keys"]:
            if set(foreign_key) != {
                "id",
                "seq",
                "table",
                "from",
                "to",
                "on_update",
                "on_delete",
                "match",
            }:
                raise ValueError("Foreign-key metadata contains an unapproved field")
        for index in table["indexes"]:
            if set(index) != {
                "seq",
                "name",
                "unique",
                "origin",
                "partial",
                "columns",
            }:
                raise ValueError("Index metadata contains an unapproved field")
            for column in index["columns"]:
                if set(column) != {"seqno", "cid", "name", "desc", "coll", "key"}:
                    raise ValueError("Index-column metadata contains an unapproved field")


def _write_captures(
    output_dir: Path,
    captures: dict[str, dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for shape in SHAPES:
        payload = captures[shape]
        (output_dir / f"{shape}.schema.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"{shape}.schema.sql").write_text(
            _sql_projection(payload),
            encoding="utf-8",
        )


def _check_captures(
    check_dir: Path,
    captures: dict[str, dict[str, Any]],
) -> None:
    compared = {}
    expected_baseline_id = None
    for shape in SHAPES:
        expected = json.loads(
            (check_dir / f"{shape}.schema.json").read_text(encoding="utf-8")
        )
        _validate_contract(expected)
        if expected["shape"] != shape:
            raise ValueError(f"{shape} fixture has the wrong shape identity")
        if expected_baseline_id is None:
            expected_baseline_id = expected["baseline_id"]
        elif expected["baseline_id"] != expected_baseline_id:
            raise ValueError("Expected fixtures name different baselines")
        computed_fingerprint = _canonical_fingerprint(expected)
        if expected["fingerprint"] != computed_fingerprint:
            raise ValueError(
                f"{shape} fixture fingerprint does not match canonical content"
            )
        expected_sql = _sql_projection(expected)
        stored_sql = (check_dir / f"{shape}.schema.sql").read_text(encoding="utf-8")
        if stored_sql != expected_sql:
            raise ValueError(
                f"{shape} sibling SQL projection does not match canonical JSON DDL"
            )
        expected_fingerprint = expected["fingerprint"]
        actual_fingerprint = captures[shape]["fingerprint"]
        if actual_fingerprint != expected_fingerprint:
            raise ValueError(
                f"{shape} schema differs: "
                f"expected {expected_fingerprint}, got {actual_fingerprint}"
            )
        compared[shape] = actual_fingerprint
    print(json.dumps({"status": "PASS", "fingerprints": compared}, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture content-free SQLite schema contracts from exact Git objects."
    )
    parser.add_argument("--git-ref")
    parser.add_argument("--baseline-id")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--check-dir", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-dir", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.child:
        if args.capture_dir is None:
            raise ValueError("--capture-dir is required for child capture")
        _capture_child(args.capture_dir)
        return
    if not args.git_ref:
        raise ValueError("--git-ref is required")
    if args.output_dir is None and args.check_dir is None:
        raise ValueError("--output-dir or --check-dir is required")
    if args.output_dir is not None and not args.baseline_id:
        raise ValueError("--baseline-id is required with --output-dir")

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    )
    commit = _resolve_commit(root, args.git_ref)
    if args.check_dir is not None:
        existing = json.loads(
            (args.check_dir / "core.schema.json").read_text(encoding="utf-8")
        )
        baseline_id = existing["baseline_id"]
    else:
        baseline_id = args.baseline_id
    captures = _capture_exported_revision(root, commit, baseline_id)
    if args.output_dir is not None:
        _write_captures(args.output_dir, captures)
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "source_commit": commit,
                    "baseline_id": baseline_id,
                    "fingerprints": {
                        shape: captures[shape]["fingerprint"] for shape in SHAPES
                    },
                },
                sort_keys=True,
            )
        )
    else:
        _check_captures(args.check_dir, captures)


if __name__ == "__main__":
    main()
