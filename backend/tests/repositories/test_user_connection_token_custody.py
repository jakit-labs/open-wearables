"""Provider OAuth tokens at rest (BH.Loop fork, T-CYK-1009 AC-3).

``user_connection.access_token`` and ``refresh_token`` hold Fernet ciphertext made
with ``TOKEN_ENCRYPTION_KEY``. The model hands the plaintext back; a raw read of the
table never sees it. Without the key no token is written at all.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from app.config import TokenEncryptionKeyMissingError, settings
from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.model_crud.user_management import UserConnectionCreate
from tests.factories import UserFactory

ACCESS = "synthetic-access-token-1009"
REFRESH = "synthetic-refresh-token-1009"


@pytest.fixture
def repo() -> UserConnectionRepository:
    return UserConnectionRepository(UserConnection)


def _create(db: Session, repo: UserConnectionRepository) -> UUID:
    now = datetime.now(timezone.utc)
    created = repo.create(
        db,
        UserConnectionCreate(
            id=uuid4(),
            user_id=UserFactory().id,
            provider="oura",
            access_token=ACCESS,
            refresh_token=REFRESH,
            status=ConnectionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        ),
    )
    return created.id


def _stored(db: Session, connection_id: UUID) -> tuple[str, str]:
    row = db.execute(
        text("SELECT access_token, refresh_token FROM user_connection WHERE id = :id"), {"id": connection_id}
    ).one()
    return row[0], row[1]


def _assert_ciphertext_of(stored: str, plaintext: str) -> None:
    assert stored.startswith("gAAAAA")
    assert plaintext not in stored
    assert Fernet(settings.token_encryption_key.get_secret_value()).decrypt(stored.encode()).decode() == plaintext


class TestTokenCustody:
    def test_a_new_connection_stores_both_tokens_as_ciphertext(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        access, refresh = _stored(db, _create(db, repo))

        _assert_ciphertext_of(access, ACCESS)
        _assert_ciphertext_of(refresh, REFRESH)

    def test_a_token_refresh_stores_ciphertext(self, db: Session, repo: UserConnectionRepository) -> None:
        connection = repo.get(db, _create(db, repo))
        assert connection is not None

        repo.update_tokens(db, connection, "synthetic-access-token-2", "synthetic-refresh-token-2", 3600)

        access, refresh = _stored(db, connection.id)
        _assert_ciphertext_of(access, "synthetic-access-token-2")
        _assert_ciphertext_of(refresh, "synthetic-refresh-token-2")

    def test_the_model_reads_the_plaintext_back(self, db: Session, repo: UserConnectionRepository) -> None:
        connection_id = _create(db, repo)
        db.expire_all()

        connection = repo.get(db, connection_id)

        assert connection is not None
        assert (connection.access_token, connection.refresh_token) == (ACCESS, REFRESH)

    def test_without_a_key_no_token_is_written(
        self, db: Session, repo: UserConnectionRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = UserFactory().id
        monkeypatch.setattr(settings, "token_encryption_key", None)

        # The token is bound before the INSERT is sent, so the refusal comes first.
        with pytest.raises(StatementError) as refused:
            repo.create(
                db,
                UserConnectionCreate(user_id=user_id, provider="oura", access_token=ACCESS, refresh_token=REFRESH),
            )
        assert isinstance(refused.value.orig, TokenEncryptionKeyMissingError)
