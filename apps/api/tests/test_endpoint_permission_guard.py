"""
Source guard: every API endpoint states its access decision out loud.

Same idea as test_local_date_timezone.py's source scan - make the rule
structural rather than a discipline each new endpoint has to remember.
Added 2026-09-23 (audit pass) after the review router grew from two to five
endpoints with no plant scoping on any of them, despite CLAUDE.md's standing
instruction: "If you add a new read OR write endpoint, gate both role and
plant". Every other router already did this by hand; nothing checked it.

Three rules, all read from the AST of every function decorated with
@api_view under apps/api/:

1. An endpoint accepting POST/PUT/PATCH/DELETE declares @permission_classes.
   The project default (IsAuthenticated) lets ANY role write, so leaving the
   decorator off is a decision too - it just used to be an invisible one.
   Declaring AllowAny explicitly (the cron triggers) satisfies this: the
   point is that the choice is written down, not what it is.

2. An endpoint that handles a plant - takes a `plant` argument, reads a
   "plant" field, or is built from a per-plant `cfg` - calls one of the plant
   scoping helpers in its body.

3. (2026-10-02, layered access) Every endpoint, reads included, is gated by
   a permission: requires(...), IsAdmin or HasAnyAccess. Bare
   IsAuthenticated, or no decorator at all, lets a LOCKED account - signed
   in, no permissions granted - through, so it is allowed only for the
   sign-in, self-service and owner-only views listed with their reasons.

An endpoint that is right to break a rule goes in the matching allow-list
below WITH ITS REASON. Stale entries fail too, so the lists cannot quietly
accumulate names of views that no longer exist.

This is a tripwire, not a proof: it cannot tell whether a scoping call is in
the right place, only that the author thought about it. That is the failure
it exists to catch.
"""

import ast
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PLANT_SCOPE_CALLS = {"user_can_access_plant"}
PERMISSION_GATES = {"requires", "IsAdmin", "HasAnyAccess"}

# Rule 1 exceptions: unsafe method, deliberately no @permission_classes.
WRITES_OPEN_TO_ANY_ROLE = {
    ("device_views.py", "logout_everywhere_view"):
        "Self-service 'log out everywhere' - every account must be able to revoke its own sessions.",
}

# Rule 2 exceptions: handles a plant, deliberately no scoping call.
PLANT_ENDPOINTS_NOT_SCOPED = {
    ("_domestic_base.py", "sync_trigger"):
        "IsAdmin, returns only a status (no plant data), and admins are who set plant scope in the first place.",
    ("imports_views.py", "sync_trigger"):
        "Same as the domestic sync_trigger: IsAdmin, status-only response, re-reads that plant's own Drive CSV.",
    ("views.py", "readiness"):
        "Unauthenticated uptime probe; loops over plants only to name stale pipeline steps - no plant data returned.",
    ("mir_views.py", "vendors"):
        "Vendor master is company-wide, not plant data; MIR entry picker for a PO that names no vendor.",
}

# Rule 3 exceptions: deliberately not gated by a permission.
NOT_PERMISSION_GATED = {
    ("auth_views.py", "whoami"):
        "Tells the page who is signed in and what they hold - a locked account must reach it to see 'no access'.",
    ("device_views.py", "device_verify"): "Sign-in step, before any account is known.",
    ("device_views.py", "logout_view"): "Signing out must always work.",
    ("device_views.py", "logout_everywhere_view"): "Self-service: revoking your own sessions.",
    ("google_oauth_views.py", "oauth_session_token"): "Sign-in step, before any account is known.",
    ("password_views.py", "request_password_change"): "Self-service: changing your own password.",
    ("password_views.py", "confirm_password_change"): "Self-service: changing your own password.",
    ("password_views.py", "request_password_reset"): "Forgot password on the sign-in page, before any account is known.",
    ("password_views.py", "confirm_password_reset"): "Forgot password on the sign-in page, before any account is known.",
    ("preferences_views.py", "presets"): "Self-service: the caller's own saved sort presets.",
    ("preferences_views.py", "preset"): "Self-service: the caller's own saved sort presets.",
    ("activity_views.py", "page_view"): "Records the caller's own page visit; returns nothing.",
    ("activity_views.py", "activity"): "IsActivityLogOwner - narrower than any permission.",
    ("activity_views.py", "activity_people"): "IsActivityLogOwner - narrower than any permission.",
    ("activity_views.py", "activity_export"): "IsActivityLogOwner - narrower than any permission.",
    ("reports_views.py", "trigger_daily_report"): "Cron trigger, shared-secret checked in the body.",
    ("reports_views.py", "trigger_monthly_report"): "Cron trigger, shared-secret checked in the body.",
    ("reports_views.py", "trigger_mismatch_report"): "Cron trigger, shared-secret checked in the body.",
    ("reports_views.py", "trigger_prune_revoked_tokens"): "Cron trigger, shared-secret checked in the body.",
    ("reports_views.py", "trigger_advance_license_expiry_report"): "Cron trigger, shared-secret checked in the body.",
    ("views.py", "readiness"): "Unauthenticated uptime probe; returns no business data.",
}


def _decorator_name(node):
    func = node.func if isinstance(node, ast.Call) else node
    return getattr(func, "id", None) or getattr(func, "attr", None)


def _endpoints():
    """(file name, function name, methods, function node) for every
    @api_view function in apps/api, including the ones nested inside
    _domestic_base.py's make_* factories."""
    files = sorted(API_DIR.glob("routers/*.py")) + sorted(API_DIR.glob("*views*.py"))
    found = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            api_view = [d for d in fn.decorator_list if _decorator_name(d) == "api_view"]
            if not api_view:
                continue
            args = api_view[0].args if isinstance(api_view[0], ast.Call) else []
            methods = {elt.value for elt in args[0].elts} if args else {"GET"}
            found.append((path.name, fn.name, methods, fn))
    return found


