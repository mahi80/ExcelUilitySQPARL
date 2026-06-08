"""Project registry — CRUD over meta.project (M2).

Each onboarded dataset is a row here + an isolated schema ds_<id> + a per-project
artifact file (/artifacts/<id>/dataset.json). This module owns the lifecycle;
onboarding/main.py orchestrates DDL/load/grant around it within one transaction.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any


def new_project_id() -> str:
    return "p_" + secrets.token_hex(4)


def schema_for(project_id: str) -> str:
    return f"ds_{project_id}"


def create_project(cur, name: str, domain: str | None = None,
                   project_id: str | None = None) -> str:
    """Insert a 'profiling' row; returns the project id. Within the caller's txn."""
    pid = project_id or new_project_id()
    cur.execute(
        "INSERT INTO meta.project (id, name, domain, schema_name, status) "
        "VALUES (%s, %s, %s, %s, 'profiling')",
        (pid, name, domain, schema_for(pid)),
    )
    return pid


def finalize_project(cur, project_id: str, artifact: dict,
                     ontology_ns: str | None = None,
                     ontology_prefix: str | None = None,
                     glossary: list | None = None) -> None:
    cur.execute(
        "UPDATE meta.project SET status='ready', artifact=%s, ontology_ns=%s, "
        "ontology_prefix=%s, glossary=COALESCE(%s, glossary), updated_at=now() "
        "WHERE id=%s",
        (json.dumps(artifact), ontology_ns, ontology_prefix,
         json.dumps(glossary) if glossary is not None else None, project_id),
    )


def get_project(cur, project_id: str) -> dict | None:
    cur.execute(
        "SELECT id, name, domain, schema_name, status, ontology_ns, ontology_prefix, "
        "created_at FROM meta.project WHERE id=%s",
        (project_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def list_projects(cur) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, name, domain, schema_name, status, created_at "
        "FROM meta.project ORDER BY created_at DESC"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def delete_project(cur, project_id: str) -> None:
    cur.execute("DELETE FROM meta.project WHERE id=%s", (project_id,))


def adopt_legacy(conn, artifacts_dir: Path) -> str | None:
    """Migration: if an M1 single-dataset artifact exists and the registry is
    empty, ingest it as project 'main' (schema ds_main) so the live dataset keeps
    working after the upgrade. Idempotent; returns the adopted id or None."""
    legacy = artifacts_dir / "dataset.json"
    if not legacy.exists():
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM meta.project")
        if cur.fetchone()[0] > 0:
            return None
        try:
            artifact = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception:
            return None
        pid = "main"
        cur.execute(
            "INSERT INTO meta.project (id, name, domain, schema_name, status, artifact) "
            "VALUES (%s, %s, %s, %s, 'ready', %s) ON CONFLICT (id) DO NOTHING",
            (pid, artifact.get("dataset_name", "main"), artifact.get("domain"),
             artifact.get("schema", "ds_main"), json.dumps(artifact)),
        )
        dst = artifacts_dir / pid
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "dataset.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    conn.commit()
    return pid
