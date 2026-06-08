"""Onboarding service — profile uploads and generate isolated projects (M2).

Pipeline:
  POST /profile          upload .xlsx → reviewable schema proposal (profiler)
  POST /generate         confirmed profile → new/replaced project: DDL into an
                         isolated schema ds_<id>, load rows, grant RO, write the
                         per-project artifact, register in meta.project
  GET/DELETE /projects   registry listing + teardown

Each dataset is an isolated, switchable project (schema ds_<id>). The agent reads
/artifacts/<id>/dataset.json; meta.project is the registry source of truth.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg2
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

import ontop_combine
import registry
from generators import build_artifact, generate_ddl
from loader import load_workbook
from ontology_gen import (generate_mapping_blocks, generate_ontology_ttl,
                          generate_shapes_ttl, generate_sparql_context, project_namespaces)
from profiler import merge_profiles, profile_frames, snake_case

POSTGRES_DSN = os.environ.get("POSTGRES_DSN", "")
RO_GROUP = os.environ.get("POSTGRES_RO_GROUP", "app_ro")
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/artifacts/uploads"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort migration of an M1 single-dataset install into the registry.
    try:
        conn = psycopg2.connect(POSTGRES_DSN)
        registry.adopt_legacy(conn, ARTIFACTS_DIR)
        ontop_combine.rebuild_combined_config(conn)     # seed Ontop config (empty if no projects)
        conn.close()
    except Exception:
        pass
    yield


app = FastAPI(title="ExcelUtilitySPARQL — Onboarding", version="0.2.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.post("/profile")
async def profile(files: list[UploadFile] = File(...)):
    """Profile one or more workbooks merged into a single reviewable dataset."""
    if not files:
        raise HTTPException(400, "Upload at least one .xlsx / .xls workbook.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    pfs: list[dict] = []
    tokens: dict[str, str] = {}
    try:
        for f in files:
            if not (f.filename or "").lower().endswith((".xlsx", ".xls")):
                raise HTTPException(400, f"{f.filename}: upload .xlsx / .xls only.")
            token = registry.new_project_id().replace("p_", "u_")
            dest = UPLOAD_DIR / f"{token}.xlsx"
            dest.write_bytes(await f.read())
            pf = profile_frames(dest)
            pf["source_file"] = f.filename                  # original name, not the temp token
            for t in pf["tables"]:
                t["upload_token"] = token
                t["source_file"] = f.filename
            pfs.append(pf)
            tokens[token] = f.filename
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Could not profile the workbook(s): {e}")

    ds_name = snake_case(Path(files[0].filename or "dataset").stem)
    prof = merge_profiles(pfs, ds_name)
    prof["upload_tokens"] = tokens
    prof["original_filenames"] = list(tokens.values())
    return prof


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    profile: dict
    project_id: str | None = None     # None = new project; set = re-onboard/replace
    name: str | None = None


@app.post("/generate")
def generate(req: GenerateRequest):
    if not POSTGRES_DSN:
        raise HTTPException(500, "POSTGRES_DSN not configured.")

    profile = req.profile
    name = req.name or profile.get("dataset_name") or "dataset"
    domain = profile.get("domain")

    # Resolve the source workbook(s) each included table was uploaded from.
    tokens = {t.get("upload_token") for t in profile.get("tables", [])
              if t.get("include", True) and t.get("upload_token")}
    source_paths: dict[str, str] = {}
    for tok in tokens:
        p = UPLOAD_DIR / f"{tok}.xlsx"
        if not p.exists():
            raise HTTPException(404, "Upload expired or not found — please re-upload.")
        source_paths[tok] = str(p)
    if not source_paths:
        raise HTTPException(400, "Profile references no uploaded source files.")

    conn = psycopg2.connect(POSTGRES_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if req.project_id:
                if not registry.get_project(cur, req.project_id):
                    raise HTTPException(404, f"Project {req.project_id} not found.")
                pid = req.project_id
                cur.execute("UPDATE meta.project SET status='profiling', name=%s, "
                            "domain=%s, updated_at=now() WHERE id=%s", (name, domain, pid))
            else:
                pid = registry.create_project(cur, name, domain)
            schema = registry.schema_for(pid)

            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for stmt in generate_ddl(profile, schema):
                cur.execute(stmt)

        loaded = load_workbook(conn, schema, profile, source_paths)

        with conn.cursor() as cur:
            if RO_GROUP:
                cur.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{RO_GROUP}"')
                cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{RO_GROUP}"')

            artifact = build_artifact(profile, schema)
            artifact["project_id"] = pid
            artifact["generated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            artifact["row_counts"] = loaded

            # M3: per-project ontology / OBDA / SHACL / SPARQL-context
            ont_ns, res_ns, ont_prefix, res_prefix = project_namespaces(pid)
            ontology_ttl = generate_ontology_ttl(profile, ont_ns, ont_prefix, name)
            shapes_ttl = generate_shapes_ttl(profile, ont_ns, ont_prefix)
            artifact.update({
                "ontology_ns": ont_ns, "resource_ns": res_ns,
                "ontology_prefix": ont_prefix, "resource_prefix": res_prefix,
                "sparql_context": generate_sparql_context(
                    profile, ont_ns, res_ns, ont_prefix, res_prefix, name),
                "mapping_blocks": generate_mapping_blocks(profile, schema, ont_prefix, res_prefix),
            })

            proj_dir = ARTIFACTS_DIR / pid
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "ontology.ttl").write_text(ontology_ttl, encoding="utf-8")
            (proj_dir / "shapes.ttl").write_text(shapes_ttl, encoding="utf-8")
            (proj_dir / "dataset.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")

            registry.finalize_project(cur, pid, artifact, ontology_ns=ont_ns,
                                      ontology_prefix=ont_prefix, glossary=profile.get("glossary"))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, f"Generation failed (rolled back): {e}")
    finally:
        conn.close()

    # Rebuild the combined Ontop config (SPARQL needs a manual Ontop restart to apply).
    restart_required = False
    try:
        rc = psycopg2.connect(POSTGRES_DSN)
        ontop_combine.rebuild_combined_config(rc)
        rc.close()
        restart_required = True
    except Exception:
        pass

    return {
        "ok": True,
        "project_id": pid,
        "dataset_name": artifact["dataset_name"],
        "schema": schema,
        "tables": list(loaded.keys()),
        "row_counts": loaded,
        "total_rows": sum(loaded.values()),
        "ontop_restart_required": restart_required,
    }


# ---------------------------------------------------------------------------
# Projects registry
# ---------------------------------------------------------------------------

@app.get("/projects")
def projects():
    conn = psycopg2.connect(POSTGRES_DSN)
    try:
        with conn.cursor() as cur:
            return {"projects": registry.list_projects(cur)}
    finally:
        conn.close()


@app.get("/projects/{project_id}")
def project_detail(project_id: str):
    conn = psycopg2.connect(POSTGRES_DSN)
    try:
        with conn.cursor() as cur:
            proj = registry.get_project(cur, project_id)
            if not proj:
                raise HTTPException(404, "Project not found.")
            return proj
    finally:
        conn.close()


@app.delete("/projects/{project_id}")
def project_delete(project_id: str):
    conn = psycopg2.connect(POSTGRES_DSN)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            proj = registry.get_project(cur, project_id)
            if not proj:
                raise HTTPException(404, "Project not found.")
            cur.execute(f'DROP SCHEMA IF EXISTS "{proj["schema_name"]}" CASCADE')
            registry.delete_project(cur, project_id)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, f"Delete failed: {e}")
    finally:
        conn.close()
    # remove the per-project artifact dir (best-effort)
    import shutil
    shutil.rmtree(ARTIFACTS_DIR / project_id, ignore_errors=True)
    try:
        rc = psycopg2.connect(POSTGRES_DSN)
        ontop_combine.rebuild_combined_config(rc)
        rc.close()
    except Exception:
        pass
    return {"ok": True, "deleted": project_id}
