"""Excel workbook profiler — the first stage of the onboarding pipeline.

Takes an uploaded .xlsx (one or more tabs) and infers a *proposed* relational
schema: one table per sheet, snake_case columns, a logical type per column,
nullability, distinct counts, sample values, a candidate primary key, and
candidate foreign keys (by column-name + value-overlap heuristics).

The output is a plain dict (JSON-serialisable) — the "profile". It is the input
to the UI review step (where a human confirms/corrects it) and then to the
generators (DDL / schema-context / allowlists). Nothing here touches Postgres;
inference is deliberately separated from generation so the human can intervene
in between (the accuracy backbone — see the design doc's risk section).

Pure-ish module: depends only on pandas + stdlib so it can be unit-tested
standalone, without Docker or a database.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# Logical types the profiler emits. Mapped to Postgres types by generators.py.
LOGICAL_TYPES = ("integer", "decimal", "boolean", "date", "datetime", "text")

_MAX_SCAN_HEADER = 15      # rows to scan when locating the header row
_TYPE_SAMPLE = 200         # non-null values sampled for type inference
_DISPLAY_SAMPLES = 5       # distinct sample values surfaced to the review UI


# ---------------------------------------------------------------------------
# Identifier normalisation
# ---------------------------------------------------------------------------

def snake_case(name: Any) -> str:
    """Normalise an arbitrary header label to a safe snake_case identifier."""
    s = str(name).strip()
    s = s.replace("%", " pct ").replace("#", " num ").replace("&", " and ")
    # split camelCase / PascalCase before lowercasing
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if not s:
        s = "col"
    if s[0].isdigit():
        s = "c_" + s
    return s


def _dedupe(names: list[str]) -> list[str]:
    """Ensure column names are unique by suffixing _2, _3, … on collisions."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen[n] = 1
            out.append(n)
        else:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
    return out


# ---------------------------------------------------------------------------
# Header-row detection
# ---------------------------------------------------------------------------

