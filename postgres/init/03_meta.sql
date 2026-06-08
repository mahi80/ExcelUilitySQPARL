-- Project registry (M2). One row per onboarded dataset; the source of truth for
-- the project list, status, and the last-good agent artifact. Each project owns
-- an isolated schema ds_<id>. Runs after 01_roles.sql (app_ro) + 02 (RO user).
CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.project (
    id              text PRIMARY KEY,            -- slug, e.g. 'p_1a2b3c4d'; schema = 'ds_'||id
    name            text NOT NULL,
    domain          text,                        -- dataset-level domain blurb
    glossary        jsonb NOT NULL DEFAULT '[]', -- [{term, definition}]
    schema_name     text NOT NULL,
    status          text NOT NULL DEFAULT 'profiling',  -- profiling | ready | failed
    ontology_ns     text,                        -- per-project ontology IRI namespace (M3)
    ontology_prefix text,                        -- short SPARQL prefix (M3)
    artifact        jsonb,                       -- last good build_artifact() output
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- The agent lists projects through the read-only role.
GRANT USAGE ON SCHEMA meta TO app_ro;
GRANT SELECT ON meta.project TO app_ro;

SELECT 'meta.project registry ready' AS note;
