from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.schemas.enums import ProviderName
from app.services.providers.google_health.oauth import GoogleOAuth


@pytest.fixture
def google_oauth() -> GoogleOAuth:
    return GoogleOAuth(
        user_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name=ProviderName.GOOGLE_HEALTH.value,
        api_base_url="https://health.googleapis.com",
    )


def test_authorize_url_always_asks_which_google_account(google_oauth: GoogleOAuth) -> None:
    """T-CYK-1009 F1: the browser that starts the link may already be signed in to another
    Google account, so Google must always ask which account, then ask for consent."""
    url, _ = google_oauth._build_auth_url("synthetic-state")

    assert parse_qs(urlparse(url).query)["prompt"] == ["select_account consent"]
