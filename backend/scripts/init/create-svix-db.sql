-- Runs via Postgres /docker-entrypoint-initdb.d on first cluster init, before the
-- server accepts external connections — so svix-server never races DB creation.
-- Idempotent; on managed Postgres, where init scripts do not run, run the Python
-- fallback scripts/init/create_svix_db.py by hand.
SELECT 'CREATE DATABASE svix'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'svix')\gexec
