"""Ontology / OBDA / SHACL / SPARQL-context generators (M3).

Turns a confirmed, dictionary-enriched profile into the per-project artifacts the
SPARQL plane needs — generalising the hand-written Yokohama files:
  - ontology.ttl   (table→owl:Class, column→DatatypeProperty, FK→ObjectProperty)
  - mapping.obda   (Ontop OBDA: subject IRI from PK, one triple per column/FK,
                    schema-qualified SQL source)
  - shapes.ttl     (SHACL NodeShape per class)
  - sparql_context (the NL→SPARQL system prompt)

Each project gets its OWN IRI namespace so a single Ontop instance can serve many
projects without term collisions (see ontop_combine.py).

Pure Python (stdlib only) — unit-testable without a DB or Ontop.
"""
from __future__ import annotations

from typing import Any

from generators import SURROGATE_PK, _effective_pk, _included, _columns

# logical type → xsd type (mirrors generators.PG_TYPE)
XSD = {
    "integer": "xsd:integer", "decimal": "xsd:decimal", "boolean": "xsd:boolean",
    "date": "xsd:date", "datetime": "xsd:dateTime", "text": "xsd:string",
}

ONTOLOGY_BASE = "http://exutil.local/ds"


def project_namespaces(project_id: str) -> tuple[str, str, str, str]:
    """(ontology_ns, resource_ns, ont_prefix, res_prefix) — all unique per project
    so one Ontop instance can serve many projects without prefix collisions."""
    ont = f"{ONTOLOGY_BASE}/{project_id}/ontology#"
    res = f"{ONTOLOGY_BASE}/{project_id}/resource/"
    ont_prefix = project_id.replace("-", "_")       # e.g. 'p_1a2b3c4d' — valid PName prefix
    res_prefix = ont_prefix + "_r"                  # distinct resource prefix
    return ont, res, ont_prefix, res_prefix


# ---------------------------------------------------------------------------
# Naming (deterministic, collision-safe within a project)
# ---------------------------------------------------------------------------

def class_name(table: str) -> str:
    return "".join(w.capitalize() for w in str(table).split("_") if w) or "T"


def prop_name(column: str) -> str:
    parts = [w for w in str(column).split("_") if w]
    if not parts:
        return "p"
    return parts[0].lower() + "".join(w.capitalize() for w in parts[1:])


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class _Naming:
    """Per-project registry so object-property local names stay unique unless they
    legitimately share a range class (mirrors Yokohama reusing atPlant)."""

    def __init__(self):
        self._obj: dict[str, str] = {}      # local-name -> range class

    def fk_prop(self, column: str, ref_table: str) -> str:
        ref_cls = class_name(ref_table)
        col = column.lower()
        base = ref_table.lower()
        if col in (f"{base}_id", base, f"{_singular(base)}_id", _singular(base)):
            name = "at" + ref_cls
        else:
            stem = column[:-3] if col.endswith("_id") else column
            name = "has" + class_name(stem)
        existing = self._obj.get(name)
        if existing and existing != ref_cls:        # collision with a different range
            name = name + "Via" + class_name(column)
        self._obj[name] = ref_cls
        return name


# ---------------------------------------------------------------------------
# Ontology TTL
# ---------------------------------------------------------------------------

_TTL_PREAMBLE = (
    "@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix owl:  <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .\n"
)