def _is_blankish(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or \
        str(v).strip().lower() in ("", "nan", "none", "nat")


def _looks_numeric(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return bool(re.fullmatch(r"[+-]?\d[\d,]*\.?\d*", str(v).strip()))


def detect_header_row(raw: pd.DataFrame, max_scan: int = _MAX_SCAN_HEADER) -> int:
    """Locate the most likely header row in a raw (header=None) sheet.

    Many real workbooks prefix tables with a title + blank rows (the Yokohama
    sample uses a 3-row prelude). We score each of the first `max_scan` rows on
    how header-like it is: mostly non-null *text* cells, distinct, with data
    populated in the row beneath it.
    """
    best_idx, best_score = 0, float("-inf")
    n = min(max_scan, len(raw))
    for i in range(n):
        cells = list(raw.iloc[i])
        nonnull = [c for c in cells if not _is_blankish(c)]
        if not nonnull:
            continue
        text_cells = [c for c in nonnull if isinstance(c, str) and not _looks_numeric(c)]
        distinct = len({str(c).strip().lower() for c in nonnull})
        below_nonnull = 0
        if i + 1 < len(raw):
            below_nonnull = sum(1 for c in raw.iloc[i + 1] if not _is_blankish(c))

        score = len(text_cells) + 0.5 * distinct
        if below_nonnull >= max(1, len(nonnull) * 0.5):
            score += 2                                  # real data sits under a header
        if len(nonnull) and len(text_cells) / len(nonnull) < 0.5:
            score -= 3                                  # mostly numeric → looks like data
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx


def read_sheet(path: str | Path, sheet: str, header_row: int) -> pd.DataFrame:
    """Read one sheet at a known header row → a cleaned DataFrame with
    snake_case, de-duplicated column names and stripped string cells.

    Shared by the profiler (inference) and the loader (ingest) so both see an
    identical frame for a given (sheet, header_row).
    """
    df = pd.read_excel(path, sheet_name=sheet, header=header_row, dtype=object)
    df = df.dropna(how="all").reset_index(drop=True)
    # drop fully-empty / "Unnamed:" auto columns
    keep = [c for c in df.columns
            if not (isinstance(c, str) and c.startswith("Unnamed:") and df[c].isna().all())]
    df = df[keep]
    df.columns = _dedupe([snake_case(c) for c in df.columns])
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(
                lambda v: None if _is_blankish(v) else (v.strip() if isinstance(v, str) else v))
    return df


# ---------------------------------------------------------------------------
# Per-column type inference
# ---------------------------------------------------------------------------

# A multi-digit string with a leading zero ('007', '000000010012', '000010') is
# an identifier/code, NOT a number — casting it to int silently drops the zeros.
_LEADING_ZERO = re.compile(r"[+-]?0\d+")


def _is_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return float(v).is_integer()
    s = str(v).strip()
    if _LEADING_ZERO.fullmatch(s):
        return False
    return bool(re.fullmatch(r"[+-]?\d+", s))


def _is_num(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    s = str(v).strip().replace(",", "")
    if _LEADING_ZERO.fullmatch(s):
        return False
    return bool(re.fullmatch(r"[+-]?\d*\.?\d+([eE][+-]?\d+)?", s))


def _as_ts(v: Any) -> pd.Timestamp | None:
    """Coerce any native temporal value (incl. numpy.datetime64, which pandas
    hands back for Excel date cells) to a pd.Timestamp; None if not temporal."""
    if isinstance(v, (pd.Timestamp, _dt.datetime, _dt.date, np.datetime64)):
        try:
            return pd.Timestamp(v)
        except Exception:
            return None
    return None


def _is_datelike(v: Any) -> bool:
    if _as_ts(v) is not None:
        return True
    s = str(v).strip()
    if not (re.search(r"[-/:]", s) and re.search(r"\d", s)):
        return False
    try:
        pd.to_datetime(s)
        return True
    except Exception:
        return False


def _has_time(v: Any) -> bool:
    ts = _as_ts(v)
    if ts is None and _is_datelike(v):
        try:
            ts = pd.Timestamp(str(v))
        except Exception:
            ts = None
    return bool(ts is not None and (ts.hour or ts.minute or ts.second))


def infer_type(series: pd.Series) -> str:
    """Infer a logical type from a column's non-null sample."""
    sample = [v for v in series.tolist() if not _is_blankish(v)][:_TYPE_SAMPLE]
    if not sample:
        return "text"

    if all(_as_ts(v) is not None for v in sample):
        return "datetime" if any(_has_time(v) for v in sample) else "date"

    low = {str(v).strip().lower() for v in sample}
    if low <= {"true", "false", "yes", "no", "t", "f"}:
        return "boolean"

    if all(_is_int(v) for v in sample):
        return "integer"
    if all(_is_num(v) for v in sample):
        return "decimal"

    if all(_is_datelike(v) for v in sample):
        return "datetime" if any(_has_time(v) for v in sample) else "date"

    return "text"


# ---------------------------------------------------------------------------
# Key detection
# ---------------------------------------------------------------------------

def _pk_rank(col: str) -> int:
    cl = col.lower()
    if cl == "id":
        return 0
    if cl.endswith("_id") or cl.endswith("id"):
        return 1
    if cl in ("sku", "code", "key") or cl.endswith("_code") or cl.endswith("_no"):
        return 2
    return 3


def detect_pk(df: pd.DataFrame) -> str | None:
    """A column is a PK candidate iff it is fully populated AND fully unique."""
    n = len(df)
    if n == 0:
        return None
    candidates = [c for c in df.columns
                  if df[c].notna().sum() == n and df[c].nunique(dropna=True) == n]
    candidates.sort(key=_pk_rank)
    return candidates[0] if candidates else None


def _singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def detect_fks(tables: list[dict], frames: dict[str, pd.DataFrame]) -> None:
    """Annotate each table dict in-place with a `fks` list.

    Heuristic: a column is a FK candidate when its name matches another table's
    PK (or <table>_id / <table>) AND at least half its distinct values overlap
    that PK's values. Self-references are skipped.
    """
    pk_index = {t["name"]: t["pk"] for t in tables if t.get("pk")}
    pk_values: dict[str, set[str]] = {
        name: {str(v) for v in frames[name][pk].dropna().unique()}
        for name, pk in pk_index.items()
    }
    for t in tables:
        name = t["name"]
        df = frames[name]
        fks: list[dict] = []
        for col in [c["name"] for c in t["columns"]]:
            if col == t.get("pk"):
                continue
            cl = col.lower()
            for tgt, tpk in pk_index.items():
                if tgt == name:
                    continue
                tl = tgt.lower()
                name_match = cl in (
                    tpk.lower(), tl, f"{tl}_id", f"{_singular(tl)}_id", f"{_singular(tl)}",
                )
                if not name_match:
                    continue
                src_vals = {str(v) for v in df[col].dropna().unique()}
                if not src_vals:
                    continue
                overlap = len(src_vals & pk_values[tgt]) / len(src_vals)
                if overlap >= 0.5:
                    fks.append({"column": col, "ref_table": tgt, "ref_column": tpk,
                                "overlap": round(overlap, 2)})
                    break       # one FK target per column is enough for M1
        t["fks"] = fks


# ---------------------------------------------------------------------------
# Workbook → profile
# ---------------------------------------------------------------------------

def _fmt_sample(v: Any) -> str:
    """Render a sample value cleanly for the review UI (no '1000.0' / nanosecond
    timestamp noise)."""
    ts = _as_ts(v)
    if ts is not None:
        return ts.strftime("%Y-%m-%d %H:%M:%S") if (ts.hour or ts.minute or ts.second) \
            else ts.strftime("%Y-%m-%d")
    if isinstance(v, float) and float(v).is_integer():
        return str(int(v))
    return str(v)


def _column_profile(series: pd.Series, n_rows: int) -> dict:
    nonnull = series.dropna()
    distinct_vals = list(pd.unique(nonnull))
    samples = [_fmt_sample(v) for v in distinct_vals[:_DISPLAY_SAMPLES]]
    return {
        "name": series.name,
        "type": infer_type(series),
        "nullable": bool(series.notna().sum() < n_rows),
        "distinct": int(len(distinct_vals)),
        "samples": samples,
        # data-dictionary fields — filled by a data_dictionary sheet or the review UI (M2)
        "business_name": None,
        "description": None,
        "synonyms": [],
        "unit": None,
    }


# Sheets recognised as a data dictionary rather than a data table.
_DICT_SHEET_NAMES = {"data_dictionary", "dictionary", "data_dict"}


def _dedupe_table_names(tables: list[dict], frames: dict[str, pd.DataFrame]) -> None:
    """Resolve snake_case collisions within a single workbook (orders, orders_2)."""
    seen: dict[str, int] = {}
    for t in tables:
        base = t["name"]
        if base in seen:
            seen[base] += 1
            t["name"] = f"{base}_{seen[base]}"
            frames[t["name"]] = frames.pop(base)
        else:
            seen[base] = 1


def profile_frames(path: str | Path) -> dict:
    """Profile ONE workbook into (tables, frames, dict_rows) without FK detection
    or dictionary merge — those run after a single/multi-file set is assembled.
    `frames` is retained so the caller can run cross-table FK detection."""
    path = Path(path)
    xls = pd.ExcelFile(path)
    tables: list[dict] = []
    frames: dict[str, pd.DataFrame] = {}
    dict_rows: list[dict] = []
    for sheet in xls.sheet_names:
        raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=object)
        if raw.dropna(how="all").empty:
            continue
        header_row = detect_header_row(raw)
        df = read_sheet(path, sheet, header_row)
        if df.empty or len(df.columns) == 0:
            continue
        if snake_case(sheet) in _DICT_SHEET_NAMES:
            dict_rows.extend(df.to_dict(orient="records"))   # a dictionary, not a table
            continue
        tname = snake_case(sheet)
        frames[tname] = df
        tables.append({
            "name": tname,
            "sheet": sheet,
            "source_file": path.name,
            "header_row": int(header_row),
            "row_count": int(len(df)),
            "include": True,
            "pk": detect_pk(df),
            "synth_pk": detect_pk(df) is None,
            "columns": [_column_profile(df[c], len(df)) for c in df.columns],
            "fks": [],
        })
    _dedupe_table_names(tables, frames)
    return {"source_file": path.name, "tables": tables, "frames": frames, "dict_rows": dict_rows}


def merge_data_dictionary(profile: dict, dict_rows: list[dict]) -> dict:
    """Apply data-dictionary rows onto a profile (dictionary overrides inference).
    Row keys are snake_cased headers: table, column, business_name, description,
    synonyms, unit (+ term/definition rows → dataset glossary)."""
    by_table = {t["name"]: t for t in profile.get("tables", [])}
    glossary = profile.setdefault("glossary", [])

    def _val(row, key):
        v = row.get(key)
        return str(v).strip() if v not in (None, "") else ""

    for row in dict_rows:
        term = _val(row, "term")
        tbl = _val(row, "table")
        if term and not tbl:
            glossary.append({"term": term, "definition": _val(row, "definition")})
            continue
        t = by_table.get(snake_case(tbl)) if tbl else None
        if not t:
            continue
        col = _val(row, "column")
        if not col:                                   # table-level description
            if _val(row, "description"):
                t["description"] = _val(row, "description")
            continue
        cname = snake_case(col)
        for c in t["columns"]:
            if c["name"] == cname:
                if _val(row, "business_name"):
                    c["business_name"] = _val(row, "business_name")
                if _val(row, "description"):
                    c["description"] = _val(row, "description")
                if _val(row, "unit"):
                    c["unit"] = _val(row, "unit")
                if _val(row, "synonyms"):
                    c["synonyms"] = [s.strip() for s in re.split(r"[,;]", _val(row, "synonyms"))
                                     if s.strip()]
                break
    return profile


def profile_workbook(path: str | Path, dataset_name: str | None = None) -> dict:
    """Profile a single workbook into a reviewable schema proposal."""
    path = Path(path)
    pf = profile_frames(path)
    detect_fks(pf["tables"], pf["frames"])
    profile = {
        "dataset_name": dataset_name or snake_case(path.stem),
        "source_file": pf["source_file"],
        "domain": None,
        "glossary": [],
        "tables": pf["tables"],
    }
    if pf["dict_rows"]:
        merge_data_dictionary(profile, pf["dict_rows"])
    return profile


def merge_profiles(pfs: list[dict], dataset_name: str) -> dict:
    """Merge several profile_frames() results into ONE dataset profile.
    Table names colliding across files are prefixed with the file stem
    (sales__orders); FK detection runs over the union of all frames."""
    counts: dict[str, int] = {}
    for pf in pfs:
        for t in pf["tables"]:
            counts[t["name"]] = counts.get(t["name"], 0) + 1

    all_tables: list[dict] = []
    all_frames: dict[str, pd.DataFrame] = {}
    all_dicts: list[dict] = []
    for pf in pfs:
        prefix = snake_case(Path(pf["source_file"]).stem)
        for t in pf["tables"]:
            orig = t["name"]
            frame = pf["frames"][orig]
            if counts[orig] > 1:                       # cross-file collision → prefix
                t["name"] = f"{prefix}__{orig}"
            all_tables.append(t)
            all_frames[t["name"]] = frame
        all_dicts.extend(pf["dict_rows"])

    detect_fks(all_tables, all_frames)
    profile = {
        "dataset_name": dataset_name,
        "source_file": ", ".join(pf["source_file"] for pf in pfs),
        "domain": None,
        "glossary": [],
        "tables": all_tables,
    }
    if all_dicts:
        merge_data_dictionary(profile, all_dicts)
    return profile


if __name__ == "__main__":          # quick manual smoke test
    import json
    import sys
    prof = profile_workbook(sys.argv[1])
    print(json.dumps(prof, indent=2, default=str))
