"""Row loader — coerce a confirmed profile's sheets to typed Python values and
bulk-insert them into the generated tables. Generalises Yokohama's per-entity
ingest.py into a profile-driven loop.

Type coercion mirrors the profiler's logical types. Coercion is forgiving: an
unconvertible cell becomes NULL rather than aborting the load (real uploads are
messy). PK / NOT-NULL violations still surface as load errors — by design, so a
bad confirmation is caught rather than silently dropping data.
"""
from __future__ import annotations

import pandas as pd
from psycopg2.extras import execute_values

from generators import SURROGATE_PK
from profiler import read_sheet


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or \
        str(v).strip().lower() in ("", "nan", "none", "nat")


def _coerce(v, logical_type: str):
    if _blank(v):
        return None
    try:
        if logical_type == "integer":
            return int(round(float(str(v).replace(",", ""))))
        if logical_type == "decimal":
            return float(str(v).replace(",", ""))
        if logical_type == "boolean":
            return str(v).strip().lower() in ("true", "yes", "y", "t", "1")
        if logical_type == "date":
            return pd.Timestamp(v).date()
        if logical_type == "datetime":
            return pd.Timestamp(v).to_pydatetime()
        return str(v)
    except Exception:
        return None        # unconvertible cell → NULL (don't fail the whole load)


def load_table(cur, schema: str, table: dict, df: pd.DataFrame) -> int:
    """Insert one table's rows. Returns the number of rows inserted.

    `table` is a confirmed table-profile dict; `df` is its sheet read at the
    confirmed header row. The surrogate _row_id (synth PK) is identity-generated
    and never inserted.
    """
    cols = [c for c in table.get("columns", []) if c.get("include", True)]
    names = [c["name"] for c in cols]
    present = [c for c in cols if c["name"] in df.columns]
    if not present:
        return 0
    series = {c["name"]: [_coerce(v, c.get("type", "text")) for v in df[c["name"]].tolist()]
              for c in present}
    n = len(df)
    records = [tuple(series[c["name"]][i] for c in present) for i in range(n)]
    if not records:
        return 0

    col_sql = ", ".join(f'"{c["name"]}"' for c in present)
    execute_values(
        cur,
        f'INSERT INTO "{schema}"."{table["name"]}" ({col_sql}) VALUES %s',
        records,
    )
    return len(records)


def load_workbook(conn, schema: str, profile: dict, source_paths: dict[str, str]) -> dict[str, int]:
    """Load every included table of a (possibly multi-file) profile. Each table
    carries an `upload_token` keying into source_paths {token: xlsx-path}.
    Returns {table: rows}."""
    loaded: dict[str, int] = {}
    only = next(iter(source_paths.values())) if len(source_paths) == 1 else None
    with conn.cursor() as cur:
        for t in profile.get("tables", []):
            if not t.get("include", True):
                continue
            path = source_paths.get(t.get("upload_token")) or only
            if not path:
                raise KeyError(f"no source file for table {t['name']} "
                               f"(upload_token={t.get('upload_token')})")
            df = read_sheet(path, t["sheet"], t["header_row"])
            loaded[t["name"]] = load_table(cur, schema, t, df)
    return loaded
