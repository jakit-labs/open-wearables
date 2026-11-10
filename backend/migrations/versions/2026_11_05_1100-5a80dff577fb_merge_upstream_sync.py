"""merge upstream sync

BH.Loop fork (T-CYK-1009): joins the fork's own revisions (runtime role grants,
access audit) and upstream's revisions since the fork was cut. Changes nothing.

Revision ID: 5a80dff577fb
Revises: 4f2b7c9d1e30, 3f6b9c2d8e41

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "5a80dff577fb"
down_revision: Union[str, Sequence[str], None] = ("4f2b7c9d1e30", "3f6b9c2d8e41")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
