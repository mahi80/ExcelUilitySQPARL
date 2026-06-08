"""LangGraph nodes — generalised from the Yokohama agent, SQL path only (M1).

Pipeline:
  retrieve → policy_check ─[block]─► compose
                          └[allow]─► generate_sql → validate_sql →
      [error & repairs left] → repair_prep → generate_sql (loop)
      [error & exhausted]    → compose
      [ok]                   → score_query →
                                   [low conf & repairs left] → repair_prep
                                   [else]                     → execute_sql →
                                       compose_insights → compose

Everything domain-specific is read from the active dataset's artifact (config.py)
— schema context, validator allowlists, target schema. SPARQL / metrics / catalog
arrive in later milestones.
"""
from __future__ import annotations

import datetime as dt
import json as _json
import os
import re
import time
from typing import Any, TypedDict

import anthropic
import httpx
import psycopg2
import psycopg2.extras

import config
from metrics import fill_metric, match_metric
from validators import validate as validate_sql_text
from validators import validate_result_graph
from validators import validate_sparql as validate_sparql_text


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State(TypedDict, total=False):
    question: str
    project: str | None
    target_lang: str
    sql_context: str | None
    sparql_context: str | None
    sql_raw: str | None
    sparql_raw: str | None
    sql: str | None
    sparql: str | None
    shacl_report: dict[str, Any] | None
    metric_id: str | None
    metric_name: str | None
    validator_error: str | None
    validator_notes: list[str] | None
    repair_count: int
    repair_feedback: str | None
    rows: list[dict[str, Any]] | None
    execution_error: str | None
    answer: str | None
    policy_block: bool
    confidence: float
    summary: str | None
    insights: list[str] | None
    follow_ups: list[str] | None
    trace: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
