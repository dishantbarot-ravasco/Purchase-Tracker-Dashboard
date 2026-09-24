"""
Unit tests for apps/services/google_client.py's find_file_id_by_title()
query construction and its _escape() helper - the one place in this app
that hand-builds a query string for an external API rather than using a
parameterized call. Narrow and deliberately scoped: no real Drive API call
is made anywhere here (get_drive_service() is monkeypatched with a fake
that just records the query string it was handed), so this costs no API
quota regardless of plan/tier - the point is guarding the query-building
logic itself, not exercising Google's API.

Real precedent for why this is worth having, not just defensive padding:
this exact function already broke production once - using Drive API v2's
"title = " field name instead of v3's "name = " returned an opaque HTTP 400
Invalid Value with no indication of what was wrong (see CLAUDE.md's "Drive
API v3, not v2" section and this module's own inline comment). A future
edit that reintroduces that mistake, or that stops escaping one of the
three clauses, would otherwise only be caught by a real sync failing
against live Drive data.

download_file_bytes()'s chunked-download loop is deliberately NOT tested
here - it's a thin wrapper around googleapiclient's own well-tested
MediaIoBaseDownload with no query-string-shaped risk, unlike this function.
"""

import pytest

from apps.services.google_client import _escape, find_file_id_by_title, list_files_in_folder


class _FakeFilesResource:
    """Records the exact kwargs its .list() was called with, and returns a
    canned response - stands in for googleapiclient's real
    service.files().list(...).execute() chain with no network call."""

    def __init__(self, files_response: list[dict]):
        self.files_response = files_response
        self.last_list_kwargs = None

    def list(self, **kwargs):
        self.last_list_kwargs = kwargs
        return self

    def execute(self):
        return {"files": self.files_response}


class _FakeDriveService:
    def __init__(self, files_response: list[dict]):
        self._files_resource = _FakeFilesResource(files_response)

    def files(self):
        return self._files_resource


@pytest.fixture
def fake_service(monkeypatch):
    """Installs a fake Drive service in place of get_drive_service(), and
    hands the test the fake so it can inspect what query was built."""
    service = _FakeDriveService(files_response=[{"id": "file123", "name": "some file"}])

    import apps.services.google_client as google_client
    monkeypatch.setattr(google_client, "get_drive_service", lambda: service)
    return service


class TestFindFileIdByTitleQueryConstruction:
    def test_uses_v3s_name_field_not_v2s_title_field(self, fake_service):
        """The exact bug that already hit production once - see this
        file's own module docstring. A v2-style `title = '...'` clause
        returns an opaque HTTP 400 from a real v3 client with no
        indication of what's wrong; only the query string itself
        distinguishes a correct fix from a regression back to it."""
        find_file_id_by_title("My File.csv")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "name = 'My File.csv'" in query
        assert "title = " not in query

    def test_always_excludes_trashed_files(self, fake_service):
        find_file_id_by_title("My File.csv")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "trashed = false" in query

    def test_parent_id_adds_a_parents_clause_when_given(self, fake_service):
        find_file_id_by_title("My File.csv", parent_id="folder456")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "'folder456' in parents" in query

    def test_no_parent_id_omits_the_parents_clause(self, fake_service):
        find_file_id_by_title("My File.csv")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "in parents" not in query

    def test_mime_type_adds_a_mimetype_clause_when_given(self, fake_service):
        find_file_id_by_title("My File", mime_type="application/vnd.google-apps.spreadsheet")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "mimeType = 'application/vnd.google-apps.spreadsheet'" in query

    def test_a_quote_in_the_title_is_escaped_not_left_to_break_the_clause(self, fake_service):
        """Confirms find_file_id_by_title() actually routes the title
        through _escape() before interpolating it - not just that
        _escape() works in isolation (tested separately below)."""
        find_file_id_by_title("Bob's File.csv")

        query = fake_service._files_resource.last_list_kwargs["q"]
        assert "name = 'Bob\\'s File.csv'" in query

    def test_returns_the_first_matching_files_id(self, fake_service):
        result = find_file_id_by_title("My File.csv")
        assert result == "file123"

    def test_raises_file_not_found_when_nothing_matches(self, monkeypatch):
        import apps.services.google_client as google_client
        monkeypatch.setattr(google_client, "get_drive_service", lambda: _FakeDriveService(files_response=[]))

        with pytest.raises(FileNotFoundError):
            find_file_id_by_title("Nonexistent File.csv")


