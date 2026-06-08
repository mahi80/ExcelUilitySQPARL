-- Read-only GROUP role for the bot's query sandbox.
-- NOLOGIN: a login user is created IN ROLE app_ro by 02_create_ro_user.sh and
-- inherits its grants. The onboarding service grants USAGE/SELECT on each
-- generated dataset schema (ds_*) to this group after creating its tables.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') THEN
    CREATE ROLE app_ro NOLOGIN;
  END IF;
END
$$;

SELECT 'app_ro group role ready' AS note;