POSTGRES_RO_DSN = os.environ["POSTGRES_RO_DSN"]
TOKEN_BUDGET = int(os.environ.get("TOKEN_BUDGET_PER_QUERY", "8000"))
MAX_REPAIR_ATTEMPTS = int(os.environ.get("MAX_REPAIR_ATTEMPTS", "2"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.7"))
ONTOP_SPARQL_ENDPOINT = os.environ.get("ONTOP_SPARQL_ENDPOINT", "http://ontop:8080/sparql")


def _trace(state: State, step: str, **fields):
    state.setdefault("trace", []).append({
        "step": step,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        **fields,
    })


_FENCE_RE = re.compile(r"```(?:sql|sparql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract(text: str) -> str:
    m = _FENCE_RE.search(text)
    return (m.group(1) if m else text).strip()


# ---------------------------------------------------------------------------
# 1. Retrieve — load the active dataset's schema context
# ---------------------------------------------------------------------------

def retrieve(state: State) -> dict:
    ds = config.load_dataset(state.get("project"))
    if ds is None:
        _trace(state, "retrieve", source="none", dataset=None)
        return {
            "policy_block": True,
            "target_lang": "sql",
            "answer": ("No dataset has been onboarded yet. Upload an Excel workbook "
                       "to create one, then ask questions about it."),
            "trace": state.get("trace", []),
        }
    _trace(state, "retrieve", source="artifact", dataset=ds.dataset_name,
           tables=len(ds.known_tables))
    return {"sql_context": ds.schema_context, "sparql_context": ds.sparql_context,
            "trace": state.get("trace", [])}


# ---------------------------------------------------------------------------
# 1b. Policy & safety — injection hard-block, off-domain creative refusal, PII flag
# ---------------------------------------------------------------------------

_INJECTION = re.compile(
    r"\b(drop\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set|truncate\s+table)\b"
    r"|;\s*--|'\s*;\s*"
    r"|ignore\s+(your|previous|all|the|above)\s+(instructions?|prompts?|rules?|directions?)"
    r"|disregard\s+(your|previous|all|the|above)"
    r"|reveal\s+(your|the)\s+(system\s+)?prompt|your\s+system\s+prompt",
    re.IGNORECASE,
)
_OUT_OF_DOMAIN = re.compile(
    r"\b(weather|news|stock\s+(price|market)|crypto|recipe|joke|song|poem|"
    r"write\s+(me\s+)?(a|an|some)\s+(poem|story|song|essay|haiku|limerick|rap|tweet|script|joke)|"
    r"meaning\s+of\s+life)\b",
    re.IGNORECASE,
)
_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn-like"),
    (re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.IGNORECASE), "email"),
]


def policy_check(state: State) -> dict:
    if state.get("policy_block"):           # retrieve already short-circuited
        return {"trace": state.get("trace", [])}
    q = state.get("question", "")
    if _INJECTION.search(q) or _OUT_OF_DOMAIN.search(q):
        reason = "injection-attempt" if _INJECTION.search(q) else "out-of-domain"
        _trace(state, "policy_check", verdict="block", reason=reason)
        ds = config.load_dataset(state.get("project"))
        name = ds.dataset_name if ds else "this dataset"
        return {
            "policy_block": True,
            "answer": (f"I can't help with that. I answer questions about the "
                       f"**{name}** data — try asking about the records, totals, or "
                       f"relationships in it."),
            "trace": state["trace"],
        }
    found_pii = [label for pat, label in _PII_PATTERNS if pat.search(q)]
    _trace(state, "policy_check", verdict="allow", pii_flagged=found_pii or None)
    return {"policy_block": False, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 2. Generate SQL
# ---------------------------------------------------------------------------

def _cumulative_tokens(state: State) -> int:
    total = 0
    for s in state.get("trace", []):
        if s.get("step") == "llm_call":
            total += (s.get("input_tokens") or 0) + (s.get("output_tokens") or 0)
    return total


def _call_claude(system: str, user_messages: list[dict], state: State) -> tuple[str | None, str | None]:
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("sk-ant-REPLACE"):
        return None, "ANTHROPIC_API_KEY not configured — set it in .env."
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    started = time.perf_counter()
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=1024, system=system, messages=user_messages)
    except anthropic.APIError as e:
        return None, f"LLM call failed: {e}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    total = in_tok + out_tok
    _trace(state, "llm_call", model=ANTHROPIC_MODEL, latency_ms=latency_ms,
           input_tokens=in_tok, output_tokens=out_tok,
           cost_guard_budget=TOKEN_BUDGET, cost_guard_breach=(total > TOKEN_BUDGET),
           repair_attempt=state.get("repair_count", 0))
    if total > TOKEN_BUDGET:
        return text, f"cost guard: {total} tokens exceeds budget {TOKEN_BUDGET}"
    return text, None


def _call_claude_with_tool(system: str, user_messages: list[dict], tool: dict, state: State,
                           max_tokens: int = 1024, step_label: str = "llm_call_tool"
                           ) -> tuple[dict | None, str | None]:
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("sk-ant-REPLACE"):
        return None, "ANTHROPIC_API_KEY not configured"
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    started = time.perf_counter()
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system,
            messages=user_messages, tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]})
    except anthropic.APIError as e:
        return None, f"LLM tool-use call failed: {e}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    total = in_tok + out_tok
    parsed = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
            parsed = dict(block.input)
            break
    _trace(state, step_label, model=ANTHROPIC_MODEL, latency_ms=latency_ms,
           input_tokens=in_tok, output_tokens=out_tok, tool_returned=parsed is not None)
    _trace(state, "llm_call", model=ANTHROPIC_MODEL, latency_ms=latency_ms,
           input_tokens=in_tok, output_tokens=out_tok, via=step_label)
    if total > TOKEN_BUDGET:
        return parsed, f"cost guard: {total} tokens exceeds budget {TOKEN_BUDGET}"
    if parsed is None:
        return None, "model did not call the expected tool"
    return parsed, None


def _build_messages(question: str, feedback: str | None, lang: str = "SQL") -> list[dict]:
    msgs = [{"role": "user", "content": question}]
    if feedback:
        msgs.append({"role": "user", "content": (
            f"Your previous {lang} was invalid: {feedback}\n"
            f"Generate a corrected {lang} that fixes this. Output ONLY the {lang}, no prose.")})
    return msgs


