"""runtime role grants on upstream tables

BH.Loop fork (T-CYK-1009 C1): the tables and sequences upstream's revisions added
since the fork was cut get the same row access ``b9ecf3d432d7`` gives: SELECT,
INSERT, UPDATE and DELETE on every table, USAGE on every sequence, only SELECT on
``alembic_version``, and only INSERT on ``access_audit`` (append-only, ``4f2b7c9d1e30``).
Never ALL, TRUNCATE, REFERENCES or TRIGGER.

Revision ID: a697b0633cf8
Revises: 5a80dff577fb

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a697b0633cf8"
down_revision: Union[str, None] = "5a80dff577fb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "bh_loop_wearables_runtime"


def upgrade() -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE SELECT, UPDATE, DELETE ON access_audit FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")


def downgrade() -> None:
    # Row access on the tables b9ecf3d432d7 covered stays; upstream's downgrades drop their own tables.
    pass
