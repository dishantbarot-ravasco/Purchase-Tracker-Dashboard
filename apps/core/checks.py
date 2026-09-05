"""
apps/core/checks.py — custom Django system checks (added 2026-09-05,
hardening pass).

Both checks below run as part of `manage.py check --deploy --fail-level
WARNING` - the same command CI already runs on every push (see
.github/workflows/ci.yml) - so a real misconfiguration in either area fails
CI instead of silently shipping. Neither check touches request-time
behavior at all; they only inspect settings/DB state at check-time.
"""

from django.conf import settings
from django.core.checks import Warning, register


@register()
def check_jwt_signing_key_is_independent(app_configs, **kwargs):
    """JWT_SIGNING_KEY defaults to DJANGO_SECRET_KEY when unset (see
    config/settings.py) for zero-downtime compatibility - convenient in
    dev, but it means a leak of SECRET_KEY (which also signs Django's
    session/CSRF tokens) would let an attacker forge JWTs too, not just
    admin sessions. Only worth flagging outside DEBUG - a dev environment
    reusing the fallback is fine."""
    if settings.DEBUG:
        return []
    if settings.JWT_SIGNING_KEY == settings.SECRET_KEY:
        return [
            Warning(
                "JWT_SIGNING_KEY is not set independently of DJANGO_SECRET_KEY - it is "
                "silently falling back to SECRET_KEY. A leak of SECRET_KEY would let an "
                "attacker forge both Django admin sessions/CSRF tokens AND JWTs for any "
                "PTUser. Set JWT_SIGNING_KEY to its own high-entropy value in production.",
                id="apps.core.W001",
            )
        ]
    return []


@register()
def check_no_unexpected_django_superuser(app_configs, **kwargs):
    """Real login in this app never touches Django's own auth.User model at
    all (see apps/api/auth_backend.py's module docstring - PTUser is a
    separate, unrelated model) - so there is normally no documented way to
    actually sign in to Django Admin (/admin/, session-based) at all. If an
    auth.User superuser exists anyway (e.g. created once via
    `manage.py createsuperuser` and forgotten about), it is an
    undocumented, unaudited path into an interface that can edit PTUser
    rows directly (bypassing users_views.py's own PTAuditLog-writing update
    path - see apps/core/admin.py's PTUserAdmin) - worth surfacing loudly
    rather than leaving it as a silent, easy-to-forget exposure."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        count = User.objects.filter(is_superuser=True, is_active=True).count()
    except Exception:
        # Database not migrated yet (e.g. a fresh `manage.py check` before
        # the first `migrate`) - nothing meaningful to report either way.
        return []
    if count:
        return [
            Warning(
                f"{count} active Django auth.User superuser account(s) exist. This app's "
                "real login never uses auth.User (see apps/api/auth_backend.py) - an "
                "auth.User superuser is an undocumented path into /admin/ that bypasses "
                "this app's own audit logging for PTUser edits. Confirm this is intentional; "
                "if not, deactivate or delete it.",
                id="apps.core.W002",
            )
        ]
    return []
