"""
apps/api/routers/google_oauth_views.py — Google OAuth 2.0 login.

Ported from the TDS Automation App's apps/api/routers/google_oauth_views.py.

Endpoints
---------
GET /api/auth/google/login/
    Redirects the browser to Google's consent screen.

GET /api/auth/google/callback/
    Google redirects here with ?code=...&state=...
    Verifies state, exchanges code, reads email, looks up PTUser, then
    applies the device-aware 2FA gate:
      - Trusted device  -> redirect to / with full JWT delivered via session
      - New device      -> store pending_user_id in session, redirect to
                            /login.html?step=device_verify

Design decisions
----------------
- No auto-registration: the email MUST already exist in the PTUser table.
  If not found, the user is redirected to the login page with an error param.
- CSRF protection: the `state` parameter is stored in the Django session and
  verified on callback.
- PKCE: google-auth-oauthlib auto-generates a code_verifier in google_login.
  It's saved to the session and restored in google_callback before
  fetch_token() - otherwise Google returns "Missing code verifier".
- Device-aware 2FA: same device trust logic as email/password login.
- Plain Django views (NOT DRF @api_view): DRF wraps the request object and
  interferes with session saves before HttpResponseRedirect.
"""
import logging
import os

import requests as http_requests
from django.conf import settings
from django.http import HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from google_auth_oauthlib.flow import Flow
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.api.permissions import is_allowed_email_domain
from apps.core.models import PTUser
from apps.services.device_service import is_trusted_device

# Allow google-auth-oauthlib to relax scope validation (Google sometimes
# returns full-URI scopes instead of short names)
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

log = logging.getLogger(__name__)

_SCOPES = ["openid", "email", "profile"]
_FRONTEND_LOGIN = "/login.html"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
            }
        },
        scopes=_SCOPES,
    )


# ── OAuth login/callback/session-token endpoints ────────────────────────────────

def google_login(request):
    """Redirect the browser to Google's OAuth consent screen. Stores the
    OAuth `state` AND the PKCE `code_verifier` in the Django session for use
    in google_callback."""
    flow = _make_flow()
    flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account",
    )

    request.session["google_oauth_state"] = state
    request.session["google_oauth_code_verifier"] = flow.code_verifier
    request.session.modified = True
    request.session.save()  # force-save before the browser leaves our domain
    log.info("Google OAuth: redirecting to consent screen")
    return HttpResponseRedirect(auth_url)


@csrf_exempt
def google_callback(request):
    """Handle the OAuth callback from Google."""
    if request.GET.get("error"):
        log.warning("Google OAuth: user denied access (%s)", request.GET.get("error"))
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=cancelled")

    code = request.GET.get("code", "")
    state = request.GET.get("state", "")

    expected_state = request.session.pop("google_oauth_state", None)
    if not expected_state or state != expected_state:
        log.warning("Google OAuth: state mismatch - possible CSRF attack")
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=state_mismatch")

    code_verifier = request.session.pop("google_oauth_code_verifier", None)

    try:
        flow = _make_flow()
        flow.redirect_uri = settings.GOOGLE_OAUTH_REDIRECT_URI
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        credentials = flow.credentials
    except Exception as exc:
        log.error("Google OAuth token exchange failed: %s", exc, exc_info=True)
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=token_failed")

    try:
        resp = http_requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=10,
        )
        resp.raise_for_status()
        userinfo = resp.json()
        email = userinfo.get("email", "").lower().strip()
        email_verified = userinfo.get("email_verified", False)
    except Exception as exc:
        log.error("Google OAuth userinfo fetch failed: %s", exc, exc_info=True)
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=userinfo_failed")

    if not email or not email_verified:
        log.warning("Google OAuth: email missing or not verified")
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=unverified_email")

    if not is_allowed_email_domain(email):
        log.warning("Google OAuth: email %s is outside the allowed domain", email)
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=domain_not_allowed")

    try:
        user = PTUser.objects.get(email=email, is_active=True)
    except PTUser.DoesNotExist:
        log.warning("Google OAuth: email %s not registered or inactive", email)
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=not_registered")

    log.info("Google OAuth: %s authenticated, checking device trust", email)

    if is_trusted_device(request, user.user_id):
        from apps.api.auth_serializers import PTTokenObtainPairSerializer

        refresh = PTTokenObtainPairSerializer.get_token(user)
        access = str(refresh.access_token)
        log.info("Google OAuth: trusted device for user_id=%s - issuing JWT", user.user_id)

        # The JWT is handed off through the server-side session (never as a
        # URL query param - that would land in browser history, proxy/access
        # logs, and Referer headers). The frontend collects it via a
        # one-time GET to /api/auth/google/session-token, which pops it from
        # the session so it can't be replayed.
        request.session["oauth_delivery"] = {
            "access_token": access,
            "user_id": user.user_id,
            "role": user.role,
            "full_name": user.full_name or "",
            "email": user.email,
        }
        request.session.modified = True
        request.session.save()

        redirect_response = HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_ready=1")
        from apps.services.device_service import set_access_cookie, set_refresh_cookie

        set_access_cookie(redirect_response, access)
        set_refresh_cookie(redirect_response, str(refresh))

        from apps.core.audit_log import PTAuditLog, log_pt_action

        log_pt_action(request, PTAuditLog.ACTION_LOGIN, actor=user, detail="Google OAuth, trusted device")

        return redirect_response
    else:
        from apps.services.device_service import send_device_otp

        try:
            send_device_otp(user)
        except Exception:
            log.error("Google OAuth: failed to send OTP to user_id=%s", user.user_id, exc_info=True)
            return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?oauth_error=email_failed")

        request.session["pending_user_id"] = user.user_id
        request.session.modified = True

        log.info("Google OAuth: new device for user_id=%s - OTP sent", user.user_id)
        return HttpResponseRedirect(f"{_FRONTEND_LOGIN}?step=device_verify")


@api_view(["GET"])
@permission_classes([AllowAny])
def oauth_session_token(request):
    """GET /api/auth/google/session-token

    One-time pickup for the JWT issued by a trusted-device Google OAuth
    login (see google_callback above) - stashed server-side in the Django
    session rather than in the redirect URL. Pops the value so it can't be
    fetched twice."""
    data = request.session.pop("oauth_delivery", None)
    request.session.modified = True
    if not data:
        return Response({"detail": "No pending OAuth session found. Please sign in again."}, status=400)
    return Response(data)
