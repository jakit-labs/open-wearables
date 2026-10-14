"""runtime_role_grants

BH.Loop fork (T-CYK-1009 C1): the API, worker and beat log in as a member of
``bh_loop_wearables_runtime``, never as the owner that runs these migrations, so
they cannot alter, truncate or drop what the owner built. The role gets row
access and nothing else: SELECT, INSERT, UPDATE and DELETE on every table, only
SELECT on ``alembic_version`` (the boot gate reads it), USAGE and SELECT on every
sequence, and CONNECT on this database. Never ALL, TRUNCATE, REFERENCES or
TRIGGER: TRUNCATE ignores row-level security.

The role is created by the operator of the database before the first migration
(BH.Loop's role bootstrap); without it this revision fails. A later revision
that adds a table grants the same row access on it.

Revision ID: b9ecf3d432d7
Revises: a7c3e9f1b2d4

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9ecf3d432d7"
down_revision: Union[str, None] = "a7c3e9f1b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "bh_loop_wearables_runtime"


def _on_this_database(statement: str) -> None:
    """Run ``statement`` with ``%I`` bound to the name of the database being migrated."""
    op.execute(f"DO $$ BEGIN EXECUTE format('{statement}', current_database()); END $$")


def upgrade() -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")
    _on_this_database(f"GRANT CONNECT ON DATABASE %I TO {RUNTIME_ROLE}")


def downgrade() -> None:
    _on_this_database(f"REVOKE CONNECT ON DATABASE %I FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}")
