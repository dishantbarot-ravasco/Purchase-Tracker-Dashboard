"""
apps/api/permissions.py - Who may do what (layered access, 2026-10-02).

Three layers, decided by the project owner on 2026-10-02:

  1. Admin (role "admin") - every page, every plant, every action, and the
     only role that sees Raw Material Analysis. The owner
     (settings.OWNER_EMAIL) is the one admin who may make or unmake an admin
     and change another admin's account (is_owner()).
  2. Plants - every other account ("user") reads and works on exactly the
     plants in PTUser.plants. An empty list means NO plants, never "all".
  3. Permissions - a user account also needs each page or action granted in
     PTUser.permissions (Perm below). A user with none is locked: signed in,
     sees nothing. Grants apply to all of the user's plants.

Every endpoint names the permissions it needs with
@permission_classes([requires(Perm.X, ...)]) - any one of them is enough,
an admin always passes - and calls user_can_access_plant() for the plant
it touches. test_endpoint_permission_guard.py fails an endpoint that does
neither.
"""

from django.conf import settings
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission
from rest_framework.throttling import UserRateThrottle


def is_allowed_email_domain(email: str) -> bool:
    """True only for an email whose domain is exactly one of
    settings.ALLOWED_EMAIL_DOMAINS (case-insensitive) - "x@ravasco.com" yes,
    "x@evilravasco.com" and "x@ravasco.com.evil.io" no. Gates account
    creation and login so no address outside the company domains can ever
    have or use a PTUser account."""
    local, sep, domain = (email or "").strip().lower().rpartition("@")
    return bool(sep and local) and domain in settings.ALLOWED_EMAIL_DOMAINS


def allowed_domains_text() -> str:
    """"@ravasco.com or @hindustanrubbers.com" - for refusal messages."""
    return " or ".join("@" + d for d in settings.ALLOWED_EMAIL_DOMAINS)


# The plant keys PTUser.plants holds - frontend/js/shared.js's PLANTS keys,
# not SyncRun.Plant's uppercase enum.
PLANT_KEYS = ("hrs", "achhad", "vapi")


class Perm:
    """The grantable permissions (PTUser.permissions holds their values).
    Views decide what a user can see; work permissions what they can do."""

    VIEW_DASHBOARD = "view_dashboard"        # Purchase Orders, Import Purchases, Search PO, licences
    VIEW_INVENTORY = "view_inventory"        # the Inventory tab
    VIEW_ON_ORDER = "view_on_order"          # the On Order tab
    VIEW_STOCK_ORDERS = "view_stock_orders"  # the Stock & Orders tab
    PO_UPLOAD = "po_upload"                  # PO files; PO line close / reopen / review
    MIR_ENTRY = "mir_entry"                  # MIR entry, its invoice files, MIR edits and cancels
    IMPORT_DOCS = "import_docs"              # Bill of Entry, Advance License and RoDTEP files
    RM_STORE = "rm_store"                    # RM store issues, returns and stock differences
    EDIT_FIELDS = "edit_fields"              # inline corrections, MIR pins, flag and match dismissals


# Order and labels as the Admin Panel lists them.
PERMISSIONS = (
    (Perm.VIEW_DASHBOARD, "View", "PO Dashboard"),
    (Perm.VIEW_INVENTORY, "View", "Inventory"),
    (Perm.VIEW_ON_ORDER, "View", "On Order"),
    (Perm.VIEW_STOCK_ORDERS, "View", "Stock & Orders"),
    (Perm.PO_UPLOAD, "Work", "PO upload"),
    (Perm.MIR_ENTRY, "Work", "MIR entry"),
    (Perm.IMPORT_DOCS, "Work", "Import docs"),
    (Perm.RM_STORE, "Work", "RM store"),
    (Perm.EDIT_FIELDS, "Work", "Edit fields"),
)
ALL_PERMISSIONS = tuple(p for p, _, _ in PERMISSIONS)

# The three plant stock tabs read the same stock and order data.
STOCK_VIEWS = (Perm.VIEW_INVENTORY, Perm.VIEW_ON_ORDER, Perm.VIEW_STOCK_ORDERS)
# Purchase order lists: the dashboard and the two tabs that show orders.
ORDER_VIEWS = (Perm.VIEW_DASHBOARD, Perm.VIEW_ON_ORDER, Perm.VIEW_STOCK_ORDERS)


def _active(user) -> bool:
    return user is not None and bool(getattr(user, "is_authenticated", False)) and bool(getattr(user, "is_active", False))


def is_admin(user) -> bool:
    return _active(user) and getattr(user, "role", None) == "admin"


def is_owner(user) -> bool:
    """The application's owner (settings.OWNER_EMAIL), who must also be an
    active admin. Only the owner makes or unmakes an admin."""
    return is_admin(user) and (getattr(user, "email", "") or "").strip().lower() == settings.OWNER_EMAIL