def generate_ontology_ttl(profile: dict, ontology_ns: str, ont_prefix: str,
                          dataset_name: str = "dataset") -> str:
    """Per-project ontology, written under the project's own prefix so several
    projects' ontologies can be concatenated into one combined file (P5)."""
    pfx = ont_prefix
    out = [_TTL_PREAMBLE, f"@prefix {pfx}: <{ontology_ns}> .\n",
           f"<{ontology_ns.rstrip('#')}> a owl:Ontology ; rdfs:label \"{_esc(dataset_name)}\"@en .\n"]
    naming = _Naming()
    for t in _included(profile):
        cls = class_name(t["name"])
        label = t["name"].replace("_", " ").title()
        line = f"{pfx}:{cls} a owl:Class ; rdfs:label \"{_esc(label)}\"@en"
        if t.get("description"):
            line += f" ; rdfs:comment \"{_esc(t['description'])}\"@en"
        out.append(line + " .")
        for c in _columns(t):
            p = prop_name(c["name"])
            rng = XSD.get(c.get("type", "text"), "xsd:string")
            seg = f"{pfx}:{p} a owl:DatatypeProperty ; rdfs:domain {pfx}:{cls} ; rdfs:range {rng}"
            bn = c.get("business_name") or c["name"].replace("_", " ")
            seg += f" ; rdfs:label \"{_esc(bn)}\"@en"
            for syn in (c.get("synonyms") or []):
                seg += f" ; rdfs:label \"{_esc(syn)}\"@en"
            comment = c.get("description")
            if c.get("unit"):
                comment = f"{comment + ' ' if comment else ''}(unit: {c['unit']})"
            if comment:
                seg += f" ; rdfs:comment \"{_esc(comment)}\"@en"
            out.append(seg + " .")
        pk, synth = _effective_pk(t)
        if synth:
            out.append(f"{pfx}:{prop_name(SURROGATE_PK)} a owl:DatatypeProperty ; "
                       f"rdfs:domain {pfx}:{cls} ; rdfs:range xsd:integer .")
        for fk in t.get("fks", []):
            op = naming.fk_prop(fk["column"], fk["ref_table"])
            out.append(f"{pfx}:{op} a owl:ObjectProperty ; rdfs:domain {pfx}:{cls} ; "
                       f"rdfs:range {pfx}:{class_name(fk['ref_table'])} .")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# OBDA mapping (fragment — combined across projects by ontop_combine)
# ---------------------------------------------------------------------------

def generate_mapping_blocks(profile: dict, schema: str, ont_prefix: str,
                            res_prefix: str) -> list[str]:
    """Return the mapping blocks (mappingId/target/source) for this project,
    using per-project ontology + resource prefixes so blocks from many projects
    coexist in one combined [MappingDeclaration]."""
    naming = _Naming()
    blocks: list[str] = []
    for t in _included(profile):
        cls = class_name(t["name"])
        pk, synth = _effective_pk(t)
        pk_col = pk or SURROGATE_PK
        subj = f"{res_prefix}:{t['name']}/{{{pk_col}}}"
        triples = [f"{subj} a {ont_prefix}:{cls}"]
        select_cols = [pk_col] if synth else []
        for c in _columns(t):
            col = c["name"]
            select_cols.append(col)
            p = prop_name(col)
            xsd = XSD.get(c.get("type", "text"), "xsd:string")
            if xsd == "xsd:string":
                triples.append(f"{ont_prefix}:{p} {{{col}}}")
            else:
                triples.append(f"{ont_prefix}:{p} {{{col}}}^^{xsd}")
        if synth:
            triples.insert(1, f"{ont_prefix}:{prop_name(SURROGATE_PK)} {{{pk_col}}}^^xsd:integer")
        for fk in t.get("fks", []):
            op = naming.fk_prop(fk["column"], fk["ref_table"])
            triples.append(f"{ont_prefix}:{op} {res_prefix}:{fk['ref_table']}/{{{fk['column']}}}")
            if fk["column"] not in select_cols:
                select_cols.append(fk["column"])
        target = " ; ".join(triples) + " ."
        cols_sql = ", ".join(f'"{c}"' for c in dict.fromkeys(select_cols))
        source = f'SELECT {cols_sql} FROM "{schema}"."{t["name"]}"'
        blocks.append(f"mappingId   {ont_prefix}-{t['name']}\n"
                      f"target      {target}\n"
                      f"source      {source}")
    return blocks


# ---------------------------------------------------------------------------
# SHACL shapes
# ---------------------------------------------------------------------------

_ENUM_MAX = 12


def generate_shapes_ttl(profile: dict, ontology_ns: str, ont_prefix: str) -> str:
    pfx = ont_prefix
    out = [
        "@prefix sh:  <http://www.w3.org/ns/shacl#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"@prefix {pfx}: <{ontology_ns}> .\n"
    ]
    naming = _Naming()
    for t in _included(profile):
        cls = class_name(t["name"])
        out.append(f"{pfx}:{cls}Shape a sh:NodeShape ; sh:targetClass {pfx}:{cls} ;")
        props: list[str] = []
        for c in _columns(t):
            p = prop_name(c["name"])
            xsd = XSD.get(c.get("type", "text"), "xsd:string")
            parts = [f"sh:path {pfx}:{p}", f"sh:datatype {xsd}", "sh:maxCount 1"]
            if not c.get("nullable", True):
                parts.append("sh:minCount 1")
            if c.get("samples") and c.get("distinct", 9999) <= _ENUM_MAX and xsd == "xsd:string":
                vals = " ".join(f'"{_esc(s)}"' for s in c["samples"])
                parts.append(f"sh:in ( {vals} )")
            props.append("    sh:property [ " + " ; ".join(parts) + " ]")
        for fk in t.get("fks", []):
            op = naming.fk_prop(fk["column"], fk["ref_table"])
            props.append(f"    sh:property [ sh:path {pfx}:{op} ; sh:class {pfx}:{class_name(fk['ref_table'])} ; sh:maxCount 1 ]")
        out.append(" ;\n".join(props) + " .\n")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# SPARQL context (NL→SPARQL system prompt)
