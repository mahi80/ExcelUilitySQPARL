"""Generic eval harness (M5) — works on ANY onboarded dataset.

Auto-generates a small eval set from the dataset's schema (no golden answers
needed) and uses an LLM judge to score each answer for correctness + grounding.
Generalises the Yokohama golden-eval into a domain-agnostic quality check.
"""
from __future__ import annotations

import json
import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")


def build_eval_set(ds) -> list[str]:
    """Schema-derived questions answerable from any dataset, + a metric if defined."""
    tables = ds.table_names()
    qs = [f"How many rows are in {t}?" for t in tables[:4]]
    qs += [f"Show 5 rows from {t}" for t in tables[:2]]
    for m in (ds.metrics or [])[:1]:
        qs.append(f"compute {m['name']}")
    return qs


_JUDGE_TOOL = {
    "name": "judge",
    "description": "Score a data chatbot's answer for correctness, grounding, and relevance.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 1, "maximum": 5,
                      "description": "5 = correct, grounded, relevant; 1 = wrong or ungrounded."},
            "grounded": {"type": "boolean", "description": "Every claim in the answer is supported by the result rows."},
            "relevant": {"type": "boolean", "description": "The answer addresses the question."},
            "reason": {"type": "string", "description": "One-sentence justification."},
        },
        "required": ["score", "grounded", "relevant", "reason"],
    },
}

_JUDGE_SYSTEM = """\
You evaluate a data chatbot. Given the user QUESTION, the GENERATED QUERY, the
RESULT ROWS, and the ANSWER, decide how well the answer responds. Score 1-5
(5 = correct + fully grounded + relevant). Set grounded=false if the answer
states any number or entity not supported by the rows. Set relevant=false if it
doesn't address the question. A correct "no rows / not in this dataset"
explanation for an empty result is fine (high score). Be strict about grounding.
"""


def judge(question: str, answer: str, query: str | None, rows: list[dict]) -> dict:
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("sk-ant-REPLACE"):
        return {"score": None, "error": "ANTHROPIC_API_KEY not configured"}
    payload = (
        f"QUESTION: {question}\n\n"
        f"GENERATED QUERY:\n{query or '(none)'}\n\n"
        f"RESULT ROWS (up to 10):\n{json.dumps(rows[:10], default=str, indent=2)}\n\n"
        f"ANSWER:\n{answer}"
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=400, system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
            tools=[_JUDGE_TOOL], tool_choice={"type": "tool", "name": "judge"})
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use" and b.name == "judge":
                return dict(b.input)
        return {"score": None, "error": "no verdict"}
    except Exception as e:  # noqa: BLE001
        return {"score": None, "error": str(e)[:200]}


def summarize(dataset_name: str, project_id: str | None, results: list[dict]) -> dict:
    scored = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    return {
        "dataset": dataset_name,
        "project": project_id,
        "n": len(results),
        "avg_score": round(sum(scored) / len(scored), 2) if scored else None,
        "pass_rate": round(sum(1 for s in scored if s >= 4) / len(scored), 2) if scored else None,
        "results": results,
    }
