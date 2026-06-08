"""Combine every ready project's OBDA into ONE Ontop config (M3 P5).

Ontop loads a single mapping+ontology at boot. We serve all projects from one
instance by concatenating each project's mapping blocks (each under its own
ontology + resource prefix, so no collisions) into one [MappingDeclaration], and
concatenating each project's ontology.ttl (each under its own unique prefix).

Written to ARTIFACTS_DIR/ontop/ which the ontop container mounts. Ontop must be
restarted to pick up changes (manual, per the design decision).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import registry

ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
ONTOP_DIR = ARTIFACTS_DIR / "ontop"
POSTGRES_DB = os.environ.get("POSTGRES_DB", "excelutil")

_STD_PREFIXES = [
    "rdf:   http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs:  http://www.w3.org/2000/01/rdf-schema#",
    "owl:   http://www.w3.org/2002/07/owl#",
    "xsd:   http://www.w3.org/2001/XMLSchema#",
]

_EMPTY_ONTOLOGY = (
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "<http://exutil.local/ontology> a owl:Ontology ; rdfs:label \"ExcelUtilitySPARQL\" .\n"
)


def _properties() -> str:
    url = os.environ.get("ONTOP_JDBC_URL", f"jdbc:postgresql://postgres:5432/{POSTGRES_DB}")
    return ("jdbc.driver = org.postgresql.Driver\n"
            f"jdbc.url = {url}\n"
            "jdbc.name = postgres\n")


def rebuild_combined_config(conn) -> dict:
    """Regenerate /artifacts/ontop/{mapping.obda,ontology.ttl,ontop.properties}
    from all ready projects. Returns a small summary. Caller restarts Ontop."""
    ONTOP_DIR.mkdir(parents=True, exist_ok=True)

    with conn.cursor() as cur:
        ready = [p for p in registry.list_projects(cur) if p["status"] == "ready"]

    blocks: list[str] = []
    prefix_lines: list[str] = []
    ontologies: list[str] = []
    for p in ready:
        ddir = ARTIFACTS_DIR / p["id"]
        art_path = ddir / "dataset.json"
        if not art_path.exists():
            continue
        art = json.loads(art_path.read_text(encoding="utf-8"))
        blocks.extend(art.get("mapping_blocks", []))
        if art.get("ontology_prefix") and art.get("ontology_ns"):
            prefix_lines.append(f"{art['ontology_prefix']}:   {art['ontology_ns']}")
        if art.get("resource_prefix") and art.get("resource_ns"):
            prefix_lines.append(f"{art['resource_prefix']}:   {art['resource_ns']}")
        ot = ddir / "ontology.ttl"
        if ot.exists():
            ontologies.append(ot.read_text(encoding="utf-8"))

    prefix_decl = "[PrefixDeclaration]\n" + "\n".join(_STD_PREFIXES + prefix_lines) + "\n\n"
    mapping_decl = "[MappingDeclaration] @collection [[\n\n" + "\n\n".join(blocks) + "\n\n]]\n"
    (ONTOP_DIR / "mapping.obda").write_text(prefix_decl + mapping_decl, encoding="utf-8")
    (ONTOP_DIR / "ontology.ttl").write_text(
        "\n".join(ontologies) if ontologies else _EMPTY_ONTOLOGY, encoding="utf-8")
    (ONTOP_DIR / "ontop.properties").write_text(_properties(), encoding="utf-8")

    return {"projects": len(ready), "mappings": len(blocks)}
