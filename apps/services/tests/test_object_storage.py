"""
apps/services/object_storage.py - the R2 wrapper. boto3 is driven through
botocore's Stubber, so these assert the exact S3 calls without a network or
a Cloudflare account.
"""

import datetime

import boto3
import pytest
from botocore.stub import Stubber

from apps.services import object_storage

_R2 = {
    "R2_ACCOUNT_ID": "acct123",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_ENDPOINT_URL": "",
    "R2_BUCKETS": {"po": "pt-po", "invoice": "pt-invoice", "backup": "pt-backups"},
}


@pytest.fixture
def r2(settings):
    for name, value in _R2.items():
        setattr(settings, name, value)
    return settings


@pytest.fixture
def stubbed(r2, monkeypatch):
    client = boto3.client("s3", endpoint_url="https://acct123.r2.cloudflarestorage.com", region_name="auto",
                          aws_access_key_id="key", aws_secret_access_key="secret")
    stubber = Stubber(client)
    monkeypatch.setattr(object_storage, "_client", lambda: client)
    with stubber:
        yield stubber
    stubber.assert_no_pending_responses()


def test_unconfigured_storage_names_what_is_missing(settings):
    settings.R2_ACCOUNT_ID = ""
    settings.R2_ACCESS_KEY_ID = "key"
    settings.R2_SECRET_ACCESS_KEY = ""
    settings.R2_BUCKETS = {"po": "", "invoice": "", "backup": ""}
    with pytest.raises(object_storage.StorageNotConfigured) as exc:
        object_storage.require_configured("backup")
    assert "R2_ACCOUNT_ID" in str(exc.value)
    assert "R2_SECRET_ACCESS_KEY" in str(exc.value)
    assert "R2_BUCKET_BACKUPS" in str(exc.value)
    assert "R2_ACCESS_KEY_ID" not in str(exc.value)
    assert object_storage.is_configured("po") is False


def test_each_kind_has_its_own_bucket(r2):
    r2.R2_BUCKETS = {"po": "pt-po", "invoice": "", "backup": "pt-backups"}
    assert object_storage.is_configured("po") is True
    assert object_storage.is_configured("invoice") is False
    with pytest.raises(object_storage.StorageNotConfigured, match="R2_BUCKET_INVOICE"):
        object_storage.require_configured("invoice")


def test_an_unknown_kind_is_refused(r2):
    with pytest.raises(ValueError, match="Unknown storage kind"):
        object_storage.require_configured("photos")


def test_list_objects_reads_every_page(stubbed):
    when = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    stubbed.add_response("list_objects_v2",
                         {"Contents": [{"Key": "postgres/a.dump", "Size": 10, "LastModified": when}],
                          "IsTruncated": True, "NextContinuationToken": "t1"},
                         {"Bucket": "pt-backups", "Prefix": "postgres/"})
    stubbed.add_response("list_objects_v2",
                         {"Contents": [{"Key": "postgres/b.dump", "Size": 20, "LastModified": when}],
                          "IsTruncated": False},
                         {"Bucket": "pt-backups", "Prefix": "postgres/", "ContinuationToken": "t1"})
    found = object_storage.list_objects("backup", "postgres/")
    assert [(o.key, o.size) for o in found] == [("postgres/a.dump", 10), ("postgres/b.dump", 20)]


def test_delete_objects_goes_in_chunks_of_a_thousand(stubbed):
    keys = [f"postgres/{i}.dump" for i in range(1001)]
    stubbed.add_response("delete_objects", {},
                         {"Bucket": "pt-backups", "Delete": {"Objects": [{"Key": k} for k in keys[:1000]], "Quiet": True}})
    stubbed.add_response("delete_objects", {},
                         {"Bucket": "pt-backups", "Delete": {"Objects": [{"Key": keys[1000]}], "Quiet": True}})
    object_storage.delete_objects("backup", keys)


def test_presigned_url_is_short_lived_and_names_the_object(r2):
    url = object_storage.presigned_url("po", "hrs/3000001167.pdf", expires_seconds=120)
    assert url.startswith("https://acct123.r2.cloudflarestorage.com/pt-po/hrs/3000001167.pdf?")
    assert "X-Amz-Expires=120" in url
