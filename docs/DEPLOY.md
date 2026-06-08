# DEPLOY — ExcelUtilitySPARQL on EC2

A single Docker host runs the whole stack (postgres, redis, onboarding, agent,
ui, ontop). Suitable for a POC / internal demo. ~4 GB RAM is enough; Ontop (JVM)
is the heaviest service.

## 1. Provision

- Ubuntu 22.04/24.04 EC2 instance (t3.medium or larger — 2 vCPU / 4 GB).
- Security group: open **22** (SSH, your IP only) and **8080** (the UI). Keep
  Postgres (5432), the agent (8000), onboarding (8001), and Ontop (8090)
  **closed** to the internet — they're only needed inside the Docker network.
- Attach enough EBS (20 GB+) for images + Postgres data.

## 2. Install Docker

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker
```

## 3. Get the code + the Ontop JDBC driver

```bash
git clone https://github.com/mahi80/ExcelUilitySQPARL.git
cd ExcelUilitySQPARL
# The Postgres JDBC jar is gitignored — fetch it into the ontop build context:
curl -L -o ontop/postgresql-42.7.4.jar \
  https://jdbc.postgresql.org/download/postgresql-42.7.4.jar
```

## 4. Configure

```bash
cp .env.example .env
# Edit .env and set, at minimum:
#   ANTHROPIC_API_KEY=sk-ant-...            (required for NL→SQL/SPARQL + evals)
#   POSTGRES_PASSWORD / POSTGRES_RO_PASSWORD (strong, distinct)
#   POSTGRES_DSN / POSTGRES_RO_DSN           (match the passwords above)
#   UI_AUTH_USER / UI_AUTH_PASSWORD          (the login)
#   UI_SESSION_SECRET=$(openssl rand -hex 32)
# Keep the default ports unless you need to remap them.
```

## 5. Run

```bash
docker compose up -d --build          # first build pulls the Ontop JVM image (~1-2 min)
docker compose ps                     # all services should become healthy
```

Open **http://<EC2_PUBLIC_IP>:8080** → log in → upload a workbook → review → chat.

## 6. SPARQL (Ontop)

Ontop loads its mapping at boot. After onboarding (or deleting) a dataset, the
combined config is regenerated and `/generate` returns `ontop_restart_required`.
Apply it with:

```bash
docker compose restart ontop
```

SQL chat and metrics work immediately; only the SPARQL path needs the restart.
(One Ontop serves all datasets via per-project IRI prefixes, so switching the
active project in the UI needs no restart.)

## 7. Operate

- **Logs:** `docker compose logs -f agent` (or onboarding / ui / ontop).
- **DB shell:** `docker exec -it exutil-postgres psql -U excelutil -d excelutil`.
- **Backup:** the `postgres_data` volume holds all datasets; the `artifacts`
  volume holds generated schema-context/ontology/uploads. Snapshot both
  (`docker run --rm -v exutil_postgres_data:/v -v $PWD:/b alpine tar czf /b/pg.tgz /v`).
- **Reset:** `docker compose down -v` wipes datasets + artifacts (fresh start).

## 8. Security notes

- Generated queries run as a **read-only** Postgres role with
  `default_transaction_read_only=on` + a `statement_timeout`; the validator
  independently rejects non-SELECT / off-allowlist / multi-statement queries.
- Auth is a single shared credential (`UI_AUTH_*`) over a signed session cookie —
  fine for a POC behind a tight security group. Put it behind TLS (a reverse
  proxy / ALB) and real accounts before any real exposure.
- **Rotate** any Anthropic key that has appeared in chat/logs.
- The agent, onboarding, Ontop, Postgres, and Redis ports must **not** be exposed
  publicly — only the UI (8080).

## Phase-2 features

### pgvector PageIndex (built-in)
The Postgres image is `pgvector/pgvector:pg16`. At onboarding, each table/column is
embedded (fastembed / BAAI/bge-small-en-v1.5, CPU; ~130 MB model downloaded on first
use) into `meta.schema_embedding`. For **large** schemas (> `PAGEINDEX_MIN_COLUMNS`,
default 40, or > 8 tables) the agent retrieves a focused schema slice per question via
pgvector cosine; small schemas keep the full context. No config needed; tune with
`PAGEINDEX_MIN_COLUMNS` / `PAGEINDEX_SLICE_TABLES`.

### OpenMetadata catalog (opt-in)
OpenMetadata runs as its **own** stack (it brings its own Postgres + Elasticsearch):

```bash
# from the repo root — uses the vendored official compose
docker compose -f catalog/openmetadata.yml -p om up -d openmetadata-server
# wait ~3-5 min; OM UI at http://localhost:8585 (admin@open-metadata.org / admin)
```

Then point onboarding at it so each onboarded dataset is pushed to the catalog
(service → database → schema → tables → columns):

```bash
# in .env (the onboarding container reaches the separate OM stack via host-gateway):
OPENMETADATA_HOST=http://host.docker.internal:8585
docker compose up -d onboarding        # recreate to pick up the env
```

A dataset is pushed automatically on `/generate`, or on demand:
`curl -XPOST http://localhost:8001/projects/<id>/catalog/push`. Leave
`OPENMETADATA_HOST` blank to disable. OM is heavy (~3-4 GB RAM) — give the host
enough memory, and remap OM's Postgres host port if 5432 is taken (the vendored
compose uses 5435:5432).
