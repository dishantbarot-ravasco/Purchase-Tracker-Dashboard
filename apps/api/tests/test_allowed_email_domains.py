"""
Sign-in and accounts are limited to the company email domains,
settings.ALLOWED_EMAIL_DOMAINS - ravasco.com and hindustanrubbers.com
(owner, 2026-10-01). The match is on the whole domain, so a look-alike
("evilravasco.com", "ravasco.com.evil.io") is refused.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from apps.api.permissions import is_allowed_email_domain
from apps.api.tests.factories import make_user
from apps.core.models import PTUser


@pytest.mark.parametrize("email, allowed", [
    ("someone@ravasco.com", True),
    ("Someone@HindustanRubbers.com", True),
    (" someone@hindustanrubbers.com ", True),
    ("someone@evilravasco.com", False),
    ("someone@ravasco.com.evil.io", False),
    ("someone@gmail.com", False),
    ("@ravasco.com", False),
    ("ravasco.com", False),
    ("", False),
    (None, False),
])
def test_only_the_company_domains_are_allowed(email, allowed):
    assert is_allowed_email_domain(email) is allowed


def test_the_domains_come_from_settings(settings):
    settings.ALLOWED_EMAIL_DOMAINS = ("example.test",)
    assert is_allowed_email_domain("a@example.test")
    assert not is_allowed_email_domain("a@ravasco.com")


@pytest.mark.django_db
def test_an_admin_can_create_a_hindustanrubbers_account_but_not_an_outside_one():
    client = APIClient()
    client.force_authenticate(user=make_user(email="admin@ravasco.com", role="admin"))
    body = {"fullName": "New Person", "role": "user", "password": "An0therStr0ngPass!"}

    assert client.post("/api/auth/users/create", {**body, "email": "new@hindustanrubbers.com"},
                       format="json").status_code == 201
    refused = client.post("/api/auth/users/create", {**body, "email": "new@gmail.com"}, format="json")
    assert refused.status_code == 400
    assert "@ravasco.com or @hindustanrubbers.com" in str(refused.json())


@pytest.mark.django_db
def test_create_pt_user_accepts_both_domains():
    call_command("create_pt_user", email="first@hindustanrubbers.com", password="An0therStr0ngPass!", role="viewer")
    assert PTUser.objects.filter(email="first@hindustanrubbers.com").exists()
    with pytest.raises(CommandError):
        call_command("create_pt_user", email="x@gmail.com", password="An0therStr0ngPass!", role="viewer")
