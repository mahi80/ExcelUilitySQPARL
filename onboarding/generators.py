"""Generators — turn a *confirmed* profile into the artifacts the rest of the
stack consumes:

  - generate_ddl()            → CREATE SCHEMA / CREATE TABLE statements
  - generate_schema_context() → the natural-language schema description fed to
                                the Text-to-SQL LLM (generalises Yokohama's
                                hand-written agent/schema_context.py)
  - generate_allowlists()     → {known_tables, known_columns} for the validator
                                (generalises validators.KNOWN_TABLES/COLUMNS)
  - build_artifact()          → the single JSON blob the agent loads per request

This is the heart of "the utility generates what Yokohama hard-codes". Pure
Python (stdlib only) so it is trivially unit-testable.
"""
from __future__ import annotations

from typing import Any

# Logical type (profiler) → Postgres column type.
PG_TYPE = {
    "integer": "BIGINT",
    "decimal": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "text": "TEXT",
}

SURROGATE_PK = "_row_id"        # synthesised when a sheet has no natural key
DEFAULT_ROW_LIMIT = 100
_ENUM_SAMPLE_MAX = 12           # surface sample values when distinct count ≤ this


def _q(ident: str) -> str:
    """Quote a SQL identifier (handles reserved words / odd names safely)."""
    return '"' + str(ident).replace('"', '""') + '"'


def _included(profile: dict) -> list[dict]:
    return [t for t in profile.get("tables", []) if t.get("include", True)]


def _columns(table: dict) -> list[dict]:
    return [c for c in table.get("columns", []) if c.get("include", True)]