def _handles_a_plant(fn) -> bool:
    """A `plant` argument, a "plant" field read, a `plant` variable (e.g.
    looping over every plant's config - exactly what the unscoped
    next_review did), or a per-plant factory's `cfg`."""
    source = ast.unparse(fn)
    return (
        "plant" in {a.arg for a in fn.args.args}
        or any(isinstance(n, ast.Name) and n.id == "plant" for n in ast.walk(fn))
        or '"plant"' in source
        or "cfg." in source
    )


def _calls(fn) -> set:
    return {_decorator_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def test_the_scanner_actually_finds_the_endpoints():
    """A scanner that silently finds nothing would pass every rule below.
    The app has well over fifty endpoints; if this drops, the AST walk broke,
    not the app."""
    endpoints = _endpoints()
    assert len(endpoints) > 50
    names = {(f, n) for f, n, _, _ in endpoints}
    assert ("_domestic_base.py", "correct_field") in names  # nested in a make_* factory
    assert ("mir_views.py", "post_entry") in names


_PRE_FIX_REVIEW_ROUTER = '''
@api_view(["GET"])
def next_review(request):
    for match_type, plant in groups:
        picked.append(_pick_one(match_type, plant, set()))
    return Response(picked)

@api_view(["POST"])
def submit_review(request):
    plant = request.data.get("plant")
    return Response(MatchReview.objects.create(plant=plant))
'''


def test_the_rules_flag_the_review_router_as_it_was_before_the_fix():
    """Proves both rules detect the real gap they were written for, using a
    reduced copy of the review router as it stood before 2026-09-23 - rather
    than by reverting the live view to watch the guard go red."""
    fns = {fn.name: fn for fn in ast.walk(ast.parse(_PRE_FIX_REVIEW_ROUTER)) if isinstance(fn, ast.FunctionDef)}

    for name in ("next_review", "submit_review"):
        assert _handles_a_plant(fns[name]), f"{name} handles a plant and must be recognised as doing so"
        assert not (_calls(fns[name]) & PLANT_SCOPE_CALLS), f"{name} has no scoping call - rule 2 must flag it"
    assert not any(_decorator_name(d) == "permission_classes" for d in fns["submit_review"].decorator_list)


def test_every_write_endpoint_declares_its_permission_classes():
    offenders = [
        f"{f}::{n} {sorted(methods & UNSAFE_METHODS)}"
        for f, n, methods, fn in _endpoints()
        if methods & UNSAFE_METHODS
        and not any(_decorator_name(d) == "permission_classes" for d in fn.decorator_list)
        and (f, n) not in WRITES_OPEN_TO_ANY_ROLE
    ]
    assert not offenders, (
        "These endpoints accept writes with no @permission_classes, so ANY signed-in account can call them. "
        "Add requires(Perm...)/IsAdmin, or - if open-to-every-account is genuinely right - add the view to "
        "WRITES_OPEN_TO_ANY_ROLE with the reason:\n  " + "\n  ".join(offenders)
    )


def test_every_plant_endpoint_calls_a_plant_scoping_helper():
    offenders = [
        f"{f}::{n}"
        for f, n, _, fn in _endpoints()
        if _handles_a_plant(fn)
        and not (_calls(fn) & PLANT_SCOPE_CALLS)
        and (f, n) not in PLANT_ENDPOINTS_NOT_SCOPED
    ]
    assert not offenders, (
        "These endpoints handle a plant but never check PTUser.plants, so an account scoped to one plant can "
        "reach another's data. Call user_can_access_plant(), or add the view to "
        "PLANT_ENDPOINTS_NOT_SCOPED with the reason:\n  " + "\n  ".join(offenders)
    )


def _gates(fn) -> set:
    """Names used inside the endpoint's @permission_classes([...])."""
    found = set()
    for d in fn.decorator_list:
        if _decorator_name(d) == "permission_classes":
            found |= {getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(d)}
    return found


def test_the_permission_rule_flags_a_bare_isauthenticated_read():
    """Rule 3 must catch the shape the layered-access rework replaced: a read
    open to any signed-in account, locked ones included."""
    fn = ast.parse('''
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def materials(request):
    return Response([])
''').body[0]
    assert not (_gates(fn) & PERMISSION_GATES)


def test_every_endpoint_is_gated_by_a_permission():
    offenders = [
        f"{f}::{n}"
        for f, n, _, fn in _endpoints()
        if not (_gates(fn) & PERMISSION_GATES) and (f, n) not in NOT_PERMISSION_GATED
    ]
    assert not offenders, (
        "These endpoints are open to any signed-in account, including one with no permissions granted. "
        "Gate them with requires(Perm...), IsAdmin or HasAnyAccess, or - for a sign-in or self-service "
        "view - add the view to NOT_PERMISSION_GATED with the reason:\n  " + "\n  ".join(offenders)
    )


def test_allow_lists_name_only_endpoints_that_still_exist():
    names = {(f, n) for f, n, _, _ in _endpoints()}
    stale = sorted((set(WRITES_OPEN_TO_ANY_ROLE) | set(PLANT_ENDPOINTS_NOT_SCOPED) | set(NOT_PERMISSION_GATED)) - names)
    assert not stale, f"Allow-list entries for endpoints that no longer exist - delete them: {stale}"
