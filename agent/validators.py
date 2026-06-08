"""SQL validator — generalised from Yokohama's validators.py.

Same defence-in-depth (sqlglot AST walk, SELECT-only, table + qualified-column
allowlists, auto-LIMIT) but the allowlists are passed in per-request from the
active dataset's artifact rather than baked in as module constants.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

ALLOWED_STATEMENT_TYPES = (exp.Select,)
DEFAULT_ROW_LIMIT = 100

_FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
    exp.Alter, exp.TruncateTable, exp.Merge, exp.Copy, exp.Grant, exp.RenameColumn,
)


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None
    sparql: str | None = None
    error: str | None = None
    notes: list[str] | None = None


def validate(sql: str, known_tables: set[str], known_columns: dict[str, set[str]]) -> ValidationResult:
    notes: list[str] = []

    # 1. Parse — single statement only.
    try:
        parsed = sqlglot.parse(sql, dialect="postgres")
    except sqlglot.errors.ParseError as e:
        return ValidationResult(ok=False, sql=sql, error=f"parse error: {e}")
    parsed = [p for p in parsed if p is not None]
    if len(parsed) == 0:
        return ValidationResult(ok=False, sql=sql, error="no statement parsed")
    if len(parsed) > 1:
        return ValidationResult(ok=False, sql=sql, error="multi-statement not allowed")
    stmt = parsed[0]

    # 2. SELECT only.
    if not isinstance(stmt, ALLOWED_STATEMENT_TYPES):
        return ValidationResult(ok=False, sql=sql,
                                error=f"non-select statement rejected: {type(stmt).__name__}")

    # 3. No DDL/DML nested in CTEs / subqueries.
    for node in stmt.walk():
        if isinstance(node, _FORBIDDEN):
            return ValidationResult(ok=False, sql=sql,
                                    error=f"forbidden expression: {type(node).__name__}")

    known_tables = {t.lower() for t in known_tables}
    known_columns = {t.lower(): {c.lower() for c in cols} for t, cols in known_columns.items()}

    # 4. Table allowlist (CTE aliases are local — exempt).
    cte_names = {(c.alias_or_name or "").lower() for c in stmt.find_all(exp.CTE)}
    unknown = []
    for table in stmt.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in known_tables and name not in cte_names:
            unknown.append(name)
    if unknown:
        return ValidationResult(ok=False, sql=sql, error=f"unknown table(s): {sorted(set(unknown))}")

    # 4b. Qualified-column allowlist — resolve aliases to base tables and reject
    # columns the LLM hallucinated. Unqualified / CTE-qualified columns are
    # skipped (attributing them needs full scope resolution).
    alias_to_table: dict[str, str] = {}
    for tbl in stmt.find_all(exp.Table):
        base = (tbl.name or "").lower()
        if base in known_columns:
            alias_to_table[base] = base
            alias = (tbl.alias or "").lower()
            if alias:
                alias_to_table[alias] = base
    bad_columns: list[str] = []
    for col in stmt.find_all(exp.Column):
        qualifier = (col.table or "").lower()
        cname = (col.name or "").lower()
        if not qualifier or not cname:
            continue
        base = alias_to_table.get(qualifier)
        if base is None:
            continue
        if cname not in known_columns[base]:
            bad_columns.append(f"{qualifier}.{col.name}")
    if bad_columns:
        return ValidationResult(ok=False, sql=sql, error=f"unknown column(s): {sorted(set(bad_columns))}")

    # 5. Auto-LIMIT.
    if not stmt.args.get("limit"):
        stmt = stmt.limit(DEFAULT_ROW_LIMIT)
        notes.append(f"auto-applied LIMIT {DEFAULT_ROW_LIMIT}")

    return ValidationResult(ok=True, sql=stmt.sql(dialect="postgres"), notes=notes)


# ============================================================================
# SPARQL / SHACL validator (M3) — per-project ontology + prefix
# ============================================================================

_FORBIDDEN_SPARQL = re.compile(
    r"\b(INSERT\s+(DATA|\{)|DELETE\s+(DATA|WHERE|\{)|DROP\b|CLEAR\b|LOAD\b|"
    r"CREATE\s+(SILENT\s+)?GRAPH|COPY\s+\w|MOVE\s+\w|ADD\s+\w)", re.IGNORECASE)

# ontology_path -> {mtime, predicates:set[str], classes:set[str]}
_ont_cache: dict[str, dict] = {}


def _load_ontology(ontology_path: str) -> dict | None:
    p = Path(ontology_path)
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    ent = _ont_cache.get(str(p))
    if ent and ent["mtime"] == mtime:
        return ent
    from rdflib import Graph, OWL, RDF
    g = Graph()
    g.parse(str(p), format="turtle")
    preds = {str(s) for s in g.subjects(RDF.type, OWL.ObjectProperty)}
    preds |= {str(s) for s in g.subjects(RDF.type, OWL.DatatypeProperty)}
    preds.add(str(RDF.type))
    classes = {str(s) for s in g.subjects(RDF.type, OWL.Class)}
    ent = {"mtime": mtime, "predicates": preds, "classes": classes}
    _ont_cache[str(p)] = ent
    return ent


def validate_sparql(sparql_text: str, ontology_path: str, ontology_ns: str,
                    prefix: str) -> ValidationResult:
    """Validate generated SPARQL: parse, SELECT-only, predicate allowlist against
    the active project's ontology (scanned via its prefix), auto-LIMIT."""
    notes: list[str] = []
    try:
        from rdflib.plugins.sparql import prepareQuery
        prepareQuery(sparql_text)
    except Exception as e:  # noqa: BLE001
        return ValidationResult(ok=False, sparql=sparql_text, error=f"SPARQL parse error: {e}")

    if _FORBIDDEN_SPARQL.search(sparql_text):
        return ValidationResult(ok=False, sparql=sparql_text,
                                error="SPARQL UPDATE / management ops not allowed")
    for kw in ("CONSTRUCT", "DESCRIBE", "ASK"):
        if re.search(rf"\b{kw}\b", sparql_text, re.IGNORECASE):
            return ValidationResult(ok=False, sparql=sparql_text, error=f"{kw} not allowed; use SELECT")
    if not re.search(r"\bSELECT\b", sparql_text, re.IGNORECASE):
        return ValidationResult(ok=False, sparql=sparql_text, error="only SELECT queries allowed")

    ent = _load_ontology(ontology_path)
    if ent and prefix:
        used = {ontology_ns + m.group(1)
                for m in re.finditer(rf"\b{re.escape(prefix)}:([A-Za-z_]\w*)", sparql_text)}
        unknown = used - ent["predicates"] - ent["classes"]
        if unknown:
            return ValidationResult(ok=False, sparql=sparql_text,
                                    error=f"unknown ontology term(s): {sorted(unknown)}")

    if not re.search(r"\bLIMIT\s+\d+", sparql_text, re.IGNORECASE):
        sparql_text = sparql_text.rstrip("\n ; ") + "\nLIMIT 100"
        notes.append("auto-applied LIMIT 100")
    return ValidationResult(ok=True, sparql=sparql_text, notes=notes)


