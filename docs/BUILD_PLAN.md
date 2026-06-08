# BUILD PLAN — how to build ExcelUtilitySPARQL (with pros & cons)

A step-by-step plan for building a **domain-agnostic "upload Excel → chat with your
data"** utility (SQL + SPARQL + deterministic metrics + catalog), and the reasoning /
trade-offs behind each decision. This is how the working system in this repo was built.

---

## 0. The core idea

A normal NL-to-SQL bot is **hard-coded** to one schema: the DDL, the LLM prompt that
describes the tables, the validator's table/column allowlists, the ontology/OBDA for
SPARQL, and any business logic are all written by hand for that one dataset.

**The shift:** generate all of those **from the uploaded spreadsheet at onboarding
time**, and make the agent read them per-request. The query engine (plan → generate →
validate → execute → ground) stays the same; only its *configuration* becomes data.

- **Pros:** one codebase serves any dataset; no per-customer engineering; the trust
  stack (validation, read-only sandbox, grounding) is reused everywhere.
- **Cons:** generation is heuristic, so a **human review step is mandatory** for
  accuracy; generic prompts are slightly less tuned than hand-written ones.

---

## 1. Pick the stack

| Choice | Why | Pros | Cons |
|---|---|---|---|
| **PostgreSQL** as the data store | mature, typed, schema-per-dataset isolation, read-only roles | strong isolation + security primitives; SQL is the workhorse | one engine for all tenants (noisy-neighbour risk at scale) |
| **LangGraph** agent control-plane | explicit state machine (plan/validate/repair/execute) | auditable, testable nodes; easy to branch SQL/SPARQL/metric | extra dependency vs a bare loop |
| **Claude** (Anthropic) for NL→query + judging | strong codegen + tool-use; grounded summaries | high quality; tool-use gives structured output | needs an API key; cost per query |
| **Ontop** for SPARQL (OBDA over Postgres) | maps relational → RDF without copying data | real SPARQL with no ETL | JVM-heavy; reloads mapping only at boot |
| **pgvector + fastembed** for retrieval | semantic schema-slice for big schemas, local embeddings | no external embedding API; ONNX (no torch) | model download on first use; only helps large schemas |
| **HTMX + FastAPI + Jinja** UI | server-rendered, minimal JS | simple, fast to build | not a rich SPA |
| **OpenMetadata** for the catalog | the standard open catalog | lineage/search/glossary out of the box | heavyweight (ES + its own DB); run as a separate stack |
| **Docker Compose** | one-command local + EC2 | reproducible; profiles for opt-in services | single-host (not k8s-scale) |

**Alternative considered:** a lighter lexical retriever instead of pgvector (no model)
— rejected because the goal is *semantic* matching; kept full-context for small schemas
so the heavy path only runs when it pays off.

---

## 2. Build in shippable milestones

Each milestone is independently runnable and verifiable. Build + verify one at a time.

### M1 — one Excel → reviewed schema → SQL Q&A  *(the proof)*
**Steps:** profiler (sheet→table, infer types/PK/FK, detect header row) → **review UI**
→ generators (DDL + LLM schema-context + validator allowlists) → loader (typed bulk
insert) → generalised agent (plan→generate_sql→validate→score→repair→execute→ground)
reading a generated **artifact** instead of constants → read-only Postgres sandbox.
- **Pros:** proves the whole generalisation end-to-end with the smallest surface.
- **Cons:** single dataset only; SQL only; FKs recorded but not enforced.
- **Verify:** upload sample → correct types in PG; a grounded answer; validator rejects
  a hallucinated column; the RO role can't write.

### M2 — multi-project · data dictionary · multi-file
**Steps:** (a) **registry** (`meta.project`) + isolated `ds_<id>` schema per dataset +
UI project picker; the agent seam becomes `load_dataset(project_id)`. (b) **data
dictionary** — a `data_dictionary` tab / UI editor adds business names, synonyms, units,
domain, glossary → flow into the LLM context. (c) **multi-file merge** — several
workbooks → one dataset, file-prefix on name collisions, FK detection across the union.
- **Pros:** uploads stop overwriting each other; synonyms make NL→SQL understand the
  user's vocabulary; cross-file joins work.
- **Cons:** more moving parts (registry, per-project artifacts, session-scoped active project).
- **Verify:** two isolated datasets answer independently; a synonym maps to an opaque
  column; a cross-file JOIN returns correct rows.

