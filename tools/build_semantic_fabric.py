"""Build the sealed compact translation-memory family for AetherState's semantic fabric."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any


SOURCE_SCHEMA = "semantic-fabric-source/1"
EXPANSION_SCHEMA = "semantic-fabric-genre-expansions/1"
MANIFEST_SCHEMA = "aetherstate-semantic-fabric/1"
PACK_SCHEMA = "semantic-translation-memory/2"
SOURCES_SCHEMA = "semantic-fabric-sources/1"
LEX_IDS = ("capability", "referent", "scene", "action", "claim")
PACK_LEX_IDS = LEX_IDS[1:]


def canonical_bytes(value: object, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def fingerprint(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def entry_meaning_fingerprint(lex_id: str, entry: dict[str, Any]) -> str:
    """Seal only the canonical meaning fields for one non-capability Lex entry."""
    required = (
        "concept_id",
        "kind",
        "required_roles",
        "optional_roles",
        "completion",
        "features",
    )
    if lex_id not in PACK_LEX_IDS or any(key not in entry for key in required):
        raise ValueError("semantic-fabric meaning fingerprint input is incomplete")
    return fingerprint(
        {
            "lex_id": lex_id,
            "concept_id": entry["concept_id"],
            "kind": entry["kind"],
            "required_roles": entry["required_roles"],
            "optional_roles": entry["optional_roles"],
            "completion": entry["completion"],
            "features": entry["features"],
        }
    )


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def genre_ids(root: Path) -> list[str]:
    out: list[str] = []
    for path in sorted((root / "corpus" / "capability-glossary" / "genres").glob("*.json")):
        doc = read_object(path)
        rows = doc.get("genres")
        if not isinstance(rows, list):
            raise ValueError(f"{path} has no genre list")
        for row in rows:
            genre_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(genre_id, str) or not genre_id:
                raise ValueError(f"{path} has an invalid genre row")
            if genre_id in out:
                raise ValueError(f"duplicate genre id: {genre_id}")
            out.append(genre_id)
    if len(out) != 31:
        raise ValueError(f"expected 31 CapabilityLex genre facets, found {len(out)}")
    return sorted(out)


def merge_source(base: dict[str, Any], expansion: dict[str, Any] | None) -> dict[str, Any]:
    if base.get("schema") != SOURCE_SCHEMA:
        raise ValueError("unsupported semantic-fabric source schema")
    if set(base) != {"schema", "sources", "packs"}:
        raise ValueError("semantic-fabric source fields do not match v1")
    if not isinstance(base.get("sources"), list) or not isinstance(base.get("packs"), dict):
        raise ValueError("semantic-fabric source requires source and pack collections")
    if set(base["packs"]) != set(PACK_LEX_IDS):
        raise ValueError("semantic-fabric source must define every non-capability Lex")
    merged = json.loads(json.dumps(base, ensure_ascii=False))
    if expansion is None:
        return merged
    if expansion.get("schema") != EXPANSION_SCHEMA:
        raise ValueError("unsupported semantic-fabric expansion schema")
    if set(expansion) != {"schema", "sources", "packs"}:
        raise ValueError("semantic-fabric expansion fields do not match v1")
    if not isinstance(expansion.get("sources"), list):
        raise ValueError("semantic-fabric expansion sources must be a list")
    merged["sources"].extend(expansion["sources"])
    packs = expansion.get("packs")
    if not isinstance(packs, dict):
        raise ValueError("semantic-fabric expansion packs must be an object")
    for lex_id, delta in packs.items():
        if lex_id not in PACK_LEX_IDS or not isinstance(delta, dict):
            raise ValueError(f"unsupported semantic-fabric expansion pack: {lex_id}")
        if set(delta) != {"entries", "constructions", "ambiguities"}:
            raise ValueError(f"{lex_id} expansion fields do not match v1")
        target = merged["packs"][lex_id]
        for field in ("entries", "constructions", "ambiguities"):
            rows = delta.get(field, [])
            if not isinstance(rows, list):
                raise ValueError(f"{lex_id} expansion {field} must be a list")
            target[field].extend(rows)
    return merged


def seal_rows(
    rows: list[dict[str, Any]],
    identity: str,
    *,
    lex_id: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in rows:
        if (
            not isinstance(source, dict)
            or identity not in source
            or "fingerprint" in source
            or (identity == "concept_id" and "meaning_fingerprint" in source)
        ):
            raise ValueError(f"semantic-fabric row is malformed for {identity}")
        row_id = source[identity]
        if not isinstance(row_id, str) or not row_id or row_id in seen:
            raise ValueError(f"duplicate or invalid semantic-fabric row: {row_id!r}")
        seen.add(row_id)
        row = json.loads(json.dumps(source, ensure_ascii=False))
        if identity == "concept_id":
            row.setdefault("genre_terms", [])
            if lex_id is None:
                raise ValueError("semantic-fabric concept rows require a Lex id")
            row["meaning_fingerprint"] = entry_meaning_fingerprint(lex_id, row)
        row["fingerprint"] = fingerprint(row)
        out.append(row)
    return sorted(out, key=lambda row: row[identity])


def build_documents(root: Path) -> dict[str, bytes]:
    corpus = root / "corpus" / "semantic-fabric"
    base = read_object(corpus / "source" / "base.json")
    expansion_path = corpus / "source" / "genre-expansions.json"
    expansion = read_object(expansion_path) if expansion_path.is_file() else None
    source = merge_source(base, expansion)

    sources = source.get("sources")
    packs = source.get("packs")
    if not isinstance(sources, list) or not isinstance(packs, dict):
        raise ValueError("semantic-fabric source requires sources and packs")
    source_ids: set[str] = set()
    for row in sources:
        source_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(source_id, str) or not source_id or source_id in source_ids:
            raise ValueError(f"duplicate or invalid semantic-fabric source: {source_id!r}")
        source_ids.add(source_id)
    sources_doc = {"schema": SOURCES_SCHEMA, "sources": sorted(sources, key=lambda row: row["id"])}

    documents: dict[str, bytes] = {
        "sources.json": canonical_bytes(sources_doc, pretty=True),
    }
    for lex_id in PACK_LEX_IDS:
        source_pack = packs.get(lex_id)
        if not isinstance(source_pack, dict) or set(source_pack) != {
            "description",
            "entries",
            "constructions",
            "ambiguities",
        }:
            raise ValueError(f"{lex_id} source pack fields do not match v1")
        entries = seal_rows(source_pack["entries"], "concept_id", lex_id=lex_id)
        constructions = seal_rows(source_pack["constructions"], "construction_id")
        for row in (*entries, *constructions):
            unknown = set(row.get("source_ids") or []) - source_ids
            if unknown:
                identity = row.get("concept_id") or row.get("construction_id")
                raise ValueError(f"{identity} uses unknown sources: {sorted(unknown)}")
        ambiguities = sorted(
            source_pack["ambiguities"],
            key=lambda row: (row.get("term", ""), tuple(row.get("concept_ids") or ())),
        )
        pack = {
            "schema": PACK_SCHEMA,
            "lex_id": lex_id,
            "version": 2,
            "description": source_pack["description"],
            "authority": "recognition_only",
            "entries": entries,
            "constructions": constructions,
            "ambiguities": ambiguities,
        }
        pack["fingerprint"] = fingerprint(pack)
        documents[f"{lex_id}-lex.json"] = canonical_bytes(pack, pretty=True)

    artifacts = []
    for relative, raw in sorted(documents.items()):
        artifacts.append(
            {
                "path": relative,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    capability_manifest = root / "corpus" / "capability-glossary" / "manifest.json"
    capability_bytes = capability_manifest.read_bytes()
    capability_doc = json.loads(capability_bytes)
    if not isinstance(capability_doc, dict) or capability_doc.get("schema") != (
        "aetherstate-capability-glossary/2"
    ):
        raise ValueError("Semantic Fabric requires CapabilityLex glossary schema v2")
    capability_hash = hashlib.sha256(capability_bytes).hexdigest()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "family_version": 3,
        "lex_ids": list(LEX_IDS),
        "artifacts": artifacts,
        "capability_lex": {
            "schema": capability_doc["schema"],
            "manifest_fingerprint": "sha256:" + capability_hash,
            "mode": "external_adapter",
        },
        "genre_ids": genre_ids(root),
        "authority_boundary": "recognition_only",
    }
    manifest["fingerprint"] = fingerprint(manifest)
    documents["manifest.json"] = canonical_bytes(manifest, pretty=True)
    return documents


def publish(root: Path, documents: dict[str, bytes], *, check: bool) -> None:
    corpus = root / "corpus" / "semantic-fabric"
    stale: list[str] = []
    for relative, raw in documents.items():
        path = corpus / relative
        if check:
            if not path.is_file() or path.read_bytes() != raw:
                stale.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
    if stale:
        raise SystemExit("semantic-fabric corpus is stale: " + ", ".join(sorted(stale)))


def validate_documents(root: Path, documents: dict[str, bytes]) -> None:
    """Load the exact candidate bytes before they can replace the sealed live family."""
    from aetherstate.semantic_fabric import SemanticFabric

    capability_source = root / "corpus" / "capability-glossary"
    with tempfile.TemporaryDirectory(prefix="aetherstate-semantic-fabric-") as raw_temp:
        corpus = Path(raw_temp) / "corpus"
        shutil.copytree(capability_source, corpus / "capability-glossary")
        semantic_root = corpus / "semantic-fabric"
        semantic_root.mkdir(parents=True)
        for relative, raw in documents.items():
            path = semantic_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        SemanticFabric.load(semantic_root)


def build(root: Path, *, check: bool = False) -> dict[str, bytes]:
    resolved = root.resolve()
    documents = build_documents(resolved)
    validate_documents(resolved, documents)
    publish(resolved, documents, check=check)
    return documents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    documents = build(args.root, check=args.check)
    print(f"semantic fabric: {len(documents)} sealed artifacts")


if __name__ == "__main__":
    main()
