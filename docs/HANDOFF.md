# HANDOFF — ExcelUtilitySPARQL (M1–M3)

**Status (2026-06-08):** **M1–M4 built and verified live, end-to-end, in WSL Docker.**

- **M1** — one Excel → reviewed schema → SQL Q&A.
- **M2** — multi-project (`meta.project` registry + isolated `ds_<id>` + UI picker) · data dictionary
  (`data_dictionary` sheet import + UI editor; synonyms drive NL→SQL) · multi-file merge (N workbooks →
  one dataset, cross-file FK + JOIN).
- **M3** — auto-generated ontology + OBDA mapping + SHACL per dataset; ONE Ontop serves all projects via
  per-project IRI prefixes; agent SPARQL path (plan→generate→validate→execute→SHACL). SPARQL needs a
  manual `docker restart exutil-ontop` after onboarding (the chosen reload mechanism — no docker socket).
- **M4** — config-driven metrics: per-dataset KPI templates (`/metrics` UI + onboarding CRUD, stored in the
  artifact); planner routes a name/synonym match to `execute_metric` — deterministic param-fill (numbers/dates
  auto-extracted, text matched to listed values, else default) → SELECT-only/allowlist validate → RO execute →
  audited SQL. No LLM arithmetic. `agent/metrics.py` is the pure-Python core.

Offline: m1 (13) + m2m3 (11) + m3gen (15) + m4 (11) = **50/50**.
Live verified: per-project SQL + SPARQL, FK→object-property traversal, synonym resolution, cross-file JOIN,
RO sandbox, project switching, metric routing + param extraction + injection containment.
**Remaining: M5 (OpenMetadata catalog optional + generic /evals + EC2 deploy + pgvector PageIndex).**

---
## (M1 record below)

## What's built

The full M1 vertical slice — **one Excel → reviewed schema → SQL Q&A** — across
five services wired in `docker-compose.yml` (postgres, redis, onboarding, agent, ui).

- **onboarding** (`onboarding/`): `profiler.py` (header detection, type/PK/FK
  inference), `generators.py` (DDL + schema-context + allowlists + artifact),
  `loader.py` (typed bulk load), `main.py` (`/profile`, `/generate`).
- **agent** (`agent/`): `config.py` (active-dataset artifact loader),
  `validators.py` (dynamic-allowlist SQL AST validator), `nodes.py`/`graph.py`
  (LangGraph SQL pipeline), `main.py` (`/query`, `/query/stream`, `/dataset`).
- **ui** (`ui/`): login, upload→review→finalise wizard, chat (SSE + history),
  templates.
- **postgres/init**: `app_ro` NOLOGIN group + RO login user (read-only +
  statement timeout).

## Verified offline (no Docker / LLM)

- `python tests/test_offline_m1.py` → **13/13 pass**: profile→artifact round-trip,
  README excluded, leading-zero codes kept as text, synth PK, and the validator
  accepting valid joins (+ auto-LIMIT) while rejecting hallucinated columns,
  unknown tables, DELETE, and multi-statement/DROP.
- All Python sources `py_compile` clean.
- `docker compose config` valid.
- Profiler hand-checked against `POC_Sample_Data 1.xlsx`: header rows correct
  (incl. `Discount Band`=1, `README`=5), types correct, **FK inference matches
  the Yokohama join graph**.

## Verified live (WSL Docker, ports remapped to avoid the running Yokohama stack)

`docker compose -p exutil up -d --build` on Ubuntu 24.04 / Docker 29:
- All 5 services build + go healthy.
- Upload → profile (`dataset_name=poc_sample_data_1`) → generate: **10 tables, 210
  rows** loaded into `ds_main`; README sheet excluded.
- Column types correct in PG: `sap_matnr` **text** (leading zeros preserved:
  `000000010012`), `week_of` date, `source_updated_at` timestamp, `available_to_use` bigint.
- **RO sandbox**: `app_ro_user` SELECT works; `INSERT`/`DROP` rejected
  ("cannot execute … in a read-only transaction").
- **NL→SQL** (Claude `claude-opus-4-7`): "how many products" → `COUNT(*)` = 15;
  "surplus capacity at Salem" → filtered `c.plant='PL-SAL'`, 12 rows; "top 5
  customers by order value" → correct 3-table join, grouped by currency, grounded
  multi-currency caveat in the insight.
- **Safety gate**: injection ("ignore your instructions … drop table") and
  off-domain ("write me a poem") both short-circuit to a refusal (`policy_block`).

Reproduce: `docker compose up -d --build`, set `ANTHROPIC_API_KEY` in `.env`, open
http://localhost:8080 (the verified run used remapped ports — see the test `.env`).

## Deliberate M1 scope cuts (pick up in M2+)

- **Single dataset** — `/generate` drops & recreates one `ds_main` schema and one
  artifact. M2 adds the `ds_<id>` registry + project switcher.
- **FKs are not enforced** as DB constraints (recorded in the schema-context for
  the LLM only) — real uploads have orphan rows / excluded lookup sheets.
- **No data dictionary** yet — business names/synonyms/descriptions are stubbed
  in the profile (`business_name`/`description` = null). M2 adds the editor.
- **SQL only** — no SPARQL (M3), metrics (M4), or catalog/evals (M5).
- **Profiler header detection / type inference is heuristic** — the review step
  is the accuracy backbone; always confirm before generating.

## Known sharp edges

- `NOT NULL` is emitted for columns the profiler saw as fully-populated; a later
  re-upload with nulls in those columns would fail the load (M1 is single-load).
- Coercion is forgiving (unconvertible cell → NULL) **except** PK/NOT-NULL
  violations, which surface as a rolled-back load error by design.
- The agent reads the artifact per request (mtime-cached) — re-onboarding
  hot-swaps the schema with no restart.

## Repo

Target remote (per design): `github.com/mahi80/ExcelUilitySQPARL`. Not pushed yet
— `git init` + initial commit when ready.
