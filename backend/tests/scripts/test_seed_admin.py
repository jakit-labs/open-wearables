"""The admin seed in the migrate step (BH.Loop fork, T-CYK-1009 CSO F4).

A password the schema refuses fails the step. The traceback it prints must not
carry the password: pydantic would otherwise echo it as ``input_value``.
"""

import traceback
from contextlib import nullcontext

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from scripts.init import seed_admin

SHORT_PASSWORD = "syn7xq"  # synthetic, below min_password_length


def test_a_refused_admin_password_is_not_printed(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seed_admin, "SessionLocal", lambda: nullcontext(db))

    with pytest.raises(ValidationError) as refused:
        seed_admin.seed_admin("synthetic-admin@synthetic.test", SHORT_PASSWORD)

    printed = "".join(traceback.format_exception(refused.value))
    assert "password" in printed
    assert SHORT_PASSWORD not in printed