def validate_result_graph(data_graph, shapes_path: str) -> dict:
    """Run SHACL shapes over the SPARQL result graph (non-blocking report)."""
    from rdflib import Graph, RDF, URIRef
    subjects = set(data_graph.subjects())
    if not subjects:
        return {"conforms": True, "validated_nodes": 0, "violations": 0, "summary": []}
    sp = Path(shapes_path)
    if not sp.exists():
        return {"conforms": None, "validated_nodes": len(subjects), "violations": None,
                "summary": [], "note": "shapes not found"}
    shapes = Graph()
    shapes.parse(str(sp), format="turtle")
    try:
        from pyshacl import validate as _run_shacl
        conforms, results_graph, _ = _run_shacl(
            data_graph, shacl_graph=shapes, ont_graph=None, inference="none", advanced=False)
    except Exception as e:  # noqa: BLE001
        return {"conforms": None, "validated_nodes": len(subjects), "violations": None,
                "summary": [], "note": f"pyshacl error: {e}"}
    sh = "http://www.w3.org/ns/shacl#"
    vresults = list(results_graph.subjects(RDF.type, URIRef(sh + "ValidationResult")))
    summary = []
    for r in vresults[:5]:
        msg = results_graph.value(r, URIRef(sh + "resultMessage"))
        path = results_graph.value(r, URIRef(sh + "resultPath"))
        summary.append({"path": str(path).rsplit("#", 1)[-1] if path else None,
                        "message": str(msg) if msg else None})
    return {"conforms": bool(conforms), "validated_nodes": len(subjects),
            "violations": len(vresults), "summary": summary}
