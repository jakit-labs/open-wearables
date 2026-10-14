-- Runs via Postgres /docker-entrypoint-initdb.d on first cluster init (local
-- docker compose only). The runtime_role_grants migration grants row access to
-- this role and fails without it; on managed Postgres the database operator
-- creates it (BH.Loop: its role bootstrap). NOLOGIN: a login joins it.
CREATE ROLE bh_loop_wearables_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
