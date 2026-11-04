"""The API key seed in the migrate step (BH.Loop fork, T-CYK-1009 S20).

The operator mints the key straight into Secret Manager; Cloud Run hands it to the
migrate job as ``SEED_API_KEY``. The seed stores only its hash and display prefix,
exactly as a minted key is stored, and never prints the key.
"""

import hashlib
from contextlib import nullcontext

import pytest
from sqlalchemy.orm import Session

from app.models import ApiKey
from app.services.api_key_service import api_key_service
from scripts.init import seed_api_key

SEED_KEY = "sk-5eed0000000000000000000000c0ffee"  # synthetic


@pytest.fixture(autouse=True)
def _seed_session(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_api_key, "SessionLocal", lambda: nullcontext(db))


def test_stores_only_the_hash_and_prefix_and_the_sidecar_accepts_the_key(db: Session) -> None:
    seed_api_key.seed_api_key(SEED_KEY)

    [row] = db.query(ApiKey).all()
    assert row.key_hash == hashlib.sha256(SEED_KEY.encode()).hexdigest()
    assert row.key_prefix == SEED_KEY[:10]
    assert row.created_by is None
    assert SEED_KEY not in repr([row.key_hash, row.key_prefix, row.name])
    assert api_key_service.validate_api_key(db, SEED_KEY).id == row.id


def test_a_second_start_adds_nothing(db: Session) -> None:
    seed_api_key.seed_api_key(SEED_KEY)
    seed_api_key.seed_api_key(SEED_KEY)

    assert db.query(ApiKey).count() == 1


def test_prints_no_key(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    seed_api_key.seed_api_key(SEED_KEY)
    seed_api_key.seed_api_key(SEED_KEY)

    printed = capsys.readouterr()
    assert printed.out
    assert SEED_KEY not in printed.out + printed.err
    assert SEED_KEY[3:] not in printed.out + printed.err


@pytest.mark.parametrize("value", [SEED_KEY + "\n", SEED_KEY.upper(), "sk-short", "pk-" + SEED_KEY[3:]])
def test_a_misshapen_key_is_refused_without_printing_it(
    db: Session, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        seed_api_key.seed_api_key(value)

    printed = capsys.readouterr()
    assert "SEED_API_KEY_REFUSED" in str(refused.value.code)
    assert value.strip() not in str(refused.value.code) + printed.out + printed.err
    assert db.query(ApiKey).count() == 0


def test_no_seed_api_key_seeds_nothing(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_api_key.settings, "seed_api_key", None)

    seed_api_key.main()

    assert db.query(ApiKey).count() == 0