def _effective_pk(table: dict) -> tuple[str | None, bool]:
    """Return (pk_column, use_surrogate). Falls back to a synthesised surrogate
    when there is no natural PK, the table is flagged synth, or the chosen PK
    column was excluded in review."""
    included = {c["name"] for c in _columns(table)}
    pk = table.get("pk")
    if pk and not table.get("synth_pk") and pk in included:
        return pk, False
    return None, True


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def generate_ddl(profile: dict, schema: str) -> list[str]:
    """Return ordered DDL statements: schema + one CREATE TABLE per table.

    FKs are recorded in the schema-context for the LLM but NOT enforced as DB
    constraints in M1 — real uploads routinely have orphan rows / excluded
    lookup sheets, and a failed constraint would abort the whole load. (M2's
    dictionary-driven generation can opt into enforcement.)
    """
    stmts = [f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}"]
    for t in _included(profile):
        pk, synth = _effective_pk(t)
        lines: list[str] = []
        if synth:
            lines.append(f"  {_q(SURROGATE_PK)} BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
        for c in _columns(t):
            pg = PG_TYPE.get(c.get("type", "text"), "TEXT")
            line = f"  {_q(c['name'])} {pg}"
            if not c.get("nullable", True):
                line += " NOT NULL"
            lines.append(line)
        if not synth and pk:
            lines.append(f"  PRIMARY KEY ({_q(pk)})")
        body = ",\n".join(lines)
        stmts.append(f"CREATE TABLE {_q(schema)}.{_q(t['name'])} (\n{body}\n)")
    return stmts


# ---------------------------------------------------------------------------
# Schema context (LLM system prompt)
# ---------------------------------------------------------------------------

def _join_keys(profile: dict) -> list[str]:
    out: list[str] = []
    for t in _included(profile):
        for fk in t.get("fks", []):
            out.append(f"  {t['name']}.{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}")
    return out


def generate_schema_context(profile: dict) -> str:
    name = profile.get("dataset_name", "dataset")
    desc = profile.get("domain") or profile.get("description")
    parts: list[str] = []

    header = (
        f'You generate PostgreSQL SELECT queries over the "{name}" dataset.\n'
        "You ONLY produce a single SELECT statement, never DDL or DML."
    )
    if desc:
        header += f"\n\nDomain: {desc}"
    parts.append(header)

    # == TABLES ==
    tbl_lines = ["== TABLES =="]
    for t in _included(profile):
        title = f"\n{t['name']} ({t['row_count']} rows)"
        if t.get("description"):
            title += f" — {t['description']}"
        tbl_lines.append(title)
        for c in _columns(t):
            seg = f"  {c['name']} ({c.get('type', 'text')})"
            label = c.get("business_name") or c.get("description")
            if label:
                seg += f" — {label}"
            if c.get("synonyms"):
                seg += " (also known as: " + ", ".join(c["synonyms"]) + ")"
            if c.get("unit"):
                seg += f" (unit: {c['unit']})"
            # surface enum-like value vocabularies (region, status, family, …)
            if c.get("samples") and c.get("distinct", 9999) <= _ENUM_SAMPLE_MAX:
                seg += "  e.g. " + ", ".join(repr(s) for s in c["samples"])
            tbl_lines.append(seg)
        eff_pk, _ = _effective_pk(t)
        tbl_lines.append(f"  primary key: {eff_pk or SURROGATE_PK}")
    parts.append("\n".join(tbl_lines))

    # == JOIN KEYS ==
    jk = _join_keys(profile)
    if jk:
        parts.append("== JOIN KEYS (inferred) ==\n" + "\n".join(jk))

    # == GLOSSARY (business terms → data) ==
    gl = [g for g in (profile.get("glossary") or []) if g.get("term")]
    if gl:
        parts.append("== GLOSSARY ==\n" + "\n".join(
            f"  {g['term']}: {g.get('definition', '')}".rstrip() for g in gl))

    # == TEXT MATCHING ==
    parts.append(
        "== TEXT-MATCHING GUIDANCE ==\n"
        "  For partial / 'contains' matches on text columns, use ILIKE '%phrase%' "
        "(case-insensitive), NOT = exact equality — a user's phrase is usually a\n"
        "  substring of the stored value (names, descriptions, codes). Use exact "
        "equality only for short codes / enum values listed above."
    )

    # == RULES ==
    parts.append(
        "== RULES ==\n"
        "1. Output ONE valid PostgreSQL SELECT. Nothing else — no DDL, DML, or comments.\n"
        f"2. Always include LIMIT (default LIMIT {DEFAULT_ROW_LIMIT} when no count is implied).\n"
        "3. Lowercase identifiers. Quote string literals with single quotes. Use ISO dates "
        "('2026-07-06').\n"
        "4. In ANY query with a JOIN, QUALIFY every column with its table alias (e.g. p.sku, "
        "c.name) — several columns share names across tables and an unqualified reference "
        "raises 'column reference is ambiguous'.\n"
        "5. When summing monetary or quantity values that may mix currencies/units, GROUP BY "
        "the currency/unit column rather than summing across them.\n"
        "6. Use ONLY the tables and columns listed above. Never invent a column or table; if "
        "the data needed isn't present, produce the closest faithful query (it may return 0 "
        "rows, which the agent explains downstream)."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Validator allowlists
# ---------------------------------------------------------------------------

def generate_allowlists(profile: dict) -> dict[str, Any]:
    known_tables: list[str] = []
    known_columns: dict[str, list[str]] = {}
    for t in _included(profile):
        known_tables.append(t["name"])
        cols = [c["name"] for c in _columns(t)]
        _, synth = _effective_pk(t)
        if synth:
            cols = [SURROGATE_PK] + cols
        known_columns[t["name"]] = sorted(set(cols))
    return {"known_tables": sorted(known_tables), "known_columns": known_columns}


# ---------------------------------------------------------------------------
# Artifact (what the agent loads)
# ---------------------------------------------------------------------------

def build_artifact(profile: dict, schema: str) -> dict:
    allow = generate_allowlists(profile)
    return {
        "dataset_name": profile.get("dataset_name", "dataset"),
        "source_file": profile.get("source_file"),
        "domain": profile.get("domain"),
        "glossary": profile.get("glossary", []),
        "schema": schema,
        "tables": [
            {
                "name": t["name"],
                "row_count": t["row_count"],
                "description": t.get("description"),
                "pk": _effective_pk(t)[0] or SURROGATE_PK,
                "columns": [
                    {"name": c["name"], "type": c.get("type", "text"),
                     "business_name": c.get("business_name"), "description": c.get("description"),
                     "synonyms": c.get("synonyms", []), "unit": c.get("unit")}
                    for c in _columns(t)
                ],
                "fks": [fk for fk in t.get("fks", []) if fk.get("include", True)],
            }
            for t in _included(profile)
        ],
        "schema_context": generate_schema_context(profile),
        "known_tables": allow["known_tables"],
        "known_columns": allow["known_columns"],
    }
