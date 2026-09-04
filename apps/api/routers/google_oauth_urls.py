"""
apps/api/routers/google_oauth_urls.py — URL routes for Google OAuth 2.0 login.

Included in apps/api/urls.py under the /api/ prefix.
"""

from django.urls import path

from . import google_oauth_views

urlpatterns = [
    path("auth/google/login/", google_oauth_views.google_login, name="google-login"),
    path("auth/google/callback/", google_oauth_views.google_callback, name="google-callback"),
    path("auth/google/session-token", google_oauth_views.oauth_session_token, name="google-session-token"),
]
