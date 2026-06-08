"""PageIndex builder (Phase-2) — embed a dataset's tables/columns into pgvector
so the agent can retrieve a focused schema slice for large schemas.
"""
from __future__ import annotations

from psycopg2.extras import execute_values

import embedder


def _table_content(t: dict) -> str:
    cols = ", ".join(c["name"] for c in t.get("columns", []) if c.get("include", True))
    desc = f": {t['description']}" if t.get("description") else ""
    return f"table {t['name']}{desc}. columns: {cols}"


def _column_content(table: str, c: dict) -> str:
    parts = [f"{table}.{c['name']}"]
    if c.get("business_name"):
        parts.append(c["business_name"])
    if c.get("synonyms"):
        parts.append(", ".join(c["synonyms"]))
    if c.get("description"):
        parts.append(c["description"])
    parts.append(f"type {c.get('type', 'text')}")
    if c.get("samples"):
        parts.append("examples: " + ", ".join(str(s) for s in c["samples"][:5]))
    return " | ".join(parts)


def index_schema(conn, project_id: str, profile: dict) -> int:
    """Embed every included table + column and (re)store the vectors. Returns the
    row count. Caller wraps in try/except — PageIndex is best-effort."""
    rows: list[tuple] = []
    for t in profile.get("tables", []):
        if not t.get("include", True):
            continue
        rows.append(("table", t["name"], None, _table_content(t)))
        for c in t.get("columns", []):
            if c.get("include", True):
                rows.append(("column", t["name"], c["name"], _column_content(t["name"], c)))
    if not rows:
        return 0
    vecs = embedder.embed([r[3] for r in rows])
    with conn.cursor() as cur:
        cur.execute("DELETE FROM meta.schema_embedding WHERE project_id=%s", (project_id,))
        execute_values(
            cur,
            "INSERT INTO meta.schema_embedding "
            "(project_id, kind, table_name, column_name, content, embedding) VALUES %s",
            [(project_id, r[0], r[1], r[2], r[3], embedder.to_pgvector(v))
             for r, v in zip(rows, vecs)],
        )
    return len(rows)
