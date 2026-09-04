"""
apps/api/exceptions.py — Custom DRF exception handler.

Ported from the TDS Automation App's apps/api/exceptions.py (unchanged
design). Wired in via REST_FRAMEWORK['EXCEPTION_HANDLER'] in
config/settings.py, so every DRF view funnels its raised exceptions through
custom_exception_handler() below instead of DRF's default - one consistent
JSON error shape everywhere:

  { "detail": "human-readable message" }
or, for serializer validation errors:
  { "detail": { "field_name": ["error message", ...] } }
"""

import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.utils import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)

# Exception types this codebase's own service layer may raise deliberately
# with human-readable messages - safe to pass str(exc) straight through to
# the client instead of flattening it into the generic 500 message below.
# Audit any new addition for that before including it (none of these embed
# secrets, file paths, or raw SQL).
_DESCRIBABLE_EXCEPTIONS = (ValueError, KeyError, DjangoValidationError, ObjectDoesNotExist)


def _describe_integrity_error(exc):
    """Turn a raw django.db.utils.IntegrityError into a human-readable
    message without leaking the underlying SQL. Falls back to a generic
    message if the driver's diagnostic info isn't available."""
    diag = getattr(getattr(exc, "__cause__", None), "diag", None)
    constraint = getattr(diag, "constraint_name", None) or ""
    message_detail = (getattr(diag, "message_detail", None) or str(exc)).lower()

    if "unique" in constraint or "duplicate key" in message_detail:
        return "A record with this value already exists."
    if "fkey" in constraint or "foreign key" in message_detail:
        return "Referenced record no longer exists."
    if "not null" in message_detail or "not-null" in message_detail:
        return "A required field was left empty."
    return "Database constraint violation."


def custom_exception_handler(exc, context):
    """Called by DRF whenever a view raises an exception."""
    response = exception_handler(exc, context)

    if response is None:
        logger.exception("Unhandled exception in %s", context.get("view"))

        if isinstance(exc, _DESCRIBABLE_EXCEPTIONS):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(exc, IntegrityError):
            return Response({"detail": _describe_integrity_error(exc)}, status=status.HTTP_400_BAD_REQUEST)

        detail = "An unexpected server error occurred."
        if settings.DEBUG:
            detail = f"{detail} ({type(exc).__name__}: {exc})"
        return Response({"detail": detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if isinstance(response.data, dict) and "detail" not in response.data:
        response.data = {"detail": response.data}

    return response
