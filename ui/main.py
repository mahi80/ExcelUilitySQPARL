"""HTMX UI for ExcelUtilitySPARQL — login, the upload→review→finalise
onboarding wizard, and the chat (proxying the agent)."""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time

import httpx
import markdown
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

AGENT_URL = os.environ.get("AGENT_URL", "http://agent:8000")
ONBOARDING_URL = os.environ.get("ONBOARDING_URL", "http://onboarding:8001")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
AUTH_USER = os.environ.get("UI_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("UI_AUTH_PASSWORD", "change_me")
SESSION_SECRET = os.environ.get("UI_SESSION_SECRET") or secrets.token_hex(32)
HISTORY_CAP = 50

LOGICAL_TYPES = ["integer", "decimal", "boolean", "date", "datetime", "text"]


# --- chip + trace helpers (from the agent's compose() contract) ---------------
_CHIP_RE = re.compile(r"<<<FOLLOWUP_CHIPS>>>(.*?)<<<END>>>", re.DOTALL)
_TRYNEXT_RE = re.compile(r"(?im)^[ \t]*#{1,6}[ \t]*try[ \t]*next[ \t]*$\n?")

_STEP_ICON = {
    "retrieve": "🔍", "policy_check": "🛡️", "plan": "🧭", "llm_call": "🤖", "generate_sql": "📝",
    "generate_sparql": "📝", "validate_sql": "✅", "validate_sparql": "✅", "score_query": "📊",
    "execute_sql": "🗄️", "execute_sparql": "🕸️", "shacl_post_exec": "🔎",
    "compose_insights": "💡", "compose_insights_llm": "🤖", "compose": "📋", "repair": "🔧",
}
_STEP_LABEL = {
    "retrieve": "Retrieve context", "policy_check": "Policy & safety", "plan": "Plan path",
    "llm_call": "Claude call", "generate_sql": "Generate SQL", "generate_sparql": "Generate SPARQL",
    "validate_sql": "Validate SQL", "validate_sparql": "Validate SPARQL", "score_query": "Confidence score",
    "execute_sql": "Execute SQL", "execute_sparql": "Execute SPARQL", "shacl_post_exec": "SHACL validation",
    "compose_insights": "Compose insights", "compose_insights_llm": "Insights (Claude)",
    "compose": "Compose answer", "repair": "Repair loop",
}
_TRACE_SKIP = {"step", "ts", "via"}


def _extract_chips(answer: str) -> tuple[str, list[str]]:
    chips: list[str] = []

    def _pluck(m: re.Match) -> str:
        chips.extend([p.strip() for p in m.group(1).split("|") if p.strip()])
        return ""
    return _CHIP_RE.sub(_pluck, answer), chips


def _fmt_pill(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v) if v else "—"
    s = str(v)
    return s if len(s) <= 90 else s[:87] + "…"


def _build_trace_view(trace: list[dict]) -> list[dict]:
    view: list[dict] = []
    for step in trace:
        name = step.get("step", "?")
        if name == "llm_call" and step.get("via"):
            continue
        pills = [{"k": k.replace("_", " "), "v": _fmt_pill(val)}
                 for k, val in step.items() if k not in _TRACE_SKIP and val is not None]
        view.append({"icon": _STEP_ICON.get(name, "•"),
                     "label": _STEP_LABEL.get(name, name.replace("_", " ").title()),
                     "is_llm": "model" in step, "pills": pills})
    return view


# --- redis (profile staging + chat history) -----------------------------------
_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        import redis as _redislib
        _redis = _redislib.from_url(REDIS_URL, decode_responses=True,
                                    socket_connect_timeout=2, socket_timeout=2)
    return _redis


def _stage_profile(user: str, profile: dict) -> None:
    _get_redis().set(f"exutil:profile:{user}", json.dumps(profile), ex=3600)


def _load_staged_profile(user: str) -> dict | None:
    raw = _get_redis().get(f"exutil:profile:{user}")
    return json.loads(raw) if raw else None


def _persist_history(user: str | None, q: str, html: str) -> None:
    if not user:
        return
    try:
        r = _get_redis()
        key = f"exutil:hist:{user}"
        r.lpush(key, json.dumps({"q": q, "html": html, "ts": time.time()}))
        r.ltrim(key, 0, HISTORY_CAP - 1)
    except Exception:
        pass


def _load_history(user: str | None, limit: int = 20) -> list[dict]:
    if not user:
        return []
    try:
        items = _get_redis().lrange(f"exutil:hist:{user}", 0, limit - 1)
    except Exception:
        return []
    out = []
    for it in items:
        try:
            out.append(json.loads(it))
        except Exception:
            pass
    return list(reversed(out))


# --- message render ------------------------------------------------------------
def _message_context(q: str, data: dict, elapsed_ms: int) -> dict:
    cleaned, chips = _extract_chips(data.get("answer") or "")
    cleaned = _TRYNEXT_RE.sub("", cleaned)
    answer_html = markdown.markdown(cleaned, extensions=["fenced_code", "tables"])
    if data.get("policy_block"):
        path_label, path_class = "POLICY BLOCK", "badge-policy"
    elif (data.get("target_lang") or "") == "sparql":
        path_label, path_class = "SPARQL via Ontop", "badge-sparql"
    elif (data.get("target_lang") or "") == "sql":
        path_label, path_class = "SQL", "badge-sql"
    else:
        path_label, path_class = "(unknown)", "badge-unknown"
    return {
        "q": q, "answer_html": answer_html, "sql": data.get("sql"), "rows": data.get("rows"),
        "trace_view": _build_trace_view(data.get("trace") or []),
        "trace_json": json.dumps(data.get("trace") or [], indent=2, default=str),
        "elapsed_ms": elapsed_ms, "path_label": path_label, "path_class": path_class,
        "confidence": data.get("confidence"), "repair_count": data.get("repair_count") or 0,
        "follow_up_chips": chips,
    }


def _render_message_html(q: str, data: dict, elapsed_ms: int) -> str:
    return templates.env.get_template("message.html").render(_message_context(q, data, elapsed_ms))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _active_dataset(project: str | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            params = {"project": project} if project else {}
            r = await client.get(f"{AGENT_URL}/dataset", params=params)
            return r.json()
    except Exception:
        return {"onboarded": False}


async def _list_projects() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{AGENT_URL}/projects")
            return r.json().get("projects", [])
    except Exception:
        return []


# --- app ----------------------------------------------------------------------
app = FastAPI(title="ExcelUtilitySPARQL UI", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="exutil_session",
                   https_only=False, max_age=8 * 60 * 60)
templates = Jinja2Templates(directory="templates")


def _is_authed(request: Request) -> bool:
    return bool(request.session.get("user"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if _is_authed(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = (hmac.compare_digest(username.strip().lower(), AUTH_USER.strip().lower())
          and hmac.compare_digest(password, AUTH_PASSWORD))
    if not ok:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid username or password."},
            status_code=401)
    request.session["user"] = AUTH_USER
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    projects = await _list_projects()
    if not projects:
        return RedirectResponse("/upload", status_code=303)
    active = request.session.get("project")
    if active not in {p["id"] for p in projects}:
        active = projects[0]["id"]
        request.session["project"] = active
    ds = await _active_dataset(active)
    if not ds.get("onboarded"):
        return RedirectResponse("/upload", status_code=303)
    # history is keyed per (user, project) so switching projects shows its own thread
    hist_key = f"{request.session.get('user')}:{active}"
    return templates.TemplateResponse("index.html", {
        "request": request, "user": request.session.get("user"), "dataset": ds,
        "projects": projects, "active_project": active,
        "history_messages": _load_history(hist_key)})


# --- onboarding wizard --------------------------------------------------------
@app.get("/upload", response_class=HTMLResponse)
async def upload_form(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    projects = await _list_projects()
    return templates.TemplateResponse("upload.html", {
        "request": request, "user": request.session.get("user"),
        "n_projects": len(projects), "error": None})


@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(request: Request, files: list[UploadFile] = File(...)):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    try:
        _ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        multipart = [("files", (f.filename, await f.read(), _ct)) for f in files]
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{ONBOARDING_URL}/profile", files=multipart)
            r.raise_for_status()
            profile = r.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", str(e)) if e.response else str(e)
        return templates.TemplateResponse("upload.html", {
            "request": request, "user": request.session.get("user"),
            "n_projects": 0, "error": detail}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("upload.html", {
            "request": request, "user": request.session.get("user"),
            "n_projects": 0, "error": f"Upload failed: {e}"}, status_code=500)

    _stage_profile(request.session["user"], profile)
    return RedirectResponse("/review", status_code=303)


@app.get("/review", response_class=HTMLResponse)
async def review(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    profile = _load_staged_profile(request.session["user"])
    if not profile:
        return RedirectResponse("/upload", status_code=303)
    return templates.TemplateResponse("review.html", {
        "request": request, "user": request.session.get("user"),
        "profile": profile, "logical_types": LOGICAL_TYPES})


@app.post("/finalize", response_class=HTMLResponse)
async def finalize(request: Request):
    if not _is_authed(request):
        return RedirectResponse("/login", status_code=303)
    profile = _load_staged_profile(request.session["user"])
    if not profile:
        return RedirectResponse("/upload", status_code=303)

    form = await request.form()

    # Dataset-level: domain + glossary (3 fixed rows in the form)
    profile["domain"] = (form.get("ds_domain") or "").strip() or None
    glossary = []
    gi = 0
    while form.get(f"glossary_{gi}_term") is not None:
        term = (form.get(f"glossary_{gi}_term") or "").strip()
        if term:
            glossary.append({"term": term, "definition": (form.get(f"glossary_{gi}_def") or "").strip()})
        gi += 1
    profile["glossary"] = glossary

    def _syn(v: str | None) -> list[str]:
        return [s.strip() for s in re.split(r"[,;]", v or "") if s.strip()]

    # Per-table / per-column edits (keyed by index)
    for ti, table in enumerate(profile.get("tables", [])):
        table["include"] = form.get(f"tbl_{ti}_include") == "on"
        table["description"] = (form.get(f"tbl_{ti}_desc") or "").strip() or None
        pk_choice = form.get(f"tbl_{ti}_pk", "")
        if pk_choice == "__synth__":
            table["pk"], table["synth_pk"] = None, True
        elif pk_choice:
            table["pk"], table["synth_pk"] = pk_choice, False
        for ci, col in enumerate(table.get("columns", [])):
            col["include"] = form.get(f"col_{ti}_{ci}_include") == "on"
            new_type = form.get(f"col_{ti}_{ci}_type")
            if new_type in LOGICAL_TYPES:
                col["type"] = new_type
            col["business_name"] = (form.get(f"col_{ti}_{ci}_bn") or "").strip() or None
            col["description"] = (form.get(f"col_{ti}_{ci}_desc") or "").strip() or None
            col["unit"] = (form.get(f"col_{ti}_{ci}_unit") or "").strip() or None
            col["synonyms"] = _syn(form.get(f"col_{ti}_{ci}_syn"))
        for fi, fk in enumerate(table.get("fks", [])):
            fk["include"] = form.get(f"fk_{ti}_{fi}_include") == "on"

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{ONBOARDING_URL}/generate", json={"profile": profile})
            r.raise_for_status()
            result = r.json()
    except httpx.HTTPStatusError as e:
        detail = e.response.json().get("detail", str(e)) if e.response else str(e)
        return templates.TemplateResponse("review.html", {
            "request": request, "user": request.session.get("user"),
            "profile": profile, "logical_types": LOGICAL_TYPES,
            "error": detail}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return templates.TemplateResponse("review.html", {
            "request": request, "user": request.session.get("user"),
            "profile": profile, "logical_types": LOGICAL_TYPES,
            "error": f"Generation failed: {e}"}, status_code=500)

    _get_redis().delete(f"exutil:profile:{request.session['user']}")
    request.session["project"] = result.get("project_id")   # make the new dataset active
    return templates.TemplateResponse("done.html", {
        "request": request, "user": request.session.get("user"), "result": result})


@app.post("/projects/{project_id}/activate")
async def activate_project(request: Request, project_id: str):
    if not _is_authed(request):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    request.session["project"] = project_id
    return JSONResponse({"ok": True, "project": project_id})


# --- chat ---------------------------------------------------------------------
@app.post("/ask", response_class=HTMLResponse)
async def ask(request: Request, q: str = Form(...)):
    if not _is_authed(request):
        return HTMLResponse('<div class="message"><div class="answer">Session expired — '
                            '<a href="/login">sign in</a>.</div></div>', status_code=401)
    user = request.session.get("user")
    project = request.session.get("project")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{AGENT_URL}/query",
                                     json={"q": q, "user": user, "project": project})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        data = {"answer": f"**Agent service unreachable:** `{e}`", "trace": []}
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    html = _render_message_html(q, data, elapsed_ms)
    _persist_history(f"{user}:{project}", q, html)
    return HTMLResponse(html)


@app.get("/ask/stream")
async def ask_stream(request: Request, q: str):
    if not _is_authed(request):
        return StreamingResponse(iter([_sse("error", {"message": "Session expired."})]),
                                 media_type="text/event-stream")
    user = request.session.get("user")
    project = request.session.get("project")

    async def gen():
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", f"{AGENT_URL}/query/stream",
                                         json={"q": q, "user": user, "project": project}) as resp:
                    event = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            payload = line.split(":", 1)[1].strip()
                            if event == "step":
                                step = json.loads(payload).get("step")
                                yield _sse("step", {"step": step,
                                                    "label": _STEP_LABEL.get(step, (step or "…").replace("_", " ").title()),
                                                    "icon": _STEP_ICON.get(step, "•")})
                            elif event == "result":
                                data = json.loads(payload)
                                elapsed = int((time.perf_counter() - started) * 1000)
                                html = _render_message_html(q, data, elapsed)
                                _persist_history(f"{user}:{project}", q, html)
                                yield _sse("done", {"html": html})
                            elif event == "error":
                                yield _sse("error", json.loads(payload))
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
