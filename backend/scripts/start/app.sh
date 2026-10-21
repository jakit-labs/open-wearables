#!/bin/bash
set -e -x

# T-CYK-1009 AC-4: the API never migrates its own database. Schema changes and
# seeds run as their own step (scripts/start/migrate.sh) before the API starts,
# so a database that is not at the Alembic head refuses the boot. A check that
# cannot run at all (settings, connection) refuses too, under its own name.
current=$(uv run alembic current) || { echo WEARABLES_SCHEMA_CHECK_FAILED >&2; exit 78; }
grep -q '(head)' <<<"$current" || { echo WEARABLES_SCHEMA_BEHIND >&2; exit 78; }

# Init app
echo "Starting the FastAPI application..."
if [ "$ENVIRONMENT" = "local" ]; then
    uv run fastapi dev app/main.py --host 0.0.0.0 --port "${API_PORT:-8000}"
else
    uv run fastapi run app/main.py --host 0.0.0.0 --port "${API_PORT:-8000}"
fi
