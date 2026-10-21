from datetime import datetime
from uuid import UUID

from sqlalchemy import Dialect, Index, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64
from app.schemas.auth import ConnectionStatus


class EncryptedText(TypeDecorator[str]):
    """A provider token: Fernet ciphertext in the table, plaintext in the model (T-CYK-1009 AC-3)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else settings.token_fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else settings.token_fernet().decrypt(value.encode()).decode()


class UserConnection(BaseDbModel):
    """OAuth connections to external cloud providers (Suunto, Garmin, Polar, Coros)"""

    __table_args__ = (
        Index(
            "ix_user_connection_token_expiry",
            "token_expires_at",
            postgresql_where="status = 'active'",
        ),
        Index("ix_user_connection_user_provider", "user_id", "provider", unique=True),
        Index("ix_user_connection_status_user_id", "status", "user_id"),
        Index(
            "ix_user_connection_provider_external_id",
            "provider",
            "provider_user_id",
            postgresql_where="provider_user_id IS NOT NULL AND status = 'active'",
        ),
    )
    __tablename__ = "user_connection"

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[str_64]  # 'suunto', 'garmin', 'polar', 'coros'

    # Provider user data
    provider_user_id: Mapped[str | None]
    provider_username: Mapped[str | None]

    # OAuth tokens (optional for SDK-based providers like Apple)
    access_token: Mapped[str | None] = mapped_column(EncryptedText)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText)
    token_expires_at: Mapped[datetime | None]
    scope: Mapped[str | None]

    # Metadata
    status: Mapped[ConnectionStatus]
    last_synced_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