### M3 — SPARQL via auto-generated ontology
**Steps:** generators emit per-dataset `ontology.ttl` (table→Class, column→DatatypeProperty,
FK→ObjectProperty), `mapping.obda` (subject IRI from PK, typed literals, FK object
triples, schema-qualified source), SHACL `shapes.ttl`, and a generic SPARQL context.
**One Ontop** serves all projects via **per-project IRI namespaces** (no collisions);
the agent gains a SPARQL branch (validate against the project's ontology + SHACL).
- **Pros:** real SPARQL + graph view with zero hand-authoring; one Ontop for all datasets;
  switching projects needs no restart.
- **Cons:** a direct-mapping ontology is only as meaningful as the dictionary; Ontop only
  reloads its mapping **at boot**, so onboarding a new dataset needs a manual restart.
- **Verify:** "list X via the ontology" answers with the project prefix; an FK→object-property
  traversal joins correctly; SHACL runs on the result graph.

### M4 — config-driven metrics
**Steps:** per-dataset KPI templates (`/metrics` editor) = a named, parameterised SELECT;
the planner routes a matching question to a **deterministic executor** (fill params as safe
literals → the same SELECT-only/allowlist validator → RO execute → audited SQL).
- **Pros:** exact, auditable numbers with **no LLM arithmetic**; generalises bespoke logic.
- **Cons:** authors must write the template + name it distinctly; param extraction is simple
  (numbers/dates/listed values).
- **Verify:** define a KPI → "compute <kpi>" runs deterministically; a malicious param stays
  a quoted literal (injection contained).

### M5 — generic evals + deploy docs
**Steps:** an eval harness auto-generates schema-derived questions, runs them through the
agent, and an **LLM judge** scores correctness + grounding; a `/evals` dashboard; an EC2
`DEPLOY.md`.
- **Pros:** quality signal on *any* dataset with no golden answers; catches ungrounded answers.
- **Cons:** judge is itself an LLM (cost + not infallible).

### Phase-2 — PageIndex + OpenMetadata
**Steps:** (a) **pgvector PageIndex** — embed tables/columns at onboarding; for large schemas
retrieve a focused slice per question (full context for small ones). (b) **OpenMetadata** —
run the official OM stack separately; onboarding pushes service→database→schema→tables→columns.
- **Pros:** scales to big schemas; a real searchable catalog with lineage.
- **Cons:** embeddings add a model + pgvector; OM is RAM-heavy and its own stack.

---

## 3. Cross-cutting decisions (pros & cons)

- **Artifact as a file the agent mtime-caches** (`/artifacts/<id>/dataset.json`), with the
  registry as source of truth. *Pro:* hot-swap on re-onboard, no DB on the read path. *Con:*
  two places to keep in sync (mitigated: onboarding writes both).
- **Read-only sandbox + AST validator (defence in depth).** *Pro:* a generated/LLM query
  can't write or escape the dataset even if one layer is wrong. *Con:* a few legitimate
  queries get auto-LIMITed / rejected; acceptable.
- **Manual Ontop restart on onboard (no docker socket).** *Pro:* no privilege-escalation
  surface. *Con:* SPARQL for a new dataset waits for an operator restart (SQL is immediate).
- **Generate Ontop-safe types only (no jsonb).** *Pro:* avoids Ontop's type pitfalls by
  construction (no view workaround needed). *Con:* complex types are flattened to text.
- **One Postgres for all datasets (schema-per-project).** *Pro:* simple, strong per-schema
  grants. *Con:* shared resource; a per-tenant DB would isolate further at higher cost.
- **HTMX over an SPA.** *Pro:* fast to build, server-rendered, low JS. *Con:* less rich UX.

---

## 4. Testing strategy

- **Offline first:** the risky pure-logic pieces (profiler, generators, validator, metric
  fill, ontology/SHACL emit) are unit-tested with no Docker/DB/LLM — `tests/test_offline_*.py`
  (50 checks). *Pro:* fast, deterministic, catches most bugs before a container build.
- **Live verification per milestone:** bring the stack up in Docker, drive the real APIs +
  UI, confirm end-to-end (DDL+load, RO sandbox blocks writes, NL→SQL/SPARQL/metric answers,
  PageIndex slice, OM push).
- **Generic /evals** for ongoing answer-quality regression on any dataset.

---

## 5. Sequence summary (do this, in order)

1. Stack + Docker Compose skeleton (postgres + redis) and the read-only role.
2. **M1**: profiler → review UI → generators → loader → SQL agent + artifact seam. Verify.
3. **M2**: registry + `ds_<id>` + picker; data dictionary; multi-file merge. Verify.
4. **M3**: ontology/OBDA/SHACL generators → one Ontop (namespaced) → agent SPARQL path. Verify.
5. **M4**: metric templates + deterministic executor + planner routing. Verify.
6. **M5**: eval harness + `/evals` + deploy docs. Verify.
7. **Phase-2**: pgvector PageIndex (threshold-gated) → OpenMetadata push. Verify.
8. Throughout: keep the offline test suite green; commit per milestone.

See **[DEPLOY.md](DEPLOY.md)** to run it on AWS EC2, and **[HANDOFF.md](HANDOFF.md)** for the
current built/verified status.
