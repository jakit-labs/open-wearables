"""access_audit

BH.Loop fork (T-CYK-1009 AC-1): one row per request the sidecar serves, denials
included. The runtime role may INSERT rows and nothing else: it can neither read,
change nor delete the record of what it served.

Revision ID: 4f2b7c9d1e30
Revises: b9ecf3d432d7

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f2b7c9d1e30"
down_revision: Union[str, None] = "b9ecf3d432d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "bh_loop_wearables_runtime"


def upgrade() -> None:
    op.create_table(
        "access_audit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("actor_ref", sa.UUID(), nullable=True),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column("effective_role", sa.String(length=32), nullable=True),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=True),
        sa.Column("target_ref", sa.UUID(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("route", sa.Text(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(f"GRANT INSERT ON access_audit TO {RUNTIME_ROLE}")


def downgrade() -> None:
    op.drop_table("access_audit")
