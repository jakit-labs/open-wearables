#!/bin/bash
set -e -x

# T-CYK-1009 AC-4: the one step that changes the schema. It runs as the database
# owner, before any API, worker or beat process starts; app.sh refuses a schema
# that is not at head. Any failure stops the step: nothing here is retried later.
uv run alembic upgrade head

uv run python scripts/init_provider_settings.py
uv run python scripts/init_device_priorities.py
# Uses ADMIN_EMAIL/ADMIN_PASSWORD, so this step reads the same environment as the API.
uv run python scripts/init/seed_admin.py
uv run python scripts/init/seed_series_types.py
uv run python scripts/init/seed_archival_settings.py
uv run python scripts/init/seed_webhook_event_types.py
