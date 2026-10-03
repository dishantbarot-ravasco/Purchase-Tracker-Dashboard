"""
apps/api/exceptions.py - Custom DRF exception handler.

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
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import IntegrityError, ProgrammingError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)

# Exception types this codebase's own service layer may raise deliberately
# with human-readable messages - passed through to the client as a 400
# instead of the generic 500 below. Django's ValidationError always shows its
# message (written for users by design); ValueError and ObjectDoesNotExist
# only when raised by this app's own code (_raised_by_own_code()).
# Audit any new addition for that before including it (none of these embed
# secrets, file paths, or raw SQL).
_DESCRIBABLE_EXCEPTIONS = (ValueError, DjangoValidationError, ObjectDoesNotExist)

# KeyError was REMOVED from the tuple above (2026-09-15, audit pass). It is
# the one entry that is almost never raised deliberately with a message meant
# for a user: in practice it comes from an internal dict lookup missing a key,
# and `str(KeyError("_internal_plant_cfg"))` renders as the bare key name. That
# handed a caller a fragment of internal data-structure naming AND, worse,
# reported a genuine server-side bug to the client as a 400 - telling the user
# they sent something wrong when they did not, and mis-classifying the failure
# for anyone reading response-code metrics.
#
# A KeyError now falls through to the generic 500 branch, which is the honest
# classification. It is still logged with a full traceback by
# logger.exception() below and still reaches Sentry via the LoggingIntegration
# (config/settings.py, event_level=ERROR), so nothing is lost operationally -
# only the leak to the client goes away. If a service-layer caller genuinely
# wants to describe a missing key to a user, raise ValueError with a written
# message, the way every other deliberate case here already does.


# Where this app's own code lives. A ValueError or DoesNotExist is shown to
# the user only when it was raised here (2026-10-03): ours carry written
# messages, while one raised inside Django or a library carries that
# library's wording - "PTUser matching query does not exist." names a model,
# a parser error can quote internals. Those get _NOT_DESCRIBED instead. A
# builtin called from our code (int(), Decimal()) counts as ours, since the
# innermost Python frame is our line; its message names only the bad value.
_OWN_CODE = (Path(settings.BASE_DIR) / "apps").resolve()
_NOT_DESCRIBED = "That request could not be processed. Check the values entered and try again."


def _raised_by_own_code(exc) -> bool:
    tb = exc.__traceback__
    if tb is None:  # never raised (built and handed over directly): nothing to hide
        return True
    while tb.tb_next is not None:
        tb = tb.tb_next
    try:
        return Path(tb.tb_frame.f_code.co_filename).resolve().is_relative_to(_OWN_CODE)
    except (OSError, ValueError):
        return False


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


def _unapplied_migrations():
    """Names of migrations the code has and the database has not run, e.g.
    ["core.0085_material_base_unit"]. Empty when it cannot tell."""
    try:
        executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:  # only a hint on an error already being reported
        return []
    return [f"{m.app_label}.{m.name}" for m, _backwards in plan]


def custom_exception_handler(exc, context):
    """Called by DRF whenever a view raises an exception."""
    response = exception_handler(exc, context)

    if response is None:
        logger.exception("Unhandled exception in %s", context.get("view"))

        if isinstance(exc, DjangoValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, _DESCRIBABLE_EXCEPTIONS):
            detail = str(exc) if _raised_by_own_code(exc) else _NOT_DESCRIBED
            return Response({"detail": detail}, status=status.HTTP_400_BAD_REQUEST)

        if isinstance(exc, IntegrityError):
            return Response({"detail": _describe_integrity_error(exc)}, status=status.HTTP_400_BAD_REQUEST)

        detail = "An unexpected server error occurred."
        if settings.DEBUG:
            detail = f"{detail} ({type(exc).__name__}: {exc})"
            # A pulled change whose migration was not run yet: every query on
            # the changed table fails with "column ... does not exist". Say so.
            pending = _unapplied_migrations() if isinstance(exc, ProgrammingError) else []
            if pending:
                detail = (f"The database is behind the code - {len(pending)} migration(s) not applied "
                          f"({', '.join(pending[:3])}). Run: uv run python manage.py migrate. {detail}")
        return Response({"detail": detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    if isinstance(response.data, dict) and "detail" not in response.data:
        response.data = {"detail": response.data}

    return response
