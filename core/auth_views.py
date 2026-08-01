"""
Google Sign-In only - there is deliberately no local password anywhere in
this app, so there's nothing to leak or reuse. 2FA itself is NOT implemented
here; it's inherited from whatever your Google Workspace admin console has
enforced under Security > 2-Step Verification for ravasco.com - confirm
that's actually turned on, since "Google login" alone doesn't guarantee it.

The `hd` hint below only affects which accounts Google's account picker
shows - it is NOT a real restriction. The actual enforcement is the
`email.endswith(...)` check in the callback, which happens no matter what
the client sent.
"""
import json
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET, require_POST
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow

from .models import AuditLog, UserAccess

_CLIENT_CONFIG_TEMPLATE = {
    "web": {
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def _build_flow(request):
    config = dict(_CLIENT_CONFIG_TEMPLATE)
    config["web"].update(
        {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uris": [settings.GOOGLE_OAUTH_REDIRECT_URI],
        }
    )
    flow = Flow.from_client_config(
        config,
        scopes=["openid", "https://www.googleapis.com/auth/userinfo.email",
                 "https://www.googleapis.com/auth/userinfo.profile"],
        redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
    )
    return flow


@require_GET
def login_start(request):
    flow = _build_flow(request)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    auth_url, _ = flow.authorization_url(
        access_type="online",
        include_granted_scopes="true",
        state=state,
        hd=settings.ALLOWED_LOGIN_DOMAIN,  # UI hint only, not a real restriction
        prompt="select_account",
    )
    return redirect(auth_url)


@require_GET
def login_callback(request):
    if request.GET.get("state") != request.session.get("oauth_state"):
        return JsonResponse({"error": "Invalid OAuth state."}, status=400)

    flow = _build_flow(request)
    flow.fetch_token(code=request.GET.get("code"))
    credentials = flow.credentials

    # Verify the ID token server-side - never trust the client, never trust
    # the `hd` hint alone.
    info = google_id_token.verify_oauth2_token(
        credentials.id_token, __import__("google.auth.transport.requests", fromlist=["Request"]).Request(),
        settings.GOOGLE_OAUTH_CLIENT_ID,
    )
    email = (info.get("email") or "").lower()
    email_verified = info.get("email_verified", False)

    if not email_verified or not email.endswith("@" + settings.ALLOWED_LOGIN_DOMAIN):
        AuditLog.objects.create(email=email or "unknown", action="login_rejected_domain")
        return JsonResponse(
            {"error": f"This app is restricted to @{settings.ALLOWED_LOGIN_DOMAIN} accounts."}, status=403
        )

    request.session["email"] = email
    request.session["name"] = info.get("name", email)
    AuditLog.objects.create(email=email, action="login_success")
    return redirect("/")


@require_POST
def logout(request):
    email = request.session.get("email")
    if email:
        AuditLog.objects.create(email=email, action="logout")
    request.session.flush()
    return JsonResponse({"ok": True})


@require_GET
def me(request):
    email = request.session.get("email")
    if not email:
        return JsonResponse({"error": "Not signed in."}, status=401)
    try:
        access = UserAccess.objects.get(email=email, is_active=True)
    except UserAccess.DoesNotExist:
        return JsonResponse({"email": email, "role": "none", "plants": []})
    return JsonResponse({"email": email, "role": access.role, "plants": access.allowed_plants()})