def generate_sql(state: State) -> dict:
    msgs = _build_messages(state["question"], state.get("repair_feedback"), "SQL")
    text, err = _call_claude(state.get("sql_context") or "", msgs, state)
    if err:
        _trace(state, "generate_sql", error=err)
        return {"sql_raw": text, "validator_error": err, "trace": state["trace"]}
    sql = _extract(text or "")
    _trace(state, "generate_sql", chars=len(sql), repair_attempt=state.get("repair_count", 0))
    return {"sql_raw": sql, "validator_error": None, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# Planner — SQL (default) vs SPARQL (explicit hint)
# ---------------------------------------------------------------------------

_SPARQL_HINTS = ("via sparql", "as sparql", "use sparql", "via the ontology",
                 "ontology", "sparql", "rdf", "semantic graph", "knowledge graph",
                 "linked data")
_SQL_HINTS = ("via sql", "as sql", "use sql")


def plan_query(state: State) -> dict:
    raw = state["question"]
    q = raw.lower()
    ds = config.load_dataset(state.get("project"))
    metric = match_metric(raw, ds.metrics if ds else [])
    if metric:
        _trace(state, "plan", target_lang="metric", reason=f"metric match: {metric['name']}")
        return {"target_lang": "metric", "metric_id": metric["id"],
                "metric_name": metric["name"], "trace": state["trace"]}
    if any(h in q for h in _SQL_HINTS):
        target, reason = "sql", "explicit SQL hint"
    elif any(h in q for h in _SPARQL_HINTS):
        target, reason = "sparql", "SPARQL/ontology hint"
    else:
        target, reason = "sql", "default — SQL covers most questions"
    _trace(state, "plan", target_lang=target, reason=reason)
    return {"target_lang": target, "trace": state["trace"]}


def generate_sparql(state: State) -> dict:
    msgs = _build_messages(state["question"], state.get("repair_feedback"), "SPARQL")
    text, err = _call_claude(state.get("sparql_context") or "", msgs, state)
    if err:
        _trace(state, "generate_sparql", error=err)
        return {"sparql_raw": text, "validator_error": err, "trace": state["trace"]}
    sparql = _extract(text or "")
    _trace(state, "generate_sparql", chars=len(sparql), repair_attempt=state.get("repair_count", 0))
    return {"sparql_raw": sparql, "validator_error": None, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 3. Validate
# ---------------------------------------------------------------------------

def validate_sql(state: State) -> dict:
    raw = state.get("sql_raw")
    if not raw:
        return {"trace": state["trace"]}
    ds = config.load_dataset(state.get("project"))
    known_tables = ds.known_tables if ds else set()
    known_columns = ds.known_columns if ds else {}
    r = validate_sql_text(raw, known_tables, known_columns)
    _trace(state, "validate_sql", ok=r.ok, error=r.error, notes=r.notes)
    if not r.ok:
        return {"validator_error": r.error, "trace": state["trace"]}
    return {"sql": r.sql, "validator_notes": r.notes or [], "validator_error": None,
            "trace": state["trace"]}


def validate_sparql(state: State) -> dict:
    raw = state.get("sparql_raw")
    if not raw:
        return {"trace": state["trace"]}
    ds = config.load_dataset(state.get("project"))
    if not ds or not ds.ontology_path:
        return {"validator_error": "SPARQL is not available for this dataset.",
                "trace": state["trace"]}
    r = validate_sparql_text(raw, ds.ontology_path, ds.ontology_ns or "", ds.ontology_prefix or "")
    _trace(state, "validate_sparql", ok=r.ok, error=r.error, notes=r.notes)
    if not r.ok:
        return {"validator_error": r.error, "trace": state["trace"]}
    return {"sparql": r.sparql, "validator_notes": r.notes or [], "validator_error": None,
            "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 4. Repair prep
# ---------------------------------------------------------------------------

def repair_prep(state: State) -> dict:
    rc = state.get("repair_count", 0) + 1
    fb = (state.get("validator_error")
          or (f"low confidence ({state.get('confidence', 0):.2f}) — tighten the query"
              if state.get("confidence", 1.0) < CONFIDENCE_THRESHOLD else None)
          or "(unspecified)")
    _trace(state, "repair", attempt=rc, feedback=fb[:200])
    return {"repair_count": rc, "repair_feedback": fb, "sql_raw": None, "sparql_raw": None,
            "validator_error": None, "confidence": 0.0, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 5. Confidence score (domain-agnostic heuristic)
# ---------------------------------------------------------------------------

def score_query(state: State) -> dict:
    target = state.get("target_lang", "sql")
    query = state.get("sql") if target == "sql" else state.get("sparql")
    if not query:
        _trace(state, "score_query", confidence=0.0, components=["no-query"])
        return {"confidence": 0.0, "trace": state["trace"]}
    score, parts = 0.6, ["validator-clean:+0.6"]
    q_terms = set(re.findall(r"[a-z]\w{2,}", state.get("question", "").lower()))
    query_terms = set(re.findall(r"[a-z]\w{2,}", query.lower()))
    overlap = q_terms & query_terms
    if len(overlap) >= 3:
        score += 0.2; parts.append(f"term-overlap({len(overlap)}):+0.2")
    elif len(overlap) >= 1:
        score += 0.1; parts.append(f"term-overlap({len(overlap)}):+0.1")
    notes = state.get("validator_notes") or []
    if not any("auto-applied LIMIT" in n for n in notes):
        score += 0.1; parts.append("explicit-limit:+0.1")
    upper = query.upper()
    if target == "sql" and " JOIN " in upper:
        score += 0.1; parts.append("has-join:+0.1")
    elif target == "sparql" and (upper.count(";") + upper.count(" .")) >= 3:
        score += 0.1; parts.append("multi-triple:+0.1")
    score = min(score, 1.0)
    _trace(state, "score_query", confidence=round(score, 2), components=parts,
           threshold=CONFIDENCE_THRESHOLD)
    return {"confidence": score, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 6. Execute (RO sandbox, scoped to the dataset schema via search_path)
# ---------------------------------------------------------------------------

def execute_sql(state: State) -> dict:
    if state.get("validator_error") or not state.get("sql"):
        return {"trace": state["trace"]}
    ds = config.load_dataset(state.get("project"))
    schema = ds.schema if ds else "public"
    sql = state["sql"]
    started = time.perf_counter()
    try:
        with psycopg2.connect(POSTGRES_RO_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f'SET search_path TO "{schema}", public')
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as e:
        _trace(state, "execute_sql", error=str(e),
               latency_ms=int((time.perf_counter() - started) * 1000))
        return {"execution_error": str(e), "trace": state["trace"]}
    _trace(state, "execute_sql", row_count=len(rows),
           latency_ms=int((time.perf_counter() - started) * 1000))
    return {"rows": rows, "trace": state["trace"]}


def _shacl_validate_results(uri_subjects: set[str], shapes_path: str) -> dict:
    """Fetch the result subjects' triples via an internal CONSTRUCT and run SHACL
    on them (the CONSTRUCT is built from result IRIs, not the LLM)."""
    if not uri_subjects or not shapes_path:
        return {"conforms": True, "validated_nodes": 0, "violations": 0, "summary": []}
    from rdflib import Graph
    values = " ".join(f"<{u}>" for u in list(uri_subjects)[:200])
    construct = ("CONSTRUCT { ?s ?p ?o . ?o ?p2 ?o2 } "
                 "WHERE { VALUES ?s { " + values + " } ?s ?p ?o . OPTIONAL { ?o ?p2 ?o2 } }")
    try:
        resp = httpx.post(ONTOP_SPARQL_ENDPOINT, content=construct.encode("utf-8"),
                          headers={"Content-Type": "application/sparql-query",
                                   "Accept": "text/turtle"}, timeout=30.0)
        resp.raise_for_status()
        g = Graph()
        g.parse(data=resp.text, format="turtle")
    except Exception as e:  # noqa: BLE001
        return {"conforms": None, "validated_nodes": len(uri_subjects), "violations": None,
                "summary": [], "note": f"result-graph fetch failed: {e}"}
    return validate_result_graph(g, shapes_path)


def execute_sparql(state: State) -> dict:
    if state.get("validator_error") or not state.get("sparql"):
        return {"trace": state["trace"]}
    ds = config.load_dataset(state.get("project"))
    sparql = state["sparql"]
    started = time.perf_counter()
    try:
        resp = httpx.post(ONTOP_SPARQL_ENDPOINT, content=sparql.encode("utf-8"),
                          headers={"Content-Type": "application/sparql-query",
                                   "Accept": "application/sparql-results+json"}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        _trace(state, "execute_sparql", error=str(e),
               latency_ms=int((time.perf_counter() - started) * 1000))
        return {"execution_error": f"SPARQL endpoint error: {e}", "trace": state["trace"]}

    vars_ = data.get("head", {}).get("vars", [])
    bindings = data.get("results", {}).get("bindings", [])
    rows, uri_subjects = [], set()
    for b in bindings:
        row = {}
        for var in vars_:
            cell = b.get(var, {})
            row[var] = cell.get("value")
            if cell.get("type") == "uri" and cell.get("value"):
                uri_subjects.add(cell["value"])
        rows.append(row)
    _trace(state, "execute_sparql", row_count=len(rows),
           latency_ms=int((time.perf_counter() - started) * 1000), endpoint=ONTOP_SPARQL_ENDPOINT)

    shacl = _shacl_validate_results(uri_subjects, ds.shapes_path if ds else "")
    _trace(state, "shacl_post_exec", **{k: v for k, v in shacl.items() if k != "summary"})
    return {"rows": rows, "shacl_report": shacl, "trace": state["trace"]}


# ---------------------------------------------------------------------------
# Config-driven metric executor (M4) — deterministic, no LLM math
# ---------------------------------------------------------------------------

def execute_metric(state: State) -> dict:
    ds = config.load_dataset(state.get("project"))
    metric = next((m for m in (ds.metrics if ds else []) if m.get("id") == state.get("metric_id")), None)
    if not metric:
        _trace(state, "execute_metric", error="metric not found")
        return {"validator_error": "Configured metric not found.", "trace": state["trace"]}

    sql, perr = fill_metric(metric, state["question"])
    if perr:
        _trace(state, "execute_metric", error=perr)
        return {"validator_error": perr, "trace": state["trace"]}

    # Same SELECT-only / allowlist guard as the LLM SQL path — the template is
    # author-supplied but params are rendered as safe literals.
    r = validate_sql_text(sql, ds.known_tables, ds.known_columns)
    if not r.ok:
        _trace(state, "execute_metric", error=r.error, sql=sql[:200])
        return {"validator_error": f"metric template rejected: {r.error}", "trace": state["trace"]}

    started = time.perf_counter()
    try:
        with psycopg2.connect(POSTGRES_RO_DSN) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f'SET search_path TO "{ds.schema}", public')
                cur.execute(r.sql)
                rows = [dict(x) for x in cur.fetchall()]
    except psycopg2.Error as e:
        _trace(state, "execute_metric", error=str(e))
        return {"execution_error": str(e), "trace": state["trace"]}

    _trace(state, "execute_metric", metric=metric["name"], row_count=len(rows),
           latency_ms=int((time.perf_counter() - started) * 1000))
    return {"sql": r.sql, "rows": rows, "metric_name": metric["name"], "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 7. Compose insights (grounded summary + insights + follow-ups)
# ---------------------------------------------------------------------------

_INSIGHTS_TOOL = {
    "name": "compose_response",
    "description": "Output a structured response with summary, insights, and follow-up questions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One-sentence interpretation of the result."},
            "insights": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
            "follow_ups": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
        },
        "required": ["summary", "insights", "follow_ups"],
    },
}


def _insights_system(ds: config.Dataset | None) -> str:
    name = ds.dataset_name if ds else "the"
    tables = ", ".join(ds.table_names()) if ds else ""
    return f"""\
You are interpreting the result of a SQL query over the "{name}" dataset.
Tables available: {tables}.

Produce SUMMARY, INSIGHTS, FOLLOW_UPS via the compose_response tool.

GROUNDING — NON-NEGOTIABLE (a user may repeat these claims):
  - Use ONLY entities/values that appear in the result rows. NEVER name an
    entity (id, name, code, category) that is not present in the rows.
  - State ONLY facts the rows contain. Do NOT assert history, trends, rankings,
    or relationships the rows don't actually show.
  - ARITHMETIC: any total you state must equal the actual sum of the row values.
    Do NOT sum across different units/currencies. Recompute before writing a
    number; if unsure, say "about" or omit it.
  - When in doubt, leave it out. A shorter grounded insight beats an invented one.

SUMMARY: one conversational sentence — what we found, in plain English.
INSIGHTS (2-3 bullets): each cites specific numbers/entities from the rows and
  explains a comparison, why-it-matters, or structural point — not a restatement.
FOLLOW_UPS (2-3 questions): each answerable from the tables above, moving the
  user forward or drilling into an entity from the result.

ZERO-ROW CASE: if row count is 0, INSIGHTS MUST explain the likely reason — a
filter value not present in the data, or a question outside the dataset's coverage.
"""


def _summarize_rows_for_llm(rows: list[dict], cap: int = 15) -> str:
    if not rows:
        return "[] (zero rows)"
    body = _json.dumps(rows[:cap], default=str, indent=2)
    if len(rows) > cap:
        body += f"\n\n(+ {len(rows) - cap} more rows not shown)"
    return body


def _coerce_str_list(v: object) -> list[str]:
    if isinstance(v, list):
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        parts = [re.sub(r'^[\s\-*•.]+', "", p).strip().strip('"').strip()
                 for p in re.split(r"[\r\n]+", s)]
        parts = [p for p in parts if p]
        return parts if len(parts) > 1 else [s]
    return []


def compose_insights(state: State) -> dict:
    if state.get("policy_block"):
        return {"trace": state["trace"]}
    if state.get("validator_error") or state.get("execution_error"):
        return {"trace": state["trace"]}
    if _cumulative_tokens(state) >= TOKEN_BUDGET * 0.90:
        _trace(state, "compose_insights", skipped="cost-guard-near-breach")
        return {"trace": state["trace"]}

    rows = state.get("rows") or []
    user_payload = (
        f"User question:\n  {state['question']}\n\n"
        f"Row count: {len(rows)}\n\n"
        f"Generated query:\n```\n{state.get('sql') or state.get('sparql') or '(no query)'}\n```\n\n"
        f"Result rows (up to 15):\n{_summarize_rows_for_llm(rows)}"
    )
    parsed, err = _call_claude_with_tool(
        system=_insights_system(config.load_dataset(state.get("project"))),
        user_messages=[{"role": "user", "content": user_payload}],
        tool=_INSIGHTS_TOOL, state=state, max_tokens=1024,
        step_label="compose_insights_llm")
    if err or not parsed:
        _trace(state, "compose_insights", error=err, skipped="llm-failed")
        return {"trace": state["trace"]}
    summary = parsed.get("summary") or ""
    if isinstance(summary, list):
        summary = " ".join(str(x) for x in summary if x)
    insights = _coerce_str_list(parsed.get("insights"))
    follow_ups = _coerce_str_list(parsed.get("follow_ups"))
    _trace(state, "compose_insights", summary_len=len(str(summary)),
           insight_count=len(insights), follow_up_count=len(follow_ups))
    return {"summary": str(summary), "insights": insights[:3], "follow_ups": follow_ups[:3],
            "trace": state["trace"]}


# ---------------------------------------------------------------------------
# 8. Compose final answer
# ---------------------------------------------------------------------------

_CHIP_OPEN = "<<<FOLLOWUP_CHIPS>>>"
_CHIP_CLOSE = "<<<END>>>"


def _chip_payload(follow_ups: list[str] | None) -> str:
    if not follow_ups:
        return ""
    safe = [f.replace("|", "/") for f in follow_ups]
    return f"{_CHIP_OPEN}{'|'.join(safe)}{_CHIP_CLOSE}"


def _visible_keys(row: dict) -> list[str]:
    return [k for k in row.keys() if not str(k).startswith("__")]


def _pretty_header(key: str) -> str:
    label = str(key).replace("_", " ").strip().title()
    label = re.sub(r"\bPct\b", "%", label)
    label = re.sub(r"\bId\b", "ID", label)
    return label


def _render(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    s = str(v)
    # shorten ontology / resource URIs (SPARQL results) to their local name
    if s.startswith(("http://", "https://")):
        local = re.split(r"[#/]", s.rstrip("/"))[-1]
        if local:
            s = local
    return s.replace("|", "\\|").replace("\n", " ")


def compose(state: State) -> dict:
    if state.get("policy_block"):
        _trace(state, "compose", branch="upstream_answer")
        return {"trace": state["trace"]}

    err = state.get("validator_error") or state.get("execution_error")
    if err:
        attempts = state.get("repair_count", 0)
        suffix = f" (after {attempts} repair attempt{'s' if attempts != 1 else ''})" if attempts else ""
        _trace(state, "compose", branch="error", error=err)
        return {"answer": (f"I couldn't put that one together{suffix}. It may need a detail the "
                           "dataset doesn't track, or the question needs rephrasing. Try asking "
                           "about the records, totals, or relationships in the data."),
                "trace": state["trace"]}

    rows = state.get("rows") or []
    summary = state.get("summary")
    insights = state.get("insights") or []
    follow_ups = state.get("follow_ups") or []
    parts: list[str] = []

    if state.get("target_lang") == "metric" and state.get("metric_name"):
        parts.append(f"### Metric · {state['metric_name']}\n"
                     "_Computed deterministically — audited query below, no LLM arithmetic._")

    if summary:
        parts.append(f"### Summary\n{summary}")
    elif not rows:
        parts.append("### Summary\nNo matching records came back — usually that means the value "
                     "or filter you asked about isn't present in this dataset.")
    else:
        parts.append(f"### Summary\nFound {len(rows)} row(s).")

    if rows:
        keys = _visible_keys(rows[0])
        head = "| " + " | ".join(_pretty_header(k) for k in keys) + " |"
        sep = "| " + " | ".join("---" for _ in keys) + " |"
        body = "\n".join("| " + " | ".join(_render(r.get(k)) for k in keys) + " |" for r in rows[:20])
        more = f"\n_… +{len(rows) - 20} more rows_" if len(rows) > 20 else ""
        parts.append(f"### Data\n{head}\n{sep}\n{body}{more}")
    else:
        parts.append("### Data\n_No matching records in this dataset._")

    if insights:
        parts.append("### Insights\n" + "\n".join(f"- {i}" for i in insights))
    if follow_ups:
        parts.append("### Try next\n" + _chip_payload(follow_ups))

    target = state.get("target_lang", "sql")
    query_text = state.get("sparql") if target == "sparql" else state.get("sql")
    lang = "sparql" if target == "sparql" else "sql"
    if query_text:
        parts.append(f"<details><summary>Generated {lang.upper()}</summary>\n\n"
                     f"```{lang}\n{query_text}\n```\n</details>")
    shacl = state.get("shacl_report")
    if shacl and shacl.get("conforms") is False and shacl.get("violations"):
        parts.append(f"_⚠ SHACL: {shacl['violations']} data-quality violation(s) in the result graph._")

    _trace(state, "compose", branch="ok", row_count=len(rows), target=target,
           with_summary=bool(summary), with_insights=len(insights))
    return {"answer": "\n\n".join(parts), "trace": state["trace"]}


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

def route_after_policy(state: State) -> str:
    return "block" if state.get("policy_block") else "allow"


def route_after_plan(state: State) -> str:
    t = state.get("target_lang")
    if t == "metric":
        return "metric"
    return "sparql" if t == "sparql" else "sql"


def route_after_validate_sql(state: State) -> str:
    if state.get("validator_error"):
        return "repair" if state.get("repair_count", 0) < MAX_REPAIR_ATTEMPTS else "compose"
    return "score"


# SPARQL validate routes identically to SQL.
route_after_validate_sparql = route_after_validate_sql


def route_after_repair(state: State) -> str:
    return "sparql" if state.get("target_lang") == "sparql" else "sql"


def route_after_score(state: State) -> str:
    if state.get("confidence", 1.0) < CONFIDENCE_THRESHOLD \
       and state.get("repair_count", 0) < MAX_REPAIR_ATTEMPTS:
        return "repair"
    return "execute_sparql" if state.get("target_lang") == "sparql" else "execute_sql"