def granted(user) -> set:
    """The permissions this account holds - every one for an admin, none for
    an inactive account."""
    if not _active(user):
        return set()
    if is_admin(user):
        return set(ALL_PERMISSIONS)
    return {p for p in (getattr(user, "permissions", None) or []) if p in ALL_PERMISSIONS}


def has_perm(user, *perms) -> bool:
    """True when the account holds at least one of `perms`."""
    held = granted(user)
    return any(p in held for p in perms)


def requires(*perms):
    """A DRF permission class passing an admin, or a user holding any one of
    `perms`. A user with no plants is refused too: they can read nothing."""
    if not perms or any(p not in ALL_PERMISSIONS for p in perms):
        raise ValueError(f"requires() needs known permissions, got {perms!r}")

    class _Requires(BasePermission):
        message = "You do not have access to this. Ask an admin to grant it."
        needed = perms

        def has_permission(self, request, view):
            user = request.user
            if is_admin(user):
                return True
            return has_perm(user, *perms) and bool(getattr(user, "plants", None))

    _Requires.__name__ = "Requires_" + "_or_".join(perms)
    return _Requires


class HasAnyAccess(BasePermission):
    """Any account that can see something: an admin, or a user with at least
    one permission and one plant. For read endpoints every page shares (the
    sync status the freshness watcher polls)."""

    message = "You do not have access to this. Ask an admin to grant it."

    def has_permission(self, request, view):
        user = request.user
        return is_admin(user) or (bool(granted(user)) and bool(getattr(user, "plants", None)))


class IsAdmin(BasePermission):
    """Allows access only to users with role 'admin'."""

    message = "Admin role required."

    def has_permission(self, request, view):
        user = request.user
        return (
            user is not None
            and bool(getattr(user, "is_active", False))
            and getattr(user, "role", None) == "admin"
        )


def is_activity_log_owner(user) -> bool:
    """True only for the active account named by
    settings.ACTIVITY_LOG_OWNER_EMAIL - the activity log is private to it."""
    return (
        user is not None
        and bool(getattr(user, "is_authenticated", False))
        and bool(getattr(user, "is_active", False))
        and (getattr(user, "email", "") or "").strip().lower() == settings.ACTIVITY_LOG_OWNER_EMAIL
    )


class IsActivityLogOwner(BasePermission):
    """The activity log's reads. Anyone else gets a 404, not a 403, so the
    log's existence is not confirmed to other admins either."""

    def has_permission(self, request, view):
        if not is_activity_log_owner(request.user):
            raise NotFound()
        return True


class SyncTriggerThrottle(UserRateThrottle):
    """Stricter than the generic 200/min "user" bucket - each request queues
    a real background Drive-sync job (apps/services/sync_trigger.py), so an
    IsAdmin-gated but buggy/compromised client hammering this endpoint is a
    materially worse resource-exhaustion vector than an ordinary field
    correction hitting the same generic limit. Same "distinct scope, keyed
    the same way UserRateThrottle already keys by user pk" pattern
    auth_views.py's LoginRateThrottle uses for its own reason (per-account,
    not per-IP) - see config/settings.py's DEFAULT_THROTTLE_RATES for the
    actual "sync_trigger" rate."""

    scope = "sync_trigger"


class BlTrackThrottle(UserRateThrottle):
    """The BL "Track" lookup (imports_views.track_bl) makes a synchronous
    SafeCube call that can hold a gunicorn worker for its whole timeout, and
    production runs two sync workers - so a few repeated clicks could leave
    the app unresponsive. See DEFAULT_THROTTLE_RATES' "bl_track"."""

    scope = "bl_track"


class AdminWriteThrottle(UserRateThrottle):
    """Stricter than the generic 200/min "user" bucket for admin-only writes
    that create/modify accounts (apps/api/routers/users_views.py) - see
    config/settings.py's DEFAULT_THROTTLE_RATES for the actual
    "admin_write" rate."""

    scope = "admin_write"


def user_can_access_plant(user, plant_key: str) -> bool:
    """True if `user` may read or work on `plant_key` ("hrs"/"achhad"/"vapi"
    - the lowercase frontend/js/shared.js keys, not SyncRun.Plant's enum).
    An admin reaches every plant; a user only the plants in PTUser.plants,
    and an empty list means none (2026-10-02 - it used to mean all).

    This is the plant layer only. The endpoint's @permission_classes decides
    whether the account may use it at all (requires()). Not a DRF permission
    class because the plant usually is not known until inside the view (a
    URL kwarg, a body field, or a per-router constant) - call it from the
    view body and return a 403, or filter the results, on False."""
    if is_admin(user):
        return True
    if not _active(user):
        return False
    plants = getattr(user, "plants", None) or []
    return plant_key in plants
