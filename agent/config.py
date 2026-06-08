"""Active-dataset config — the seam that makes the agent domain-agnostic and
now multi-project (M2).

Each project's artifact lives at ARTIFACTS_DIR/<id>/dataset.json (written by the
onboarding service). The agent reads it as a file, mtime-cached PER PROJECT, so
re-onboarding hot-swaps a dataset with no restart. The active project id arrives
on the request; None resolves to DEFAULT_PROJECT, else the newest 'ready' project
from the meta.project registry (read over the read-only DSN).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import psycopg2

ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", "/artifacts"))
DEFAULT_PROJECT = os.environ.get("DEFAULT_PROJECT", "")
RO_DSN = os.environ.get("POSTGRES_RO_DSN", "")


class Dataset:
    """Typed view over a project's onboarding artifact."""

    def __init__(self, data: dict):
        self._d = data

    @property
    def project_id(self) -> str | None:
        return self._d.get("project_id")

    @property
    def schema(self) -> str:
        return self._d.get("schema", "public")

    @property
    def dataset_name(self) -> str:
        return self._d.get("dataset_name", "dataset")

    @property
    def schema_context(self) -> str:
        return self._d.get("schema_context", "")

    @property
    def known_tables(self) -> set[str]:
        return set(self._d.get("known_tables", []))

    @property
    def known_columns(self) -> dict[str, set[str]]:
        return {t: set(cols) for t, cols in self._d.get("known_columns", {}).items()}

    @property
    def tables(self) -> list[dict]:
        return self._d.get("tables", [])

    @property
    def row_counts(self) -> dict[str, int]:
        return self._d.get("row_counts", {})

    @property
    def metrics(self) -> list[dict]:
        """Config-driven KPI definitions (M4)."""
        return self._d.get("metrics", [])

    # M3 — SPARQL plane
    @property
    def ontology_ns(self) -> str | None:
        return self._d.get("ontology_ns")

    @property
    def ontology_prefix(self) -> str | None:
        return self._d.get("ontology_prefix")

    @property
    def resource_ns(self) -> str | None:
        return self._d.get("resource_ns")

    @property
    def sparql_context(self) -> str:
        return self._d.get("sparql_context", "")

    @property
    def ontology_path(self) -> str | None:
        return str(ARTIFACTS_DIR / self.project_id / "ontology.ttl") if self.project_id else None

    @property
    def shapes_path(self) -> str | None:
        return str(ARTIFACTS_DIR / self.project_id / "shapes.ttl") if self.project_id else None

    def table_names(self) -> list[str]:
        return sorted(self.known_tables)


_cache: dict[str, dict] = {}        # project_id -> {mtime, ds}
_lock = threading.Lock()


def list_projects() -> list[dict]:
    """Ready projects, newest first, read over the RO DSN. [] if unavailable."""
    if not RO_DSN:
        return []
    try:
        with psycopg2.connect(RO_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, domain, schema_name, status, created_at "
                    "FROM meta.project WHERE status='ready' ORDER BY created_at DESC")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception:
        return []


def _resolve(project_id: str | None) -> str | None:
    if project_id:
        return project_id
    if DEFAULT_PROJECT:
        return DEFAULT_PROJECT
    projs = list_projects()
    return projs[0]["id"] if projs else None


def _artifact_path(pid: str) -> Path:
    return ARTIFACTS_DIR / pid / "dataset.json"


def load_dataset(project_id: str | None = None) -> Dataset | None:
    """Return the active project's Dataset, or None if nothing is onboarded.
    Reloads transparently when that project's artifact changes on disk."""
    pid = _resolve(project_id)
    if not pid:
        return None
    path = _artifact_path(pid)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    with _lock:
        ent = _cache.get(pid)
        if ent is None or ent["mtime"] != mtime:
            data = json.loads(path.read_text(encoding="utf-8"))
            ent = {"mtime": mtime, "ds": Dataset(data)}
            _cache[pid] = ent
        return ent["ds"]