# ---------------------------------------------------------------------------

_PNAME_WARNING = (
    "IMPORTANT — IRI SYNTAX: resource IRIs contain a '/' (e.g.\n"
    "  <" + ONTOLOGY_BASE + "/<id>/resource/orders/O1>). In SPARQL, prefixed names\n"
    "  (PNames) CANNOT contain '/' in the local part — it is a syntax error. So\n"
    "  reference resources with full angle-bracket IRIs, or (better) bind them via a\n"
    "  triple pattern on an id/label property — NEVER with a ':table/{id}' PName."
)


def generate_sparql_context(profile: dict, ontology_ns: str, resource_ns: str,
                            ont_prefix: str, res_prefix: str,
                            dataset_name: str = "dataset") -> str:
    prefix = ont_prefix
    naming = _Naming()
    parts = [
        f'You generate SPARQL SELECT queries over the "{dataset_name}" dataset via an\n'
        "Ontop OBDA endpoint. Output ONE SELECT query, never an UPDATE/INSERT/DELETE.",
        "== NAMESPACES ==\n"
        f"PREFIX {ont_prefix}: <{ontology_ns}>\n"
        f"PREFIX {res_prefix}: <{resource_ns}>\n"
        "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
        "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
    ]
    classes = [class_name(t["name"]) for t in _included(profile)]
    parts.append("== CLASSES ==\n" + ", ".join(f"{prefix}:{c}" for c in classes))

    obj_lines: list[str] = []
    for t in _included(profile):
        for fk in t.get("fks", []):
            op = naming.fk_prop(fk["column"], fk["ref_table"])
            obj_lines.append(f"{prefix}:{op}  {prefix}:{class_name(t['name'])} -> "
                             f"{prefix}:{class_name(fk['ref_table'])}")
    if obj_lines:
        parts.append("== OBJECT PROPERTIES ==\n" + "\n".join(obj_lines))

    dt_lines: list[str] = []
    for t in _included(profile):
        cls = class_name(t["name"])
        for c in _columns(t):
            seg = f"{prefix}:{prop_name(c['name'])} ({XSD.get(c.get('type','text'),'xsd:string')}) on {prefix}:{cls}"
            hints = []
            if c.get("business_name"):
                hints.append(c["business_name"])
            hints += (c.get("synonyms") or [])
            if hints:
                seg += " — " + ", ".join(hints)
            if c.get("samples") and c.get("distinct", 9999) <= _ENUM_MAX:
                seg += "  e.g. " + ", ".join(repr(s) for s in c["samples"])
            dt_lines.append(seg)
    parts.append("== DATATYPE PROPERTIES ==\n" + "\n".join(dt_lines))

    gl = [g for g in (profile.get("glossary") or []) if g.get("term")]
    if gl:
        parts.append("== GLOSSARY ==\n" + "\n".join(
            f"  {g['term']}: {g.get('definition','')}".rstrip() for g in gl))

    parts.append(
        "== RULES ==\n"
        "1. Output ONE valid SPARQL SELECT. No UPDATE/INSERT/DELETE/CONSTRUCT/ASK.\n"
        "2. Always declare the PREFIXes you use (above) and include LIMIT (default 100).\n"
        "3. Type a node with `?x a " + prefix + ":Class` then match its datatype properties.\n"
        "4. For partial text matches use FILTER(CONTAINS(LCASE(?v), \"phrase\")); for dates\n"
        "   compare against xsd:date/xsd:dateTime literals.\n"
        "5. Use ONLY the classes/properties listed above — never invent terms.\n"
        + _PNAME_WARNING
    )
    return "\n\n".join(parts)
