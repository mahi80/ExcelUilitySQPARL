# ExcelUtilitySPARQL (M1–M3)

Upload Excel workbook(s), review the inferred schema, and chat with your data in
natural language — **SQL and SPARQL**. A **domain-agnostic** generalisation of the
Yokohama Sales-Bot: the same NL → query + validation + grounded-insights trust
stack, but the schema, LLM context, validator allowlists, ontology, OBDA mapping,
and SHACL shapes are all **generated from your upload** instead of hand-written.

> **Built & live-verified: M1–M3.** Multi-project isolation, a data-dictionary
> editor, multi-file merge into one dataset, and an auto-generated Ontop SPARQL
> plane are all working. Config-driven metrics (M4) and OpenMetadata/evals/EC2
> (M5) remain — see [Roadmap](#roadmap).

---

## How it works

Two planes, joined by a generated **artifact** (`/artifacts/dataset.json`):

```
            UPLOAD                                    CHAT
  ┌───────────────────────────┐         ┌──────────────────────────────┐
  │  onboarding (FastAPI)      │  writes │  agent (FastAPI + LangGraph)  │
  │  profile → review → DDL    │ ──────► │  NL → SQL → validate → RO     │
  │  + load + schema-context   │ artifact│  execute → grounded insights  │
  │  + allowlists              │  reads  │  (reads the active artifact)  │
  └───────────────────────────┘ ◄────── └──────────────────────────────┘
        ▲  Postgres (owner)                      │  Postgres (read-only role)
        │                                        ▼
        └──────────────  ui (HTMX wizard + chat) ┘
```

- **onboarding** — profiles the workbook (one table per sheet; infers
  snake_case columns, types, nullability, PK, and FK candidates), then on
  confirmation generates `CREATE TABLE` DDL, loads the rows, writes the LLM
  **schema-context** + validator **allowlists**, and grants the read-only role
  SELECT on the new schema.
- **agent** — the Yokohama control-plane, SQL path: policy/safety gate →
  generate SQL → AST validate against the **dataset's** allowlists → confidence
  score + repair loop → execute in a **read-only sandbox** scoped to the
  dataset schema → grounded summary/insights/follow-ups.
- **ui** — login, the upload→review→finalise wizard, and the chat (SSE
  streaming + per-user history).

---

## Quickstart

**Prerequisites:** Docker Desktop running, and an Anthropic API key.

```bash
cd ExcelUtilitySPARQL
cp .env.example .env          # then edit .env:
#   - set ANTHROPIC_API_KEY=sk-ant-...
#   - change the POSTGRES_* / UI_AUTH_* / UI_SESSION_SECRET values for anything non-local

docker compose up -d --build
```

Open **http://localhost:8080**, sign in with `UI_AUTH_USER` / `UI_AUTH_PASSWORD`,
then:

1. **Upload** an `.xlsx` (try `../POC_Sample_Data 1.xlsx`).
2. **Review** the inferred schema — fix any column types/keys, untick non-data
   sheets (e.g. a README), then **Create dataset**.
3. **Chat** — e.g. *"Where do we have surplus capacity at Salem?"*,
   *"Top 5 customers by order value"*, *"List the iceGUARD products"*.

---

## Verifying M1

| Check | How |
|---|---|
| Tables created with correct types | After onboarding, the *Dataset ready* page lists per-table row counts; `docker exec -it exutil-postgres psql -U excelutil -c '\d ds_main.*'`. |
| Correct **grounded** answer | Ask a question with a known answer; the reply cites only values from the result rows. |
| Validator rejects a hallucinated column | Offline: `python tests/test_offline_m1.py` (asserts it). |
| RO sandbox blocks writes | The agent connects as `app_ro_user` (`default_transaction_read_only=on`); a generated `DELETE`/`DROP` is also blocked by the validator first. |

**Offline tests (no Docker / key needed):**

```bash
python tests/test_offline_m1.py        # profile → artifact → validator round-trip (13 checks)
```

---

## Project layout

```
ExcelUtilitySPARQL/
├── docker-compose.yml        # postgres, redis, onboarding, agent, ui
├── .env.example
├── onboarding/               # NEW plane — generate what Yokohama hard-codes
│   ├── profiler.py           #   xlsx → reviewable schema proposal
│   ├── generators.py         #   profile → DDL + schema-context + allowlists + artifact
│   ├── loader.py             #   coerce + bulk-load rows
│   └── main.py               #   FastAPI: /profile, /generate
├── agent/                    # generalised Yokohama agent (SQL path, M1)
│   ├── config.py             #   loads the active dataset artifact (mtime-cached)
│   ├── validators.py         #   SQL AST validator w/ dynamic allowlists
│   ├── nodes.py / graph.py   #   LangGraph control plane
│   └── main.py               #   FastAPI: /query, /query/stream, /dataset
├── ui/                       # HTMX login + upload wizard + chat
├── postgres/init/            # app_ro group role + RO login user
└── tests/test_offline_m1.py  # offline verification
```

## How it generalises Yokohama

| Yokohama (hard-coded) | ExcelUtilitySPARQL (generated at onboarding) |
|---|---|
| `postgres/init/01_schema.sql` | `generators.generate_ddl()` from the confirmed profile |
| `agent/schema_context.py` | `generators.generate_schema_context()` |
| `validators.KNOWN_TABLES` / `KNOWN_COLUMNS` | `generators.generate_allowlists()` → artifact → `config.Dataset` |
| `ingest/ingest.py` (per-entity) | `loader.load_workbook()` (profile-driven loop) |
| campaign/quote `child_scripts.py` | *(M4 — config-driven metrics)* |
| ontology / OBDA | *(M3 — Ontop bootstrap)* |

The agent control-plane, validators, read-only sandbox, HTMX UI, SSE streaming,
and trace **carry over** — just parameterised by the active dataset.

## Roadmap

- **M1 ✅** — one Excel → reviewed schema → SQL Q&A.
- **M2 ✅** — multi-project (`ds_<id>` isolation + `meta.project` registry + switcher),
  multi-file merge into one dataset, data-dictionary editor (domain/synonyms/units).
- **M3 ✅** — SPARQL via Ontop with **auto-generated** ontology/OBDA/SHACL per dataset
  (one Ontop serves all projects; `docker restart exutil-ontop` applies a new dataset).
- **M4 ✅** — config-driven metrics: per-dataset KPI templates (`/metrics` editor) computed
  **deterministically** (no LLM arithmetic); the planner routes a matching question to the
  metric executor (param-fill → validate → RO execute → audited SQL).
- **M5 ◐** — generic **`/evals`** quality dashboard (auto-generated questions + LLM judge, any
  dataset) ✅ and an EC2 **[DEPLOY.md](docs/DEPLOY.md)** ✅. **OpenMetadata** and **pgvector
  PageIndex** are deferred to Phase-2 (the registry + generated schema-context cover catalog/
  search at current scale — see DEPLOY.md "Not deployed").

### SPARQL note
SPARQL runs over a single Ontop container serving every project via per-project IRI
namespaces. Switching projects needs no restart; **onboarding a new dataset** rebuilds
the combined Ontop config and prints a reminder to run `docker restart exutil-ontop`
(SQL chat works immediately regardless).

## Security notes (M1)

- Generated queries run as a **read-only** role with `default_transaction_read_only=on`
  and a `statement_timeout`; the validator independently rejects non-SELECT,
  multi-statement, and off-allowlist queries.
- Basic single-credential login (POC). `.env` is gitignored — rotate any secret
  that leaks. SSO / per-user accounts are a later milestone.