class TestListFilesInFolder:
    """Covers list_files_in_folder()'s Python-side name_prefix re-check
    (added for sync_rodtep.py's whole-folder listing - see that function's
    own docstring). Real bug, found and fixed 2026-09-10 (reported as "the
    rodtep script sync is not working" right after a new file was added to
    the Drive folder): this re-check used a case-sensitive str.startswith(),
    inconsistent with Drive's own case-insensitive "contains" query - a
    validly-matching new file named with different casing than the
    established "RODTEP-JNPT-<N>" convention could be silently dropped right
    back out here, indistinguishable from "the sync isn't picking up the
    new file" at all."""

    def test_prefix_match_is_case_insensitive(self, monkeypatch):
        service = _FakeDriveService(files_response=[
            {"id": "f1", "name": "Rodtep-JNPT-16.xlsx"},  # differently cased than the usual "RODTEP-..." convention
            {"id": "f2", "name": "rodtep-jnpt-17.xlsx"},
            {"id": "f3", "name": "Unrelated File.xlsx"},
        ])
        import apps.services.google_client as google_client
        monkeypatch.setattr(google_client, "get_drive_service", lambda: service)

        files = list_files_in_folder("folder123", name_prefix="RODTEP")

        names = {f["name"] for f in files}
        assert names == {"Rodtep-JNPT-16.xlsx", "rodtep-jnpt-17.xlsx"}

    def test_still_a_prefix_check_not_a_bare_substring_match(self, monkeypatch):
        """A file that merely CONTAINS "rodtep" somewhere but doesn't start
        with it must still be excluded - only the case-sensitivity was the
        bug, not the prefix-vs-substring distinction itself."""
        service = _FakeDriveService(files_response=[
            {"id": "f1", "name": "RODTEP-JNPT-1.xlsx"},
            {"id": "f2", "name": "Old RODTEP backup.xlsx"},
        ])
        import apps.services.google_client as google_client
        monkeypatch.setattr(google_client, "get_drive_service", lambda: service)

        files = list_files_in_folder("folder123", name_prefix="RODTEP")

        assert [f["name"] for f in files] == ["RODTEP-JNPT-1.xlsx"]

    def test_no_prefix_returns_everything_unfiltered(self, monkeypatch):
        service = _FakeDriveService(files_response=[{"id": "f1", "name": "Anything.xlsx"}])
        import apps.services.google_client as google_client
        monkeypatch.setattr(google_client, "get_drive_service", lambda: service)

        files = list_files_in_folder("folder123")

        assert [f["name"] for f in files] == ["Anything.xlsx"]


class TestEscape:
    def test_escapes_a_single_quote(self):
        assert _escape("Bob's File") == "Bob\\'s File"

    def test_escapes_a_backslash(self):
        assert _escape(r"path\to\file") == r"path\\to\\file"

    def test_leaves_an_ordinary_string_unchanged(self):
        assert _escape("My File.csv") == "My File.csv"


class _PagedFilesResource:
    """Serves `pages` one per list() call, keyed by the pageToken passed in -
    the shape of Drive's real paginated response."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        index = int(self.calls[-1].get("pageToken") or 0)
        resp = {"files": self.pages[index]}
        if index + 1 < len(self.pages):
            resp["nextPageToken"] = str(index + 1)
        return resp


class TestPaginationAndDuplicates:
    def _install(self, monkeypatch, pages):
        resource = _PagedFilesResource(pages)
        service = type("S", (), {"files": lambda self: resource})()
        import apps.services.google_client as google_client
        monkeypatch.setattr(google_client, "get_drive_service", lambda: service)
        return resource

    def test_list_follows_every_page(self, monkeypatch):
        page1 = [{"id": f"a{i}", "name": f"RODTEP-JNPT-{i:03}.xlsx"} for i in range(100)]
        page2 = [{"id": "last", "name": "RODTEP-JNPT-100.xlsx"}]
        resource = self._install(monkeypatch, [page1, page2])
        files = list_files_in_folder("folder123", name_prefix="RODTEP")
        assert len(files) == 101
        assert files[-1]["id"] == "last"
        assert len(resource.calls) == 2
        assert "nextPageToken" in resource.calls[0]["fields"]

    def test_find_orders_newest_first_and_warns_on_duplicates(self, monkeypatch, caplog):
        resource = self._install(monkeypatch, [[{"id": "newest", "name": "PO.csv"}, {"id": "older", "name": "PO.csv"}]])
        with caplog.at_level("WARNING", logger="apps.services.google_client"):
            assert find_file_id_by_title("PO.csv") == "newest"
        assert resource.calls[0]["orderBy"] == "modifiedTime desc"
        assert "2 Drive files are named 'PO.csv'" in caplog.text
