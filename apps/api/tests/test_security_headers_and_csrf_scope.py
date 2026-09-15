"""
Behavioral assertions for config/security_headers.py and
config/middleware.py, added in the 2026-09-15 audit pass.

WHY THESE EXIST
---------------
Both modules were already *executed* by the suite - every APIClient request
runs the whole middleware stack, so they show ~95% line coverage. But
coverage is not assertion: no test anywhere checked WHAT the CSP actually
said, or WHICH paths the CSRF middleware applied to. Every line ran; nothing
verified the values.

That matters because both carry a security property that fails silently:

  - CSP: `script-src 'unsafe-inline'` was removed on 2026-09-05 and
    `style-src 'unsafe-inline'` on 2026-09-08, each a deliberate multi-file
    refactor (see security_headers.py's own docstring). Re-adding either to
    make one stubborn inline handler work would restore XSS exploitability
    across every page, and NOTHING would have failed. CLAUDE.md's own
    "Known gaps" entry for this had already gone stale once, claiming the
    fix was still outstanding days after it shipped - so the written record
    was not a reliable guard either.

  - AdminOnlyCsrfMiddleware: the whole JWT API depends on this middleware
    NOT applying to /api/ (Django's CsrfViewMiddleware would 403 every
    unsafe-method API call), while Django Admin depends on it DOES applying
    to /admin/. Widening the prefix breaks the API; narrowing it silently
    removes CSRF protection from the one classic session+form surface in
    the app. Neither direction announces itself.
"""

import pytest
from django.middleware.csrf import CsrfViewMiddleware
from django.test import Client, RequestFactory

from apps.api.tests.factories import make_user


def _headers_for(path="/api/auth/me"):
    """Any response works - SecurityHeadersMiddleware wraps them all,
    including 401s and static files (it sits OUTSIDE WhiteNoise precisely so
    static responses get headers too - see its module docstring)."""
    return Client().get(path)


