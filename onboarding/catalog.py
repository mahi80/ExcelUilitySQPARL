"""OpenMetadata catalog push (Phase-2) — register a dataset's schema in an
OpenMetadata instance so it shows up in the catalog (service → database →
schema → tables → columns).

Opt-in: does nothing unless OPENMETADATA_HOST is set. Best-effort — failures are
returned, never raised into the onboarding flow. Uses stdlib urllib (no new dep).
OpenMetadata runs as its own stack (see catalog/openmetadata.yml); the onboarding
container reaches it via OPENMETADATA_HOST (e.g. http://host.docker.internal:8585).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

OM_HOST = os.environ.get("OPENMETADATA_HOST", "")
OM_TOKEN = os.environ.get("OPENMETADATA_JWT_TOKEN", "")
OM_ADMIN_EMAIL = os.environ.get("OPENMETADATA_ADMIN_EMAIL", "admin@open-metadata.org")
OM_ADMIN_PASSWORD = os.environ.get("OPENMETADATA_ADMIN_PASSWORD", "admin")
OM_SERVICE = os.environ.get("OPENMETADATA_SERVICE", "excelutil")

# our logical type → OpenMetadata column dataType
_TYPE_MAP = {
    "integer": "BIGINT", "decimal": "DECIMAL", "boolean": "BOOLEAN",
    "date": "DATE", "datetime": "DATETIME", "text": "VARCHAR",
}


def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(OM_HOST.rstrip("/") + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _token() -> str | None:
    if OM_TOKEN:
        return OM_TOKEN
    try:
        pw = base64.b64encode(OM_ADMIN_PASSWORD.encode()).decode()
        r = _req("POST", "/api/v1/users/login", body={"email": OM_ADMIN_EMAIL, "password": pw})
        return r.get("accessToken")
    except Exception:
        return None


def _column(c: dict) -> dict:
    dt = _TYPE_MAP.get(c.get("type", "text"), "VARCHAR")
    col = {"name": c["name"], "dataType": dt, "dataTypeDisplay": c.get("type", "text")}
    if dt == "VARCHAR":
        col["dataLength"] = 1024
    desc_bits = [c.get("business_name"), c.get("description")]
    if c.get("synonyms"):
        desc_bits.append("synonyms: " + ", ".join(c["synonyms"]))
    desc = " — ".join(b for b in desc_bits if b)
    if desc:
        col["description"] = desc
    return col


def push_dataset(dataset_name: str, schema: str, tables: list[dict],
                 domain: str | None = None) -> dict:
    """Create/update the service, database, schema, and tables in OpenMetadata."""
    if not OM_HOST:
        return {"ok": False, "skipped": "OPENMETADATA_HOST not set"}
    token = _token()
    if not token:
        return {"ok": False, "error": "OpenMetadata auth failed"}
    try:
        _req("PUT", "/api/v1/services/databaseServices", token, {
            "name": OM_SERVICE, "serviceType": "CustomDatabase",
            "description": "Datasets onboarded via ExcelUtilitySPARQL",
            "connection": {"config": {"type": "CustomDatabase",
                                      "sourcePythonClass": "metadata.ingestion.source.database.customdatabase"}}})
        _req("PUT", "/api/v1/databases", token,
             {"name": dataset_name, "service": OM_SERVICE,
              **({"description": domain} if domain else {})})
        _req("PUT", "/api/v1/databaseSchemas", token,
             {"name": schema, "database": f"{OM_SERVICE}.{dataset_name}"})
        pushed = 0
        for t in tables:
            body = {"name": t["name"],
                    "databaseSchema": f"{OM_SERVICE}.{dataset_name}.{schema}",
                    "columns": [_column(c) for c in t.get("columns", [])]}
            if t.get("description"):
                body["description"] = t["description"]
            _req("PUT", "/api/v1/tables", token, body)
            pushed += 1
        return {"ok": True, "service": OM_SERVICE, "database": dataset_name,
                "schema": schema, "tables": pushed}
    except urllib.error.HTTPError as e:  # noqa: F821
        return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
