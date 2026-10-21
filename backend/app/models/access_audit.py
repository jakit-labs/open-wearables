from uuid import UUID

from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import PrimaryKey, str_10, str_32, str_64


class AccessAudit(BaseDbModel):
    """One row per request the sidecar serves, denials included (BH.Loop fork, T-CYK-1009 AC-1).

    A mutation leaves two rows, its attempt and its outcome, sharing one ``request_id``.
    Rows are only ever inserted: the runtime login holds INSERT and nothing else.
    The time is the inherited ``created_at``. No column carries a request value:
    the route is its template and every ref is an opaque uuid.
    """

    __tablename__ = "access_audit"

    id: Mapped[PrimaryKey[UUID]]
    request_id: Mapped[UUID]
    actor_ref: Mapped[UUID | None]
    actor_kind: Mapped[str_32]
    effective_role: Mapped[str_32 | None]
    tenant_id: Mapped[UUID | None]
    purpose: Mapped[str_32 | None]
    target_ref: Mapped[UUID | None]
    policy_version: Mapped[str_64]
    method: Mapped[str_10]
    route: Mapped[str | None]
    status: Mapped[int | None]
    outcome: Mapped[str_10]
