-- pgvector PageIndex (Phase-2): semantic index over each dataset's tables/columns
-- so the agent can retrieve a focused schema slice for large schemas.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS meta.schema_embedding (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  text NOT NULL,
    kind        text NOT NULL,            -- 'table' | 'column'
    table_name  text NOT NULL,
    column_name text,
    content     text NOT NULL,
    embedding   vector(384) NOT NULL      -- BAAI/bge-small-en-v1.5
);

CREATE INDEX IF NOT EXISTS schema_embedding_proj_idx ON meta.schema_embedding (project_id);

-- The agent reads the index through the read-only role.
GRANT SELECT ON meta.schema_embedding TO app_ro;

SELECT 'pgvector schema_embedding ready' AS note;
