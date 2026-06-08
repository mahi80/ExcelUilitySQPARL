"""PageIndex retrieval (Phase-2) — pgvector semantic schema-slice for large schemas.

For a big dataset, feeding the whole schema to the LLM bloats the prompt and dilutes
relevance. select_schema_slice() embeds the question, finds the most relevant tables
via pgvector cosine distance, and builds a focused schema-context from just those
tables (+ the join keys among them). At small sizes the full context wins, so the
caller only invokes this above a size threshold.
"""
from __future__ import annotations

import os

import psycopg2

import embedder

RO_DSN = os.environ.get("POSTGRES_RO_DSN", "")
SLICE_TABLES = int(os.environ.get("PAGEINDEX_SLICE_TABLES", "6"))


def _rank_tables(project_id: str, question: str, k: int) -> list[str]:
    if not RO_DSN:
        return []
    try:
        qv = embedder.to_pgvector(embedder.embed_one(question))
    except Exception:
        return []
    try:
        with psycopg2.connect(RO_DSN) as conn:
            with conn.cursor() as cur:
                # rank each table by the closest of its table/column embeddings
                cur.execute(
                    "SELECT table_name, MIN(embedding <=> %s::vector) AS dist "
                    "FROM meta.schema_embedding WHERE project_id = %s "
                    "GROUP BY table_name ORDER BY dist ASC LIMIT %s",
                    (qv, project_id, k))
                return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _format_slice(ds, tables: list[str]) -> str | None:
    by = {t["name"]: t for t in ds.tables}
    sel = [by[t] for t in tables if t in by]
    if not sel:
        return None
    sel_names = {t["name"] for t in sel}
    out = [
        f'You generate PostgreSQL SELECT queries over the "{ds.dataset_name}" dataset.',
        "You ONLY produce a single SELECT statement, never DDL or DML.",
        "(Only the tables most relevant to this question are shown.)",
        "", "== TABLES ==",
    ]
    for t in sel:
        out.append(f"\n{t['name']} ({t.get('row_count', '?')} rows)"
                   + (f" — {t['description']}" if t.get("description") else ""))
        for c in t.get("columns", []):
            seg = f"  {c['name']} ({c.get('type', 'text')})"
            if c.get("business_name"):
                seg += f" — {c['business_name']}"
            if c.get("synonyms"):
                seg += " (also: " + ", ".join(c["synonyms"]) + ")"
            out.append(seg)
        if t.get("pk"):
            out.append(f"  primary key: {t['pk']}")
    jk = [f"  {t['name']}.{fk['column']} -> {fk['ref_table']}.{fk['ref_column']}"
          for t in sel for fk in t.get("fks", []) if fk["ref_table"] in sel_names]
    if jk:
        out += ["", "== JOIN KEYS =="] + jk
    out += ["", "== RULES ==",
            "1. Output ONE valid PostgreSQL SELECT. Nothing else — no DDL/DML.",
            "2. Always include LIMIT (default 100).",
            "3. In any JOIN, qualify every column with its table alias.",
            "4. Use ILIKE '%phrase%' for partial text matches, not = equality.",
            "5. Use ONLY the tables and columns listed above."]
    return "\n".join(out)


def select_schema_slice(ds, question: str) -> tuple[str | None, list[str]]:
    """Return (focused_context, selected_tables) or (None, []) if unavailable."""
    tables = _rank_tables(ds.project_id, question, SLICE_TABLES)
    if not tables:
        return None, []
    return _format_slice(ds, tables), tables
