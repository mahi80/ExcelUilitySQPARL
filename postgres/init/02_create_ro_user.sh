#!/bin/bash
# Creates the read-only LOGIN user for the agent's query sandbox.
# Runs after 01_roles.sql (which created the app_ro NOLOGIN group).
# Reads $POSTGRES_RO_USER / $POSTGRES_RO_PASSWORD from the container env.
set -euo pipefail

: "${POSTGRES_RO_USER:?must be set}"
: "${POSTGRES_RO_PASSWORD:?must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${POSTGRES_RO_USER}') THEN
      CREATE ROLE "${POSTGRES_RO_USER}" LOGIN PASSWORD '${POSTGRES_RO_PASSWORD}' IN ROLE app_ro;
    ELSE
      ALTER ROLE "${POSTGRES_RO_USER}" WITH LOGIN PASSWORD '${POSTGRES_RO_PASSWORD}';
    END IF;
  END
  \$\$;

  GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO app_ro;

  -- Hard read-only + a statement timeout so a runaway generated query can't wedge the DB.
  ALTER ROLE "${POSTGRES_RO_USER}" SET default_transaction_read_only = on;
  ALTER ROLE "${POSTGRES_RO_USER}" SET statement_timeout = '5s';
EOSQL

echo "Read-only login role ${POSTGRES_RO_USER} created/updated."
