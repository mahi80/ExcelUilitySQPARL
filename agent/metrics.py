"""Config-driven metric helpers (M4) — pure Python, offline-testable.

A metric is a named, parameterised SELECT template the bot computes
deterministically (no LLM math). Params are filled from the question by type
(numbers/dates auto-extracted; text matched against author-listed values) or a
default, then rendered as safe SQL literals. The filled SQL still goes through
the normal SELECT-only / allowlist validator before execution.
"""
from __future__ import annotations

import re

_NUM_RE = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def match_metric(question: str, metrics: list[dict]) -> dict | None:
    """Longest metric name/synonym (>=4 chars) appearing in the question wins."""
    ql = question.lower()
    best, best_len = None, 0
    for m in metrics:
        for c in [m.get("name", "")] + (m.get("synonyms") or []):
            c = c.strip().lower()
            if len(c) >= 4 and c in ql and len(c) > best_len:
                best, best_len = m, len(c)
    return best


def extract_param(question: str, param: dict):
    """Pull a param value from the question by type; None if not found."""
    typ = param.get("type", "text")
    if typ in ("int", "decimal"):
        m = _NUM_RE.search(question)
        return m.group(1).replace(",", "") if m else None
    if typ == "date":
        m = _DATE_RE.search(question) or _YEAR_RE.search(question)
        return m.group(1) if m else None
    ql = question.lower()
    for v in (param.get("values") or []):
        if str(v).lower() in ql:
            return v
    return None


def param_literal(value, typ: str) -> str:
    if typ == "int":
        return str(int(float(str(value).replace(",", ""))))
    if typ == "decimal":
        return str(float(str(value).replace(",", "")))
    # text / date → quoted, single-quote-escaped (defence-in-depth; validator also runs)
    return "'" + str(value).replace("'", "''") + "'"


def fill_metric(metric: dict, question: str) -> tuple[str | None, str | None]:
    """Substitute params into the template. Returns (sql, error)."""
    sql = metric.get("sql_template", "")
    for p in metric.get("params", []):
        name = p["name"]
        val = extract_param(question, p)
        if val is None:
            val = p.get("default")
        if val is None:
            return None, f"please specify '{name}' for the '{metric.get('name')}' metric"
        lit = param_literal(val, p.get("type", "text"))
        sql = sql.replace("{{" + name + "}}", lit).replace("{{ " + name + " }}", lit)
    return sql, None
