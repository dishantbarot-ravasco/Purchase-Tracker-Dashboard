"""
Google sign-in asks for no more than sign-in needs (2026-10-01,
data-minimisation pass): openid + email, online access only. The app reads
nothing from Google but the verified email and never acts on the account
afterwards, so "profile" and a Google refresh token (access_type=offline)
are not requested.
"""

from urllib.parse import parse_qs, urlparse

import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_the_google_consent_request_is_minimal(settings):
    settings.GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
    settings.GOOGLE_CLIENT_SECRET = "test-secret"
    settings.GOOGLE_OAUTH_REDIRECT_URI = "https://example.test/api/auth/google/callback/"

    response = APIClient().get("/api/auth/google/login/")
    assert response.status_code == 302
    query = parse_qs(urlparse(response["Location"]).query)

    assert set(query["scope"][0].split()) == {"openid", "email"}
    assert query["access_type"] == ["online"]