class TestContentSecurityPolicy:
    def _csp(self):
        response = _headers_for()
        assert "Content-Security-Policy" in response, "no CSP header at all"
        return response["Content-Security-Policy"]

    def test_script_src_has_no_unsafe_inline(self):
        """The 2026-09-05 hardening removed this by extracting every inline
        <script> and every inline event-handler attribute to real files. If
        it comes back, that entire refactor is silently undone."""
        directive = next(d for d in self._csp().split(";") if d.strip().startswith("script-src"))
        assert "'unsafe-inline'" not in directive, (
            f"script-src regained 'unsafe-inline': {directive.strip()!r}. Extract "
            f"the inline script to a file under frontend/js/ instead - see "
            f"config/security_headers.py's CSP notes."
        )

    def test_style_src_has_no_unsafe_inline(self):
        """The 2026-09-08 hardening removed this by replacing every literal
        style="..." with a utility class or a data-* attribute applied by
        shared.js's applyDynamicStyles()."""
        directive = next(d for d in self._csp().split(";") if d.strip().startswith("style-src"))
        assert "'unsafe-inline'" not in directive, (
            f"style-src regained 'unsafe-inline': {directive.strip()!r}. Use a "
            f"brand.css utility class, or a data-* attribute + applyDynamicStyles()."
        )

    def test_no_directive_allows_a_wildcard_origin(self):
        """A bare `*` in any fetch directive defeats the point of having one."""
        for directive in self._csp().split(";"):
            tokens = directive.strip().split()
            assert "*" not in tokens[1:], f"wildcard origin in {directive.strip()!r}"

    @pytest.mark.parametrize("expected", [
        "default-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ])
    def test_locked_down_directives_are_present(self, expected):
        assert expected in self._csp()

    def test_chartjs_cdn_is_still_allowed(self):
        """A negative test would pass if someone dropped the CDN entirely -
        which silently breaks every chart on the dashboard (a real, confirmed
        2026-09-03 failure mode documented in security_headers.py)."""
        assert "https://cdn.jsdelivr.net" in self._csp()


class TestOtherSecurityHeaders:
    @pytest.mark.parametrize("header, expected", [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ])
    def test_header_present(self, header, expected):
        assert _headers_for()[header] == expected

    def test_permissions_policy_disables_hardware_access(self):
        value = _headers_for()["Permissions-Policy"]
        for feature in ("camera", "microphone", "geolocation", "payment"):
            assert f"{feature}=()" in value, f"{feature} is not disabled"


@pytest.mark.django_db
class TestAdminOnlyCsrfScope:
    """The scoping itself, asserted from both directions."""

    def test_api_writes_are_not_blocked_by_csrf(self):
        """enforce_csrf_checks=True makes the test client behave like a real
        browser that sent no CSRF token. An /api/ write must still reach the
        view - if AdminOnlyCsrfMiddleware ever widened to cover /api/, this
        returns 403 and the whole JWT API breaks for every browser client."""
        user = make_user(email="csrf-scope@ravasco.com", role="admin")
        client = Client(enforce_csrf_checks=True)
        client.force_login = None  # not used - this app's auth is JWT, not session

        from apps.api.auth_serializers import PTTokenObtainPairSerializer
        token = PTTokenObtainPairSerializer.get_token(user)
        response = client.post(
            "/api/auth/users/create",
            data={"email": "new-person@ravasco.com", "password": "A-Long-Enough-Pass"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token.access_token}",
        )
        assert response.status_code != 403, (
            "an /api/ write was blocked by CSRF - AdminOnlyCsrfMiddleware must "
            "apply to /admin/ ONLY (see config/middleware.py)"
        )

    def test_the_middleware_delegates_to_django_for_admin_paths(self):
        """Tests the middleware's own decision directly.

        An earlier version of this asserted that POSTing to /admin/login/
        without a CSRF token returns 403 - and it did, but for the WRONG
        reason: Django's admin login view carries its own @csrf_protect
        decorator, so it 403s whether or not this middleware runs at all.
        Verified by breaking ADMIN_PATH_PREFIX on purpose and watching the
        test still pass. A test that cannot fail when the thing it names is
        broken is worse than no test, so it was replaced with this, which
        exercises process_view()'s actual branch.
        """
        from unittest.mock import patch

        from config.middleware import AdminOnlyCsrfMiddleware

        middleware = AdminOnlyCsrfMiddleware(lambda request: None)
        factory = RequestFactory()

        def view(request):
            return None

        # An /admin/ path must be handed to Django's real CsrfViewMiddleware.
        with patch.object(CsrfViewMiddleware, "process_view", return_value=None) as parent:
            middleware.process_view(factory.post("/admin/core/ptuser/"), view, (), {})
        assert parent.called, (
            "an /admin/ request was NOT passed to Django's CSRF check - "
            "Django Admin is a classic session+form surface and must keep it"
        )

        # An /api/ path must be skipped entirely, or every unsafe-method JWT
        # API call 403s for browser clients (confirmed failure mode in the TDS
        # app this pattern is ported from).
        with patch.object(CsrfViewMiddleware, "process_view", return_value=None) as parent:
            result = middleware.process_view(factory.post("/api/auth/users/create"), view, (), {})
        assert not parent.called, (
            "an /api/ request WAS passed to Django's CSRF check - this breaks "
            "every browser-issued POST/PATCH/DELETE against the JWT API"
        )
        assert result is None

        # And anything else (the static frontend) is skipped too.
        with patch.object(CsrfViewMiddleware, "process_view", return_value=None) as parent:
            middleware.process_view(factory.post("/index.html"), view, (), {})
        assert not parent.called

    def test_the_middleware_is_actually_installed(self):
        """Guards the whole file: removing it from MIDDLEWARE would make the
        first test above pass for the wrong reason."""
        from django.conf import settings
        assert "config.middleware.AdminOnlyCsrfMiddleware" in settings.MIDDLEWARE
