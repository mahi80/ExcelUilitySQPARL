"""FastAPI entry point for the ExcelUtilitySPARQL agent (M1, SQL path)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import config
import evals
from graph import app_graph

app = FastAPI(title="ExcelUtilitySPARQL Agent", version="0.1.0")


class QueryRequest(BaseModel):
    q: str = Field(..., description="Natural-language question.")
    user: str | None = None
    project: str | None = Field(None, description="Active project id; None → default.")


class QueryResponse(BaseModel):
    answer: str
    target_lang: str | None = None
    sql: str | None = None
    sparql: str | None = None
    rows: list[dict[str, Any]] | None = None
    validator_error: str | None = None
    execution_error: str | None = None
    repair_count: int | None = None
    policy_block: bool | None = None
    confidence: float | None = None
    shacl_report: dict[str, Any] | None = None
    summary: str | None = None
    insights: list[str] | None = None
    follow_ups: list[str] | None = None
    trace: list[dict[str, Any]]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/dataset")
def dataset(project: str | None = None):
    """Surface a project's dataset so the UI can show a banner / gate the chat."""
    ds = config.load_dataset(project)
    if ds is None:
        return {"onboarded": False}
    return {
        "onboarded": True,
        "project_id": ds.project_id,
        "dataset_name": ds.dataset_name,
        "schema": ds.schema,
        "tables": ds.table_names(),
        "row_counts": ds.row_counts,
    }


@app.get("/projects")
def projects():
    """Ready projects for the UI picker."""
    return {"projects": config.list_projects()}


class EvalRequest(BaseModel):
    project: str | None = None
    questions: list[str] | None = None


@app.post("/evals/run")
async def evals_run(req: EvalRequest):
    """Generic quality eval: auto-generate questions from the dataset, run each
    through the agent, and LLM-judge correctness + grounding."""
    ds = config.load_dataset(req.project)
    if ds is None:
        return {"ok": False, "error": "No dataset onboarded."}
    qs = req.questions or evals.build_eval_set(ds)
    results: list[dict[str, Any]] = []
    for q in qs:
        try:
            final = await app_graph.ainvoke(
                {"question": q, "project": req.project, "trace": [], "repair_count": 0})
        except Exception as e:  # noqa: BLE001
            results.append({"question": q, "score": None, "reason": f"error: {e}"[:200]})
            continue
        query = final.get("sql") or final.get("sparql")
        rows = final.get("rows") or []
        v = evals.judge(q, final.get("answer") or "", query, rows)
        results.append({
            "question": q, "target_lang": final.get("target_lang"),
            "row_count": len(rows), "query": query, "score": v.get("score"),
            "grounded": v.get("grounded"), "relevant": v.get("relevant"),
            "reason": v.get("reason") or v.get("error"),
        })
    return evals.summarize(ds.dataset_name, ds.project_id, results)


def _payload(final: dict) -> dict:
    return {
        "answer": final.get("answer") or "",
        "target_lang": final.get("target_lang"),
        "sql": final.get("sql"),
        "sparql": final.get("sparql"),
        "rows": final.get("rows"),
        "validator_error": final.get("validator_error"),
        "execution_error": final.get("execution_error"),
        "repair_count": final.get("repair_count"),
        "policy_block": final.get("policy_block"),
        "confidence": final.get("confidence"),
        "shacl_report": final.get("shacl_report"),
        "summary": final.get("summary"),
        "insights": final.get("insights"),
        "follow_ups": final.get("follow_ups"),
        "trace": final.get("trace", []),
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    init = {"question": req.q, "trace": [], "repair_count": 0, "project": req.project}
    try:
        final = await app_graph.ainvoke(init)
    except Exception as e:  # noqa: BLE001
        final = {"question": req.q,
                 "answer": "Something went wrong handling that one. Please try rephrasing.",
                 "execution_error": str(e)[:200], "trace": []}
    return QueryResponse(**_payload(final))


@app.post("/query/stream")
async def query_stream(req: QueryRequest):
    async def gen():
        init = {"question": req.q, "trace": [], "repair_count": 0, "project": req.project}
        final: dict = {}
        emitted = 0
        try:
            async for state in app_graph.astream(init, stream_mode="values"):
                final = state
                trace = state.get("trace") or []
                while emitted < len(trace):
                    step = trace[emitted]
                    emitted += 1
                    yield _sse("step", {"step": step.get("step")})
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"error": str(e)})
            return
        yield _sse("result", _payload(final))

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
