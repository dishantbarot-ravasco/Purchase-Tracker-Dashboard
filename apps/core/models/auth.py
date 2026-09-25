"""
apps/core/models/auth.py - Accounts and auth state: PTUser, OTP codes, revoked refresh tokens, trusted devices.

Part of the apps.core.models package - see its __init__.py for the layout and
why it was split. Import from `apps.core.models`, not from this module.
"""

from django.db import models


# ── Auth: PTUser, OTPCode, TrustedDevice (device-aware 2FA) ────────────────
# Mirrors the TDS Automation App's own architecture (TDSUser/OTPCode/
# TrustedDevice in that app's apps/core/models/). See apps/api/
# auth_backend.py's module docstring for why AUTH_USER_MODEL stays Django's
# default and every real auth path resolves PTUser directly instead of
# get_user_model() - PTUser is not an AbstractBaseUser, it's a plain model
# exactly like TDSUser, for the same reason.
#
# Unlike TDSUser/OTPCode/TrustedDevice, these tables have no pre-Django
# history to work around - plain AutoField PKs, no managed=False baggage.

class PTUser(models.Model):
    """Application user, completely independent of Django's auth.User.

    Roles: 'admin' (full access, incl. user management) | 'editor' (full
    dashboard access, plus dismissing/overriding a flagged match and the
    inline "Edit Everywhere" field corrections - see CLAUDE.md's "Dismiss/
    override a flagged match" and "Inline 'Edit Everywhere'" sections) |
    'viewer' (read-only dashboard access - view POs, materials, sync
    status). password_hash is bcrypt (see
    apps/api/auth_backend.py's _verify_password) - never returned by any API
    response and excluded from the Django Admin form (PTUserAdmin).

    `plants` scopes which plants an admin may edit via the inline "Edit
    Everywhere" feature (apps/api/permissions.py's user_can_edit_plant()) -
    an empty list means "all plants" (deliberate default so every admin that
    existed before this field was added keeps full access with no data
    backfill), a non-empty list restricts to just those plant keys (the
    lowercase `hrs`/`achhad`/`vapi` keys from frontend/js/shared.js's PLANTS
    map, NOT SyncRun.Plant's uppercase enum - the two are unrelated). Has no
    bearing on read access - every role can still read every plant's
    dashboard, this only gates writes."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    user_id = models.AutoField(primary_key=True)
    email = models.TextField(unique=True)
    password_hash = models.TextField()
    full_name = models.TextField(null=True, blank=True)
    role = models.TextField(choices=Role.choices, default=Role.VIEWER)
    designation = models.TextField(null=True, blank=True)
    plants = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # Account lockout (added 2026-09-05, hardening pass) - the existing
    # LoginRateThrottle (apps/api/auth_views.py, 5/minute per email) is a
    # rate limit, not a lockout: it slows down guessing but never actually
    # stops it, forever, with no signal that an account is under sustained
    # attack. failed_login_attempts increments on each wrong password
    # (apps/api/auth_backend.py::PTUserBackend.authenticate()) and resets on
    # a successful login; locked_until is set 15 minutes into the future
    # once failed_login_attempts reaches 5, at which point login is refused
    # outright (even with the correct password) until it elapses.
    failed_login_attempts = models.PositiveSmallIntegerField(default=0)
    # Wrong device/password-change codes across EVERY code issued in the last
    # 24 hours (otp_service.verify_otp()). Each new code starts its own 5
    # tries, and signing in again issues a new code, so per code the
    # attempts were unbounded over time: about 25 guesses a minute for someone
    # holding the password, the 6-digit space in about 28 days. Capped at
    # otp_service._MAX_DAILY_FAILURES (2026-09-25).
    otp_failed_attempts = models.PositiveSmallIntegerField(default=0)
    otp_failures_since = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    # "Log out everywhere" (added 2026-09-05, hardening pass) - every JWT
    # issued for this user embeds the current value as a `ver` claim (see
    # apps/api/auth_serializers.py's PTTokenObtainPairSerializer.get_token()).
    # Both access-token auth (apps/api/auth_backend.py's
    # PTJWTAuthentication.get_user()) and refresh (PTTokenRefreshSerializer)
    # reject any token whose `ver` claim doesn't match the user's current
    # value. Incrementing this instantly invalidates every previously issued
    # access AND refresh token at once - a stronger, simpler guarantee than
    # RevokedRefreshToken alone provides (that table only knows about tokens
    # explicitly rotated-away or logged out, not every token ever issued;
    # this stamp needs no such registry, and covers live access tokens too,
    # which per-jti revocation never did). See
    # apps/services/token_revocation.py's revoke_all_sessions().
    token_version = models.PositiveIntegerField(default=0)

    # ── Django/DRF auth protocol ──────────────────────────────────────────
    # PTUser does NOT inherit from AbstractBaseUser, so these must be
    # declared explicitly - DRF's IsAuthenticated permission reads
    # request.user.is_authenticated and raises AttributeError without it.
    is_authenticated = True
    is_anonymous = False

    class Meta:
        db_table = "pt_users"

    def __str__(self):
        return f"{self.email} ({self.role})"


class OTPCode(models.Model):
    """One active email-OTP row per address (login 2FA on a new device).

    Security design (identical to the TDS app's OTPCode - see
    apps/services/otp_service.py): code_hash is a bcrypt hash, never the
    plaintext code; expires_at enforces a 10-minute TTL; attempts increments
    per wrong guess and the row is deleted at the cap; deleted on a
    successful verify (single-use); generate_otp() deletes any existing row
    for that email first (one active code at a time). `email` is unique at
    the DB level - generate_otp() wraps its delete-then-create in a single
    atomic, locked transaction so two concurrent requests for the same email
    can't both insert a row and make verify_otp()'s lookup ambiguous."""

    email = models.EmailField(unique=True)
    code_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "pt_otp_codes"

    def __str__(self):
        return f"OTP({self.email}, expires={self.expires_at})"


class RevokedRefreshToken(models.Model):
    """Custom refresh-token revocation list (added 2026-09-05, hardening
    pass) - deliberately NOT rest_framework_simplejwt's built-in
    `rest_framework_simplejwt.token_blacklist` app. That app's own
    OutstandingToken model FKs its `user` field to `AUTH_USER_MODEL`
    (Django's default `auth.User`), which this app never uses for real
    accounts - PTUser is a separate, unrelated model (see
    apps/api/auth_backend.py's module docstring on why AUTH_USER_MODEL
    stays at Django's default here). Confirmed incompatible the hard way:
    enabling that app crashed device_verify with "OutstandingToken.user
    must be a User instance" the moment a PTUser was passed to
    RefreshToken.for_user(). This table sidesteps that entirely by keyed
    only on the token's own `jti` claim - no user FK needed at all to check
    or record a revocation.

    Used for two things (see apps/api/auth_serializers.py's
    PTTokenRefreshSerializer and apps/api/routers/device_views.py's
    logout_view): (1) refresh-token rotation revokes the just-spent token's
    jti so it can't be replayed after a successful /api/auth/token/refresh,
    and (2) POST /api/auth/logout revokes the caller's current refresh
    token's jti directly, so a copy made before logout stops working
    immediately rather than surviving up to its full 30-day
    REFRESH_TOKEN_LIFETIME."""

    jti = models.CharField(max_length=255, unique=True)
    revoked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="Mirrors the token's own exp claim - lets a future cleanup "
                   "command purge rows whose token would have expired naturally "
                   "anyway. Not purged automatically yet.",
    )

    class Meta:
        db_table = "pt_revoked_refresh_tokens"
        indexes = [models.Index(fields=["jti"])]

    def __str__(self):
        return f"revoked jti={self.jti}"


class TrustedDevice(models.Model):
    """A verified device token for a PTUser (Instagram-style device trust -
    see apps/services/device_service.py). The real bearer credential is a
    64-char hex string (secrets.token_hex(32), 256-bit entropy), set in an
    httpOnly SameSite=Lax pt_device cookie on the browser - only its SHA-256
    hex digest (also 64 chars, hence no column-width change) is ever
    persisted here, in device_token_hash.

    Security fix, 2026-09-04: this column used to store the raw plaintext
    token directly (named device_token) - inconsistent with this same
    module's OTPCode.code_hash, which was already correctly hash-only for
    exactly this reason ("even direct DB access cannot reveal a valid
    code" - see apps/services/otp_service.py's module docstring). A device-
    trust token is a MORE valuable target than a 6-digit OTP (it bypasses
    the OTP step entirely, for up to DEVICE_COOKIE_MAX_AGE = 1 year), so a
    leaked DB backup or a compromised Postgres instance used to hand an
    attacker a ready-to-use, no-cracking-required 2FA bypass for every
    trusted device. Plain SHA-256 (not bcrypt) is the right hash here,
    unlike OTPCode - a 6-digit OTP has only 10^6 possible values and MUST
    use a slow hash to resist brute-forcing the hash itself; this token
    already has 2^256 possible values, so a fast hash is not brute-
    forceable regardless of speed, and a fast hash is required anyway since
    is_trusted_device() does an equality lookup (WHERE device_token_hash =
    ...) on every authenticated request - bcrypt has no equivalent
    "look up by hash" operation without checking every stored row.
    Migration 0015 renamed the column and hashed every existing row's
    already-known plaintext value in place - no forced re-verification for
    already-trusted devices.

    last_used_at is bumped on every successful is_trusted_device() check.
    Revocable via apps/api/routers/users_views.py's revoke_user_device()
    (Admin Panel > Edit User > Trusted Devices) - previously the admin
    "new device" alert email promised a revoke option here that did not
    yet exist; it does now."""

    user = models.ForeignKey(PTUser, on_delete=models.CASCADE, related_name="trusted_devices")
    device_token_hash = models.CharField(max_length=64, unique=True)
    device_name = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pt_trusted_devices"

    def __str__(self):
        return f"{self.device_name} ({self.user.email})"
