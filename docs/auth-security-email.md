# Authentication, authorization, security and outgoing email

This doc covers how people sign in, what each role may do, the security middleware and deploy
checks, and every email the app sends. The auth stack is a deliberate port of the TDS Automation
App's (device-aware 2FA, JWT in httpOnly cookies, role permissions), adapted to a user model,
`PTUser`, that is **not** a Django `auth.User`. Read [Non-negotiables](#non-negotiables) before
touching anything here. Related docs: [architecture.md](architecture.md) (layering, middleware
order in context), [api-and-features.md](api-and-features.md) (user-management UI, per-endpoint
behaviour), [consumption.md](consumption.md) (what the consumption report emails contain),
[testing-deployment.md](testing-deployment.md) (CI gates, Render, `.env`).

**The login flow in one pass:**

1. `POST /api/auth/login` with email + password. `PTUserBackend` checks lockout, then bcrypt
   ([auth_backend.py](../apps/api/auth_backend.py)).
2. **Trusted device** (a `pt_device` cookie whose SHA-256 matches a `TrustedDevice` row for that
   user): the response sets `pt_access` (12h) and `pt_refresh` (30 days) httpOnly cookies and returns
   `{"status": "ok", "access_token": ...}`. Done.
3. **New device**: the server stores `pending_user_id` in the Django session, emails a 6-digit OTP
   and returns `{"status": "device_verify"}`. The browser then posts the code to
   `POST /api/auth/device-verify`, which creates a `TrustedDevice`, sets `pt_device` (1 year) plus
   the two JWT cookies, and sends two informational emails.
4. Google sign-in (`/api/auth/google/login/` -> `/callback/`) applies the same device gate.
5. Every later API call authenticates from the `pt_access` cookie (or an `Authorization: Bearer`
   header). On a 401 the frontend silently calls `POST /api/auth/token/refresh`, which rotates the
   refresh token and re-cookies both.

---

## Auth & security

Ported from the TDS Automation App's auth stack: device-aware 2FA (email OTP on a new device, cookie
device-trust thereafter), JWT-in-httpOnly-cookie auth with a Bearer-header fallback for non-browser
clients, role-based permissions, the `core`/`api`/`services` layering, DB-backed cache with a
test-mode override, hardened security headers, admin-only CSRF, a shared DRF exception handler, and
CI.

**Deliberately NOT ported** - don't "fix" these back without re-deciding:

- **No `django-cors-headers`.** This frontend is same-origin (WhiteNoise serves it from the same
  process), so there is no cross-origin case CORS would solve.
- **`uv`/`pyproject.toml`, not `pip`/`requirements.txt`.** CI's `pip-audit` runs against the
  `uv`-managed venv directly.
- **`pytest`/`pytest-django`, not `manage.py test`.** Already the convention here before the port.
- **No migration-history baggage.** `PTUser`/`OTPCode`/`TrustedDevice` are plain `AutoField`-PK
  models with a clean history; none of TDS's `TDSUser` workarounds apply.
- **`cache_page` infrastructure exists, nothing uses it.**
- **No TOTP.** An authenticator-app second factor was built and then fully removed the same day at
  the project owner's request ("happy with device aware"); no code or migration remains.

### Non-negotiables

**`AUTH_USER_MODEL` stays at Django's default (`auth.User`).** This app's real user model, `PTUser`,
is a plain unrelated model, not a Django auth user. Every authentication path resolves `PTUser`
directly instead of touching `get_user_model()`: `PTUserBackend.authenticate()`/`get_user()`,
`PTJWTAuthentication.get_user()` (reads the `sub` claim), and `pt_user_authentication_rule()` (all in
[auth_backend.py](../apps/api/auth_backend.py)). `PTUser` declares `is_authenticated = True` /
`is_anonymous = False` itself, because DRF's `IsAuthenticated` reads them and it does not inherit
from `AbstractBaseUser`.

**Any new code - or any simplejwt/DRF upgrade - that calls `get_user_model()` will silently resolve to
`auth.User`, not `PTUser`, and almost certainly do the wrong thing or crash.** This is not
theoretical: it caused a real production incident in TDS (simplejwt's stock
`TokenRefreshSerializer.validate()` calls `get_user_model().objects.get(**{USER_ID_FIELD: ...})` to
re-check the user is active, and `auth.User` has no `user_id` field - every token refresh crashed).
Fixed here proactively: `PTTokenRefreshSerializer` overrides `validate()` to resolve `PTUser`
directly, wired in via `PTTokenRefreshView.serializer_class`. **Before adopting any simplejwt/DRF
upgrade, grep it for new `get_user_model()` call sites.** (The one deliberate `get_user_model()` in
the app is `checks.check_no_unexpected_django_superuser`, which really does want `auth.User`.)

**Never add `rest_framework_simplejwt.token_blacklist` to `INSTALLED_APPS`.** Confirmed the hard way:
it crashes `device_verify` with `"OutstandingToken.user" must be a "User" instance` the moment a
`PTUser` is passed to `RefreshToken.for_user()`, because that app's `OutstandingToken` FKs to
`AUTH_USER_MODEL`. Revocation here is `RevokedRefreshToken` instead - a small custom table keyed on
`jti` alone, no user FK needed, written by
[token_revocation.py](../apps/services/token_revocation.py)'s
`revoke_refresh_jti()`/`is_refresh_jti_revoked()`. (`ROTATE_REFRESH_TOKENS`/`BLACKLIST_AFTER_ROTATION`
are `True` and work fine without that app, because `PTTokenRefreshSerializer` implements the
rotate-and-revoke itself.)

**`cache_page` must never sit above a permission check, and any `cache_page`-wrapped view must be
`AllowAny`.** `cache_page` short-circuits on a hit and returns the stored response without re-invoking
the view - so a permission check inside the view only runs on the request that *misses* the cache;
after that, the same response is served to any caller regardless of auth for as long as the entry
lives. This bit TDS in production on a real endpoint. No endpoint here uses it; if one ever does, it
must be genuinely public data and gate nothing sensitive. (Caching *derived data* inside a view whose
permission check still runs per request, like `sync-status`'s 60-second summary cache, is not this
trap.)

**`SecurityHeadersMiddleware` must sit before `WhiteNoiseMiddleware` in `MIDDLEWARE`, not after.**
Django's list is outermost-first for the request phase, so a later entry is more inner.
`WhiteNoiseMiddleware` short-circuits static-file responses (every frontend HTML/CSS/JS page) by
returning directly, without calling further down the chain - a middleware placed after it never runs
for any static response, which in a frontend with no SPA shell is most of what a browser renders.
TDS shipped with this backwards for a while; this app has it correct. **If you reorder `MIDDLEWARE`,
re-check this specifically.** The current order in [settings.py](../config/settings.py) is:
`SecurityMiddleware`, (`NoCacheMiddleware` in DEBUG only), `SecurityHeadersMiddleware`,
`WhiteNoiseMiddleware`, `SelectiveGZipMiddleware`, `ApiNoStoreMiddleware`, `SessionMiddleware`,
`CommonMiddleware`, `AdminOnlyCsrfMiddleware`, `AuthenticationMiddleware`, `MessageMiddleware`,
`XFrameOptionsMiddleware`. `SelectiveGZipMiddleware` is after WhiteNoise on purpose (see
[File reference: config/middleware.py](#configmiddlewarepy)).

### Roles and plant scoping

Roles: **`admin`** (full access + user management), **`editor`** (full dashboard access + dismissing/
overriding flags + inline corrections + manual MIR pins), **`viewer`** (read-only).

Read endpoints are plain `IsAuthenticated` on *role* - any role can read, by design - **but they also
narrow by `PTUser.plants`** via `permissions.user_can_access_plant()`, the same underlying check
`user_can_edit_plant()` uses for writes (it literally delegates). **An empty `plants` list means "all
plants"**, not "no plants", so accounts predating the field are unaffected and no backfill was needed.
Plant keys are the lowercase `hrs`/`achhad`/`vapi` frontend keys, not `SyncRun.Plant`'s enum. An
admin's `plants` is always forced to `[]` on create and update, because neither helper special-cases
role and a scoped admin would otherwise lock themselves out of writes.

How an out-of-scope plant is refused differs by endpoint shape, deliberately: domestic single-plant
reads **403** outright; Import's cross-plant `purchase_orders`/`sync_status` **silently narrow** the
combined result (the same "you see less, not an error" shape the endpoint already had for a plant with
zero rows); `purchase_order_detail` **404s** rather than 403, matching its existing unknown-plant
behaviour rather than confirming a PO exists.

`IsEditor` gates: `correct_field` (domestic and import), `correct_material_field`,
`dismiss_po_mir_match`/`dismiss_mir_stock_match`/`dismiss_import_po_mir_match`/`dismiss_flag`
(domestic and import), `set_mir_match` (domestic and import), and the stock-snapshot export.
`IsAdmin` gates: every plant `sync_trigger` and the imports/RoDTEP/Advance Licence sync triggers,
`admin_overview`, and every [users_views.py](../apps/api/routers/users_views.py) endpoint (including
the password field on `PATCH /api/auth/users/<id>`). The per-endpoint list lives in
[api-and-features.md](api-and-features.md); treat this one as a summary.

**If you add a new read OR write endpoint, gate both role and plant** - don't leave a new endpoint
unscoped by plant just because it's "only a read".

**This is enforced now, not remembered (2026-09-23).** `apps/api/tests/test_endpoint_permission_guard.py`
walks the AST of every `@api_view` under `apps/api/` and fails if (1) an endpoint accepting
POST/PUT/PATCH/DELETE has no `@permission_classes` - the project default lets ANY role write, so
omitting it is a decision that used to be invisible - or (2) an endpoint that handles a plant (a
`plant` argument, a `"plant"` field, a `plant` variable, or a per-plant `cfg`) never calls a scoping
helper. Legitimate exceptions live in two allow-lists **with their reason**, and a stale entry fails
too. It exists because the review router grew to five endpoints with no plant scoping on any, despite
the sentence above. It is a tripwire, not a proof: it cannot tell whether the scoping call is in the
right place, only that someone thought about it.

`permissions.is_allowed_email_domain()` restricts accounts and logins to `@<ALLOWED_EMAIL_DOMAIN>`
(default `ravasco.com`, case-insensitive), enforced at password login (as a defence-in-depth check
after bcrypt), Google OAuth, in-app account creation and `create_pt_user`. The delete-user gate
reads `DELETE_USER_ALLOWED_EMAIL` (defaulting to the previously hardcoded address, blank treated as
unset) so the restriction survives a personnel change without a code change and redeploy. Deleting
a user is otherwise `IsAdmin`; every other admin gets Deactivate only.

**The last active admin cannot be removed.** `users_views._assert_not_last_active_admin()` locks
**every** active admin row with `select_for_update()` inside the same `transaction.atomic()` as the
save/delete. A lock scoped to "every admin except the target" would not work: two concurrent requests
excluding different admins never contend, and both could succeed, leaving zero admins. The
regression test uses two real threads with separate DB connections
(`@pytest.mark.django_db(transaction=True)`), since a row lock means nothing inside one transaction.

### Throttling, lockout, and brute-force counters

**Login throttles are keyed per-account, not per-IP.** `AnonRateThrottle`'s default `get_cache_key()`
keys on client IP, which `LoginRateThrottle` (5/min) and `DeviceVerifyThrottle` (10/min) inherited -
so every caller behind the same office router/VPN/NAT exit IP shared **one** bucket. A handful of
colleagues signing in within a minute exhausted it, after which every attempt from that IP got a
generic `429 {"detail": "Request was throttled..."}` indistinguishable from a real failure. Reported
as sign-in "misbehaving very much even [with] correct credentials".

Fixed: `LoginRateThrottle.get_cache_key()` keys on the submitted `email` (lower-cased; falling back to
the IP key only when no email was submitted); `DeviceVerifyThrottle.get_cache_key()` keys on the
session's `pending_user_id`, which is unique per in-flight login attempt. Per-account strength is
unchanged; it just no longer pools unrelated accounts. **If you add another `AnonRateThrottle`
subclass anywhere in the auth flow, key it the same way or this exact bug reappears.**

Endpoints that set no `throttle_classes` get the project defaults, `AnonRateThrottle` 60/min (per
IP) and `UserRateThrottle` 200/min (per user). That includes `POST /api/auth/token/refresh`:
simplejwt's `TokenViewBase` has `authentication_classes = ()`, so every refresh is anonymous and
lands in the per-IP 60/min bucket. It has not been a problem at this app's size, but it is the one
IP-pooled bucket left in the auth flow.

Scoped rates (all in `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]`): `login` 5/min, `otp_verify` 10/min
(device-verify, and reused by `PasswordChangeConfirmThrottle`, keyed per user),
`password_change_request` 5/min, `sync_trigger` 10/min, `admin_write` 30/min. Higher-blast-radius
writes have their own scopes rather than the generic "user" bucket: `SyncTriggerThrottle` (each
request queues a real Drive-sync job) and `AdminWriteThrottle` (user create, update/delete, and the
admin "log out everywhere"; listing and revoking devices use the default).

**Account lockout sits on top of throttling, not instead of it.** A rate limit slows password guessing
but never stops it, with no signal an account is under sustained attack. `PTUser.failed_login_attempts`
/`locked_until` (migration `0020`): `PTUserBackend.authenticate()` increments on a wrong password and
locks for 15 minutes at 5 (`_MAX_FAILED_ATTEMPTS`/`_LOCKOUT_DURATION`), resetting the counter to 0
at the moment it locks and on any correct password. **The lockout check runs before the bcrypt check
and burns an equivalent dummy-bcrypt delay** (`_dummy_verify()`), so a locked account isn't
distinguishable by timing from a wrong password on an unlocked one. The same ordering rule applies to
`is_active`: bcrypt always runs before the active check, so an inactive account does not answer
faster either. **Until 2026-09-24 that delay was not happening**: the dummy hash was `"$2b$12$"` plus
52 `a`s, 59 bytes, one short of a valid bcrypt hash, so `checkpw()` raised "Invalid salt" in
~0.04 ms against ~240 ms for a real check and the deliberate bare `except` hid it - unknown emails
and locked accounts answered ~240 ms faster, which is exactly the enumeration signal the function
exists to remove. The TDS app had the same line. `_DUMMY_HASH` is now a genuine cost-12 hash and
`test_login_timing_dummy_hash.py` fails on the old value (it must verify without raising, match
`_hash_password()`'s cost, and an unknown email must not answer much faster than a wrong password).
**If the bcrypt cost changes, regenerate it.** **Google OAuth honours the lockout too** - it once
checked only `is_active`, so five failed passwords locked the password door and left the Google door
open. It needs no timing equalisation: reaching that check already required a real Google round trip
for that verified address.

**The brute-force counters are row-locked.** `_register_failed_attempt()` and `otp_service.verify_otp()`
each used to compute a new value from an in-memory object and write it back separately; concurrent
requests could read the same stale count and each write the same single increment, undercounting
attempts. Both now use `select_for_update()` + `transaction.atomic()` around the whole check.
The `token_version` bump (in `revoke_all_tokens()`, which `revoke_all_sessions()` calls) uses an
`F("token_version") + 1` in-DB expression instead, since it needs no decision based on the read value.

### Sessions and tokens

`pt_access` (12h, path `/`) and `pt_refresh` (30 days, path-scoped to `/api/auth/`) are httpOnly,
`SameSite=Lax`, and `Secure` whenever `DEBUG` is off. Their `max_age` is read from
`SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"]`/`["REFRESH_TOKEN_LIFETIME"]`, so the cookie and token lifetimes
cannot drift. A non-browser client can send `Authorization: Bearer <token>` instead -
`PTCookieJWTAuthentication` tries the cookie first, then the header. `SameSite=Lax` on the cookie is
the API's CSRF defence; Django's CSRF check runs only under `/admin/`.

**The refresh token never appears in a response body.** It once did in `login`/`device-verify`,
handing a 30-day credential to any script on the page at the exact moment of authentication; it now
travels only as the httpOnly cookie. `PTTokenObtainPairSerializer.validate()` passes it to
`PTLoginView.post()` under a private `_refresh` key that the view pops before responding (rename one
without the other and it leaks); `device_verify` reads it from the token object directly.
`PTTokenRefreshView.post()` reads the cookie when the body has no `refresh`, returns only `access`,
and re-cookies the rotated token. A non-browser client can still pass `refresh` in the body.

**"Log out everywhere"** is `PTUser.token_version` (migration `0021`), embedded as a `ver` claim in
every JWT. Both `PTJWTAuthentication.get_user()` (checked on every request) and
`PTTokenRefreshSerializer` reject a token whose `ver` doesn't match (a missing claim reads as 0).
This is stronger than `RevokedRefreshToken` alone, which only knows about tokens explicitly rotated
away or logged out and never covered a live access token. `revoke_all_sessions()` bumps the counter
**and** deletes every `TrustedDevice` row (a fresh sign-in goes through 2FA again). Wired to
`POST /api/auth/logout-everywhere` (self-service, any authenticated account; also clears the
caller's own cookies and session) and `POST /api/auth/users/<id>/logout-everywhere` (`IsAdmin`, e.g. a
reported compromise). Verified with a real multi-client test proving a second already-logged-in
client's live access token stops working the instant the first revokes. Deactivating an account
needs no revocation: `get_user()` rejects an inactive user on every request.

**A password change evicts sessions.** Both password-setting paths once saved a new hash and stopped,
touching nothing that invalidates an already-issued JWT - so a stolen access token stayed valid for its
full 12h and the sliding refresh cookie could renew indefinitely. Since the overwhelmingly common
reason to change a password is suspected compromise, the remedy did not work against the case it
exists for. `revoke_all_tokens()` is **split out from `revoke_all_sessions()` on purpose**: the latter
also deletes every `TrustedDevice`, which is right for a panic button but wrong for a routine rotation
(a `pt_device` cookie only ever skips the OTP step, never the password, so it is worthless to an
attacker once the password changes - wiping it would re-challenge every colleague on every device for
no security gain). The self-service path re-issues fresh cookies for the caller in the same response,
so changing your own password doesn't sign you out of the browser you changed it in. It must re-read
the user row first: the bump is an in-DB `F()` expression, so the in-memory `request.user` still holds
the old `token_version` and would mint a token that fails on the next request.

**Logout** (`POST /api/auth/logout`, `AllowAny`) revokes the current `pt_refresh` jti, flushes the
session and deletes `pt_access`/`pt_refresh`. It deliberately keeps `pt_device`, so the browser stays
trusted for the next sign-in.

**The browser renews its session silently (2026-09-23).** Until this date nothing in `frontend/js/`
ever called `/api/auth/token/refresh`: every 401 went straight to `/login.html`, so the 30-day
`pt_refresh` cookie never extended a browser session and everyone was signed out 12 hours after
signing in - often mid-edit, with the freshness watcher's next poll doing the bouncing. `auth.js` now
has `authFetch()`, a drop-in for `fetch()` that on a 401 calls `refreshSession()` and replays the
request once; `apiForPlant()`, `apiImports()`, `apiReview()`, `savePoField()`, `requireAuth()`, the
password-change calls and every `admin-page.js` call go through it. Callers keep their own 401 ->
login redirect, which now fires only when renewal genuinely failed. Three details are load-bearing:

- **`refreshSession()` is single-flight.** Refresh tokens rotate and the spent one is revoked, so two
  concurrent renewals present the same token twice and the loser is refused - signing the user out
  exactly when several requests expire together, which is every page load after hour 12. Every caller
  awaits the one in-flight promise.
- **The request is replayed even when renewal fails.** With two tabs open, the other tab may have just
  rotated the shared cookie, so this tab's renewal is refused while the cookie jar already holds a
  valid new access token. The replay picks it up; a second 401 falls through to the login redirect.
- **No revocation got weaker.** "Log out everywhere", a password change and an expired refresh cookie
  all make the server refuse the refresh, so they still end the session. Replaying a POST/PATCH is safe
  because a 401 means authentication failed before the view ran.

`apps/api/tests/test_browser_session_renewal.py` pins the server side of that contract (including that
a copied refresh token cannot resurrect a revoked session); the frontend was verified in the Browser
pane with a stubbed `fetch` (single-flight, two-tab race, PATCH body replayed intact, every wrapper).
See [frontend.md](frontend.md) for `auth.js` itself.

`prune_revoked_tokens` deletes `RevokedRefreshToken` rows past their own `expires_at` (safe: an
expired token is rejected by JWT validation before this table is consulted). It is also reachable as
`/api/internal/prune-revoked-tokens` behind the report shared secret, but no cadence is configured.
Session cookie age is an explicit 30 minutes (`SESSION_COOKIE_AGE = 1800`, not Django's 2-week
default) - that DB-backed session only ever carries short-lived state (the Google OAuth `state` and
PKCE `code_verifier`, the one-time `oauth_delivery` hand-off, or `pending_user_id` during the
10-minute OTP window), never the main JWT auth path. `SESSION_SAVE_EVERY_REQUEST = True` is required
so a session write inside a redirecting view (`google_login`) is committed before the browser leaves.

bcrypt cost is pinned explicitly at `rounds=12` in both `users_views._hash_password()` and
`create_pt_user.py` - the same value bcrypt already defaulted to, just no longer implicit. OTP codes
use `rounds=10` (short-lived six-digit codes).

**Password policy** (`users_views._validate_password_strength`, mirrored in `create_pt_user.py`):
minimum **10** characters, rejects a purely-numeric password, rejects one identical to the account's
email local-part. Deliberately modest - this is an admin-bootstrapped internal tool, not a public
signup form. Django's `AUTH_PASSWORD_VALIDATORS` are configured but never consulted (nothing calls
`validate_password()`). The minimum lives in five places across three languages, so
`test_password_policy_is_stated_consistently.py` probes the real validator and asserts every
placeholder, client-side check, error message and CLI copy agrees with it.

**The OTP store is shared.** Login-OTP and password-change-OTP use the same `pt_otp_codes` row per
email (one active code per address), so requesting a password-change code invalidates a login code
still pending for the same address, and vice versa. Each code lives 10 minutes and allows 5 guesses;
the attempt counter is incremented before the bcrypt check so a request that dies mid-check still
counts.

### Alerts, audit log, and logs

[security_alerts.py](../apps/services/security_alerts.py) sends three best-effort, never-propagating
email alerts (this app previously had zero alerting on security events, only passive log lines nobody
watched): an **account-locked** alert the moment lockout actually fires (not on every failed attempt);
a **login-burst** alert for a credential-stuffing shape no per-account throttle would catch; and a
**sync-failure** alert.

The login-burst counter is cache-backed (`DatabaseCache`, `LocMemCache` under pytest), one counter
per fixed 5-minute bucket (`_BURST_WINDOW_SECONDS = 300`), counting every failed password on an
existing account and every unknown-email attempt (locked-account attempts are not counted). At 15
(`_BURST_THRESHOLD`) it alerts. The suppression key is per bucket, so **in practice it alerts at most
once per 5-minute bucket**, not once per 30 minutes: `_BURST_ALERT_SUPPRESS_SECONDS = 1800` is only
the suppression key's TTL, and a sustained attack that crosses 15 again in the next bucket alerts
again. Because the buckets are fixed, a burst straddling a boundary can reach up to 28 failures
without alerting.

[apps/core/audit_log.py](../apps/core/audit_log.py) defines `PTAuditLog` (`pt_audit_log`) and
`log_pt_action(request, action, detail='', actor=None)`. Actions: login, logout, user created/updated/
deleted, device revoked, sessions revoked. **Don't add action types speculatively - add one only when
a real mutating endpoint exists to log.** Wired into all three login paths (trusted-device fast path,
new-device OTP verify, Google OAuth trusted-device path; a Google login on a new device is logged by
the OTP verify), logout, both "log out everywhere" endpoints, the self-service password change (as
`user_updated`), and every `users_views.py` mutation. Browsable read-only in Django Admin with add/
change/delete disabled, matching the append-only intent. `log_pt_action()` never raises: a broken
audit write must not block a real login/logout. Field corrections and dismissals have their own
audit tables and are deliberately not duplicated here.

Separately, `LOGGING` writes every `INFO`+ line (Django's own plus every `apps.*` logger) to a rotating
`logs/app.log` (10MB x 5 backups) on top of console, and Sentry (when `SENTRY_DSN` is set) turns every
`ERROR`+ log line into an event with `send_default_pii=False`. **Don't conflate the two**:
`logs/app.log` is an operational trace (what the server did); `pt_audit_log` is a permanent security
record of who logged in/out, who changed which account, and from where. Neither substitutes for the
other.

`PTCookieJWTAuthentication.authenticate()` catches `(InvalidToken, AuthenticationFailed)` at `DEBUG`
and everything else at `WARNING` with a full traceback, still falling through to the Bearer attempt.
It used to catch bare `Exception`, treating a genuine bug (e.g. a DB error inside `get_user()`) the
same as an ordinary expired cookie. Raising outright here would wrongly turn a bad cookie into a hard
error for browser clients that never send a Bearer header.

`exceptions.py` deliberately excludes `KeyError` from `_DESCRIBABLE_EXCEPTIONS` - returning `str(exc)`
for one leaked internal dict key names AND reported a genuine server bug to the caller as a 400. It
falls through to a generic 500 now, still logged with a traceback.

### Deploy-time checks and headers

[apps/core/checks.py](../apps/core/checks.py) (registered by importing it in `CoreConfig.ready()`)
runs as part of `manage.py check --deploy --fail-level WARNING`, the same command CI gates on:

- `check_jwt_signing_key_is_independent` (`apps.core.W001`) warns (outside `DEBUG`) if
  `JWT_SIGNING_KEY` equals `SECRET_KEY`, i.e. is still falling back to it.
- `check_no_unexpected_django_superuser` (`apps.core.W002`) warns if an active `auth.User` superuser
  exists at all - real login never touches `auth.User`, so a superuser is an undocumented, unaudited
  path into `/admin/` that bypasses `PTAuditLog`. (`ModelBackend` stays in `AUTHENTICATION_BACKENDS`
  only so such an account could reach Admin if one were deliberately created.)

`SILENCED_SYSTEM_CHECKS = ["security.W003"]` (no `CsrfViewMiddleware`) is deliberate: the API never
uses session CSRF, and `AdminOnlyCsrfMiddleware` restores the unmodified check for `/admin/`. Full
`CsrfViewMiddleware` app-wide would 403 every unsafe API call regardless of how it authenticates.

**CSP** ([config/security_headers.py](../config/security_headers.py)):
`script-src 'self' https://cdn.jsdelivr.net`, `style-src 'self' https://fonts.googleapis.com` - **no
`'unsafe-inline'` in either**. Dropping `script-src`'s required extracting every inline `<script>` to
its own file and converting every inline event-handler attribute to a real listener (the
logo-fallback `onerror` pattern is one capture-phase listener in `theme-init.js` keyed on
`data-hide-on-error`; the modal close-button `onclick` pattern is one delegated listener in
`charts.js`). Dropping `style-src`'s required moving static inline styles to `brand.css` utility
classes and genuinely dynamic ones to `data-*` attributes plus a small JS pass (`shared.js`'s
`applyDynamicStyles()`, `admin-page.js`'s `renderBarList()`). A new inline `<script>`, `style="..."`
or `on*=` attribute is silently blocked, with only a console CSP violation to show for it. A new CDN
script needs its origin added to `script-src` or it fails the same silent way (Chart.js missing once
blanked the whole dashboard). The rest of the policy: `default-src 'self'`, `img-src 'self' data:
blob:`, `connect-src 'self'`, `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'self'`,
`form-action 'self'`, plus `CSP_EXTRA_DIRECTIVES` if set. The same middleware adds `nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and HSTS outside DEBUG.

DRF's **Browsable API is disabled** (`DEFAULT_RENDERER_CLASSES` pinned to `JSONRenderer`) - this is an
internal same-origin JSON API with a static frontend; the interactive HTML/schema UI DRF enables by
default regardless of `DEBUG` was attack surface for no benefit.

`index.html`'s jsdelivr-hosted Chart.js `<script>` carries a Subresource-Integrity hash computed
against the exact pinned `chart.js@4.5.0` build, so a compromise of that CDN file is blocked by the
browser rather than silently executed. Bumping the version means recomputing the hash.

The `pt_device` cookie's `secure` flag reads `settings.PT_DEVICE_COOKIE_SECURE` directly rather than
`getattr(settings, ..., False)` - it fails loudly if the setting is ever removed instead of silently
degrading to an insecure cookie, matching `pt_access`/`pt_refresh`'s fail-closed style.

**API responses are compressed except under `/api/auth/` and `/admin/`** (`SelectiveGZipMiddleware`):
those are the two places a secret sits in a response body next to reflected input, which is what the
BREACH attack needs. **If you add an endpoint that returns a token or OTP in its body, put it under
`/api/auth/` or add its prefix to `UNCOMPRESSED_PATH_PREFIXES`.**

**Injection status**: three parallel audits found no exploitable injection anywhere - zero raw
SQL/eval/exec in `apps/`, ORM-only DB access, the Drive query escaped, and the frontend consistently
running dynamic content through `escapeHtml()`/`textContent` before touching `innerHTML`. Keep it that
way. Every URL segment in `shared.js`/`material-modal.js` is `encodeURIComponent()`-wrapped even where
the value is backend-issued today, so a future reuse of that URL-building code can't silently regress.
Email bodies are HTML-escaped by `render_email()`, and the User-Agent-derived device name has control
characters stripped before it reaches an email.

**Dependabot** (`.github/dependabot.yml`) monitors pip, GitHub Actions and the Docker base image with
a **7-day cooldown** on each (`cooldown.default-days: 7`, project owner: "open source library will be
updated only after 7 days of any new version") - giving the upstream community a window to catch a
broken or malicious release first. A real CVE fix still lands within the same week.

---

## Outgoing email

Every email goes through one of four builders: `email_service.render_email()` (plain paragraphs,
shared by 7 of the 12), `consumption_report.py`'s table builder (8-9), `plant_mismatch_report.py`'s
table builder (10), or `advance_license_report.py`'s table builder (11-12). **Emails 1-9 and 11-12
end with "This is a system generated email. Please do not reply.", and none use an em dash in body
content** (this app's convention is a plain `" - "` and `"N/A"`; `test_no_em_dashes.py` enforces it).
**Email 10 deliberately has no such footer.** The seven `render_email()` emails (1-7) are sent as
**plain text only**: the builder returns an escaped HTML body too, but no caller passes it as
`html_message`. Emails 8-12 send both HTML and plain text. There are no template files; every body is
built in Python. SMTP comes from `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM` (port 465
= SSL, 587 = STARTTLS) with `EMAIL_TIMEOUT = 10`.

| # | Email | Recipients |
| --- | --- | --- |
| 1 | Login OTP (`device_service.send_device_otp()`) | the signing-in user, on a new/untrusted device |
| 2 | New device signed in (`send_new_device_notification()`) | that same user, right after verifying |
| 3 | New device login alert (`notify_admins_new_device_login()`) | every other active admin |
| 4 | Password change OTP (`password_service.send_password_change_otp()`) | the requesting user |
| 5 | Account locked (`security_alerts.notify_admins_account_locked()`) | every other active admin, once, when lockout fires |
| 6 | Unusual login activity (`record_failed_login_and_maybe_alert()`) | every active admin |
| 7 | Sync failure (`notify_admins_sync_failure()`) | `dishant.barot@ravasco.com` and `masira.balouch@ravasco.com` only - a fixed list per an explicit request, unlike 3/5/6/8 |
| 8 | Daily RM Consumption Report (x3, one per plant) | every active admin plus the fixed `purchase@ravasco.com` |
| 9 | Monthly RM Consumption Report (x3) | every active admin plus the fixed `purchase@ravasco.com` |
| 10 | Plant Data Correction Report (x3) | a fixed plant head each, CC'ing every admin |
| 11 | Advance License Import Validity Expiry (`advance_license_report.send_advance_license_expiry_reports()`) | every active admin plus the fixed `imports@ravasco.com` |
| 12 | Advance License Export Validity Expiry (same function) | every active admin plus the fixed `imports@ravasco.com` |

Emails 1-7 are sent from the web process through `_dispatch_email()` (see
[Dispatch](#dispatch-two-bounded-thread-pools-not-the-task-queue)). Emails 8-12 are sent
synchronously inside a request to one of the shared-secret `POST /api/internal/*` endpoints in
[reports_views.py](../apps/api/routers/reports_views.py), which an external scheduler (cron-job.org)
calls. The secret is `REPORT_CRON_SECRET`, accepted as an `X-Report-Secret` header (preferred), a
`?secret=` param or a body field, compared with `hmac.compare_digest`; a blank setting makes every
endpoint answer 503 rather than fail open, a wrong secret 403. The cron schedule and the "never use
cron-job.org's test run" rule are in [data-sync.md](data-sync.md).

**8 - Daily RM Consumption Report.** Triggered by the external scheduler hitting
`POST /api/internal/send-daily-report`. A real HTML table (Material / Issued Today / Latest Rate /
Days Left, plus Vendor), **grouped by category**, rendering one shaded heading row per category,
ordered by whichever category holds that plant's single biggest mover for the day (materials within a
category stay issued-qty-descending). A blank category buckets as **"Uncategorized"** rather than
being dropped - real on some rows. Both reports read the consumption ledger, one row per material;
what "Issued Today", `(est.)` and an empty report mean is in [consumption.md](consumption.md).

*Superseded 2026-09-21 - Achhad's "Issued Today" fallback is gone.* It existed because Achhad's
own `issued` column is a period-to-date summary and its daily Recp./Issue matrix could sit blank
for the current day, so a lot with no dated row fell back to that live cumulative column, flagged
`(est.)`. The read path no longer reads any live cumulative column: both reports read the
consumption ledger, into which Achhad's matrix is reconciled at build time. A day the ledger has
no rows for now reports **nothing**, which is the honest answer - the old fallback printed a
month-to-date running total under a heading that said "Issued Today". `isEstimate`/`(est.)`
survives with a different and now uniform meaning: the quantity was interpolated across a snapshot
gap rather than observed on one dated day, at any of the three plants.

**A Vendor column (2026-09-24, both #8 and #9).** HRS and Vapi read the Stock sheet's own supplier
column (`party_name`/`supplier_name`), highest-value lot first, capped at three names plus "+N more".
Achhad's sheet has no vendor column, so there it is the `party_name` on the MIR receipts matched to
the lot (dismissed matches excluded), marked **"(per MIR)"** because a MIR<->Stock match is a
suggestion. That covers 114 of Achhad's 303 materials today; the rest show "-".

**The trigger reports each plant's outcome, and a failure is a 502 (2026-09-24).** On 2026-09-23 only
Vapi's daily report arrived, and the endpoint had answered `200 {"status": "ok", "plants_sent": 1}`,
so cron-job.org logged a success. The result now carries `plants`
(`sent`/`already_sent`/`failed`/`deferred` per plant) and `failures` (exception type plus message),
and `_report_response()` returns **502 `partial`** when any plant failed or was deferred. Re-running
is safe: sent plants are deduplicated by their claim and failed ones released theirs. The cause of
the 2026-09-23 failure was not established from here - no production log access - and the log line to
look for is `failed (connect/login|send) for plant=`.

**That run took 23.94s, and 30s is gunicorn's kill (2026-09-24).** A report builds in 20-110ms and a
Gmail connect+EHLO takes ~0.8s (measured from a dev box against the local copy; Render's data is
larger), so 24s is two 10s `EMAIL_TIMEOUT` stalls plus one normal send. One more stall and the
request is killed mid-send. That skips the `except` that releases the claim, and since `sent_at` is
written at claim time, the plant is stuck for the day with no email. `_send_plant_reports()` (shared
by daily and monthly) therefore:

- starts a plant only while the run is under `_REPORT_START_BUDGET_S` (12s). A plant not started is
  `deferred`, **unclaimed**, and makes the run a 502, so a re-run sends it;
- uses **one** SMTP connection for every plant, with `_REPORT_SMTP_TIMEOUT_S` (4s) per step instead
  of 10 - one handshake and Gmail login per run, not three - and drops it after a failure so the next
  plant reconnects;
- returns per-plant `timings` (`buildMs`/`sendMs`), `elapsedMs`, and the failing SMTP phase
  (`connect/login` or `send`) in `failures`. That answers "why is it slow" from Render's own numbers.

Not a hard guarantee: a plant starting at 11.9s that then stalls on several SMTP steps could still
overrun. A stall normally hits one step (the connect), which this bounds to 4s.

**9 - Monthly RM Consumption Report.** `POST /api/internal/send-monthly-report`, a separate endpoint
rather than a mode flag since the two run on genuinely different schedules. Same category-grouped shape
as #8 (both share `_render_consumption_rows()`/`_render_consumption_email()` so they can't drift
apart), summed over a calendar month; the column reads "Issued This Month". Defaults to the most
recently **completed** month - the natural target for a report firing on the 1st; `year`/`month` query
params override for a manual re-send. `daysLeft`/confidence stay a present-tense estimate, same meaning
as in the daily report.

**It sums the ledger, one SUM over the same daily rows the daily report reads.** Superseded
2026-09-21: it used to sum `*RMSnapshot.issued` across the month for HRS/Vapi on the belief that
their column was a genuine per-day figure. It is period-to-date at **all three** plants, so that
summed a running total once per snapshot. The safety property that mattered survives and is
pinned by a test - a closed month is never estimated from whatever period is currently open; a
material with no dated rows for it is **excluded rather than estimated**, a visible gap being more
honest than an untrustworthy number.

**10 - Plant Data Correction Report.** `POST /api/internal/send-mismatch-report`. Unlike every other
email here, this goes to a **fixed, real individual per plant, not the internal admin list**:
`avijit.ghosh@ravasco.com` (HRS), `anil.khatri@ravasco.com` (RTP-Achhad), `mahendra.patil@ravasco.com`
(RTP-Vapi) - see `_PLANT_HEADS`. None is necessarily a `PTUser`; the list is a fixed dict, not derived
from the DB. **Every active admin is CC'd.** Real delivery is currently **switched off** by
`MISMATCH_REPORT_PLANT_HEADS_ENABLED` (default `false`) while matching is tuned; the endpoint then
returns `plant_heads_disabled: true`. A `test_recipient` override is never gated and never reaches a
plant head or admin.

It lists that plant's currently-flagged (not dismissed) `qty_mismatched`/`rate_mismatched` rows from
both `*POMirMatch` (PO Number, Material) and `*MirStockMatch` (Material) - **the exact same booleans
the dashboard's Data Quality Flags read, not a re-derived definition**. "Whichever applicable": a row
only shows a percentage for the mismatch actually true on it (`-` for the other), never a stale or
zero-looking figure. **No "system generated / do not reply" footer** - this email is meant to prompt a
real reply and a corrected source document, so telling the recipient not to reply would defeat the
point; it ends with a plain "Regards,". A plant with nothing flagged is **skipped entirely**, not sent
a routine all-clear.

Correcting the source and re-syncing reaches the dashboard automatically: the next `sync_*`/`match_*`
run re-parses the corrected file and recomputes `qty_diff_pct`/`rate_diff_pct`/`is_flagged` in place,
and the dashboard reads live from the API with no cache to invalidate - so a corrected mismatch simply
stops appearing. No separate manual step.

**Dedup guard.** Neither daily nor monthly report had protection against the external scheduler
double-firing (a network retry, or an overlapping schedule) - every admin would get a duplicate email
with nothing to stop it. `ReportSendLog` (migration `0045`) holds one row per
`(report_type, plant, period_key)`, unique-constrained; each plant claims its row via `get_or_create()`
**before** building/sending (closing the actual race, not just a check-then-send a double-fire could
slip through), and **releases the claim if building/sending then raises**, so a transient failure stays
retryable rather than permanently burning that period's slot. `period_key` is an ISO date (daily),
`YYYY-MM` (monthly) or `license_number@YYYY-MM-DD` (Advance Licence, `plant="all"`).

**Until 2026-09-23 that release only ever worked for a BUILD failure.** Every sender passed
`fail_silently=True` to `send_mail()`, so an SMTP fault returned normally, the `except` never ran, the
claim stayed, and the next line logged the email as sent. For the daily report that cost a day; for the
monthly, a month; for the Advance Licence expiry alert (#11/12), whose key embeds the validity date and
so never recurs, **one transient SMTP failure permanently suppressed the only warning that an
authorisation was about to expire**. All three pass `fail_silently=False` now, and so does every other
sender (security alerts, new-device notices, the mismatch report) - each already had an `except` that
logs, so nothing can propagate into a login or a sync, but the failure is now logged as a failure (at
ERROR for the admin alerts and OTPs, so Sentry sees it; at WARNING for the new-device notice) instead
of as a success. Two guards: `test_email_delivery_is_observable.py` fails on any `fail_silently=True`
in application code, and the delivery-failure tests inject the fault through
`apps/services/tests/refusing_email_backends.py` - a backend that honours `fail_silently` exactly like
Django's SMTP backend. **Do not test a send failure by monkeypatching `send_mail` to raise**: a
replaced `send_mail` raises whatever `fail_silently` says, so that test passes with this exact bug
present.

**Deliberately NOT applied to the Plant Data Correction report** - asked rather than guessed. That
report has no fixed cadence by design, so a once-per-day lock could block an intentional same-day
re-trigger.

**11/12 - Advance License Import/Export Validity Expiry (2026-09-22).** Two independent, consolidated
alerts - `POST /api/internal/send-advance-license-expiry-report` (same shared-secret scheme as the
other `internal/*` endpoints), listing every `AdvanceLicense` whose `import_validity_date` /
`export_validity_date` falls within the next 30 days (`_EXPIRY_WINDOW_DAYS`): License Number, Export
Product Description, Input Material Description, CIF Value Authorized (INR), FOB Value Export Target
(INR), the relevant validity date, and days remaining.

**Fires once per license, the first time it's seen inside the 30-day window - not on the exact day
it crosses 30 days out.** The latter would silently miss a license entirely if that one day's
scheduler run didn't fire (a network hiccup, a deploy window), and the license would then age past
its validity date having never been alerted at all. Dedup reuses `ReportSendLog`
(`ReportType.ADV_LICENSE_IMPORT`/`ADV_LICENSE_EXPORT`, `plant="all"` since this isn't a per-plant
concept, `period_key="<license_number>@<validity date>"`) - same claim-before-send/release-on-failure
pattern as the daily/monthly consumption reports.

**An extended validity re-arms the alert (2026-09-22, migration `0058`).** The key was a bare
`license_number`, which meant the first alert burned that license permanently - and since an Advance
License's validity is routinely EXTENDED (the export side especially, sometimes more than once), the
new deadline would then come and go in total silence, which is exactly the case the alert exists for.
Putting the date being alerted on INTO the key keeps both guarantees at once: an unchanged date
re-claims the same row on every daily run and so never repeats, while a genuinely new date is a new
claim and alerts once on its own merits. Import and export are already separate `report_type`s, so
each side extends and re-alerts independently. `0058` widens `period_key` to 64 chars (a 50-char
`license_number` plus `@` plus an ISO date) and rewrites existing bare-number rows to the new format
so nothing already alerted fires a duplicate on the first run after deploy.

**Consequence for the daily cron:** the job runs every day, but a given license emails exactly once
per deadline. A day on which every in-window license is already claimed builds no rows and sends no
email at all, rather than an empty one.

**Materials are joined into one semicolon-separated cell, not repeated as one row per material.** A
license can carry several `AdvanceLicenseMaterial` rows (one per BOE usage, same
`material_description` repeated); a flat one-row-per-material table would repeat License
Number/CIF/FOB/dates down a long block of near-identical rows for a license with several materials.
Joining distinct, sorted material descriptions into one cell matches this app's own existing
convention for a multi-value cell (see `license_links.py`'s slash-joined multi-licence CSV cells) -
a third, inconsistent layout (grouped header + sub-rows) was considered and rejected for the same
reason. A license with no materials on file shows `-` rather than an empty cell.

Import and Export are independent per license - a license inside both windows at once (its import
and export validity dates both within 30 days) is alerted on both emails, each claiming its own
`ReportSendLog` row.

### Dispatch: two bounded thread pools, not the task queue

`_dispatch_email()` used to run `threading.Thread(...).start()` per message - a fresh OS thread per
email with nothing capping how many could exist. One login sends three, so twenty colleagues signing in
from new devices at 9am meant sixty threads, each holding an SMTP socket for up to `EMAIL_TIMEOUT`
(10s). `password_service.send_password_change_otp()` was worse: it bypassed the shared helper and
spawned its own `daemon=True` thread, so a worker restart mid-send silently killed that OTP - exactly
what `_dispatch_email`'s non-daemon behaviour exists to prevent, and which the *login* OTP was already
protected from. It also had no inline-under-pytest branch, so it behaved differently under test than
every other email.

**Why not django-q2, which is installed and already running a worker:** it would make **login depend on
the qcluster worker being alive and responsive**. Today the send happens in the web process, so signing
in is self-contained; queued, a worker that is down or backed up means nobody can sign in from a new
device at all. The ORM broker also polls, adding seconds to a code someone is watching the screen for,
and django-q2 pickles its tasks while these sends are closures over local state. **Trading a login
outage for architectural tidiness is a bad deal.** **Read this before "finishing the job" by queueing
these - the reasoning against it is the point, not an omission.**

**The fix is two bounded `ThreadPoolExecutor`s** - two rather than one for the same reason the queue was
rejected: an OTP must never wait behind a backlog of admin alerts. `_dispatch_email(fn, priority=True)`
routes the two user-is-waiting emails (login OTP, password-change OTP) to a dedicated lane; everything
else stays on the bulk lane. Sizes are env-tunable (`EMAIL_OTP_POOL_WORKERS`/`EMAIL_BULK_POOL_WORKERS`,
default 4 each, blank treated as unset). The pools are created lazily, so importing the module never
starts a thread.

**Preserved deliberately:** the inline-under-pytest branch (load-bearing - every test asserting against
`mail.outbox` right after a request depends on it; it checks `"pytest" in sys.modules`); non-daemon
shutdown semantics (`ThreadPoolExecutor` workers are non-daemon and Python joins them at exit, so a
clean shutdown lets an in-flight OTP finish); and a fallback that sends **inline rather than dropping
the message** when the pool refuses new work at interpreter shutdown - a user holding a code that was
never sent is the one outcome worth avoiding at any cost. A done-callback logs any exception that
escapes a send closure, which would otherwise vanish inside the `Future`.

Measured, not assumed: dispatching 40 emails creates 8 threads, not 40, with all 40 delivered; with the
bulk lane deliberately jammed by 40 stalled sends, OTP start latency stayed at ~2ms where a single
shared lane would have waited ~4s.

---

## File reference

### apps/api/auth_backend.py

[auth_backend.py](../apps/api/auth_backend.py) - the Django auth backend and DRF authentication
classes, all resolving `PTUser` directly (see [Non-negotiables](#non-negotiables)).

- `_verify_password(plain, hashed)` - `bcrypt.checkpw`, returns False (and logs) on a malformed hash.
- `_DUMMY_HASH` / `_dummy_verify()` - a real cost-12 bcrypt hash checked against a throwaway string to
  burn the same ~240 ms as a real check. The bare `except` is deliberate; the hash's validity is pinned
  by `test_login_timing_dummy_hash.py` instead. Regenerate it if the bcrypt cost ever changes.
- `_register_failed_attempt(user)` - under `select_for_update()` + `atomic()`, increments
  `failed_login_attempts`; at `_MAX_FAILED_ATTEMPTS = 5` sets `locked_until = now + 15 min` and resets
  the counter to 0. After the transaction it calls `security_alerts.record_failed_login_and_maybe_alert()`
  and, if it just locked, `notify_admins_account_locked()` (both best-effort, never raise).
- `PTUserBackend.authenticate(request, email, password)` - order matters: unknown email -> dummy
  verify + burst counter -> None; locked -> dummy verify -> None (not counted); bcrypt; on success
  clears counter/lock (via `.update()`), on failure registers the attempt; then rejects inactive,
  wrong password, and off-domain emails. Returns the `PTUser` or None. `get_user(user_id)` for
  Django's session machinery.
- `PTJWTAuthentication.get_user(validated_token)` - reads `sub`, loads the `PTUser`, rejects missing
  (`user_not_found`), inactive (`user_inactive`) and a `ver` claim that differs from `token_version`
  (`session_revoked`). Runs on every authenticated request.
- `PTCookieJWTAuthentication.authenticate(request)` - tries the `pt_access` cookie, falls back to the
  Bearer header. Expected token errors log at DEBUG; anything else logs at WARNING with a traceback;
  neither raises. It is `DEFAULT_AUTHENTICATION_CLASSES`' only entry.
- `pt_user_authentication_rule(user)` - `SIMPLE_JWT["USER_AUTHENTICATION_RULE"]`: user exists and is
  active.

### apps/api/auth_serializers.py

[auth_serializers.py](../apps/api/auth_serializers.py) - the login serializer (with the device gate)
and the refresh serializer.

- `PTTokenObtainPairSerializer.get_token(user)` - classmethod; claims `sub` (user id as string),
  `role`, `email`, `full_name`, `ver`, plus simplejwt's own `user_id`. Also called directly by
  `device_verify`, Google OAuth and the password-change view to mint tokens.
- `PTTokenObtainPairSerializer.validate(attrs)` - lower-cases the email, calls Django `authenticate()`
  (so `PTUserBackend`). Failure -> 400 "Invalid email or password." (one message for every cause).
  Trusted device -> `{"status": "ok", access_token, _refresh, user_id, role, full_name, email}`.
  New device -> sets `session["pending_user_id"]` **before** calling `send_device_otp()` (the OTP is
  committed first, so a later send hiccup does not lose the session), returns
  `{"status": "device_verify"}`; if `send_device_otp()` itself raises (the DB write, not SMTP, which is
  backgrounded) it returns 400 `otp_send_failed` rather than sending the user to a code screen that can
  never work.
- `PTTokenRefreshSerializer.validate(attrs)` - decodes the refresh token (expiry checked by simplejwt),
  rejects a revoked jti, loads the `PTUser` and rejects inactive or `ver` mismatch (all as
  `no_active_account`), mints an access token with `ver` copied explicitly, then (rotation on) revokes
  the old jti until its own `exp` and returns a new refresh token. Non-obvious: if the payload has no
  `user_id` claim at all, the user checks are skipped; every token this app mints carries one.

### apps/api/auth_views.py

[auth_views.py](../apps/api/auth_views.py) - `/api/auth/login`, `/token/refresh`, `/token/verify` and
`/me`.

- `LoginRateThrottle` - scope `login` (5/min), keyed on the submitted email; IP fallback only when no
  email was sent.
- `PTLoginView` (`POST /api/auth/login`, `AllowAny`) - on an `"ok"` result sets `pt_access`, pops
  `_refresh` into the `pt_refresh` cookie, writes the `login` audit row ("trusted device") and
  `last_login_at` via `.update()`. The access token stays in the body for non-browser clients.
- `PTTokenRefreshView` (`POST /api/auth/token/refresh`) - fills `refresh` from the cookie when the body
  lacks it, returns `{"access": ...}` only, re-sets both cookies. No custom throttle (see
  [Throttling](#throttling-lockout-and-brute-force-counters)).
- `PTTokenVerifyView` - simplejwt's stock `TokenVerifyView` (verifies a token string; a browser cannot
  use it because its token is in an httpOnly cookie).
- `whoami` (`GET /api/auth/me`, default `IsAuthenticated`) - `userId`, `email`, `fullName`, `role`,
  `plants`. `auth.js`'s `requireAuth()` calls it on every protected page load; the frontend uses
  `plants` to decide which pencils to render.

### apps/api/permissions.py

[permissions.py](../apps/api/permissions.py) - role permission classes, throttles and plant-scoping
helpers.

- `is_allowed_email_domain(email)` - case-insensitive `endswith("@" + ALLOWED_EMAIL_DOMAIN)`.
- `IsEditor` (admin or editor) / `IsAdmin` (admin) - both also require `is_active`.
- `SyncTriggerThrottle` (scope `sync_trigger`, 10/min) / `AdminWriteThrottle` (scope `admin_write`,
  30/min) - `UserRateThrottle` subclasses, keyed per user.
- `user_can_access_plant(user, plant_key)` - True when `plants` is empty or contains the key. Called
  from view bodies (not a `BasePermission`) because the plant is only known inside the view.
- `user_can_edit_plant(user, plant_key)` - delegates to `user_can_access_plant`; kept as a separate
  name so write call sites read as writes.

### apps/api/routers/device_views.py

[device_views.py](../apps/api/routers/device_views.py) - new-device OTP verify and both logouts.

- `DeviceVerifyThrottle` - scope `otp_verify` (10/min), keyed on `session["pending_user_id"]`, IP
  fallback when there is no pending login.
- `device_verify` (`POST /api/auth/device-verify`, `AllowAny`, body `{"code"}`) - 400 on a blank code,
  401 when the session has no `pending_user_id` ("Session expired"), 400 for an inactive user or a
  wrong/expired code. On success: mints tokens, `register_device()` (sets `pt_device`), sets both JWT
  cookies, sends emails 2 and 3, pops `pending_user_id`, writes the `login` audit row ("new device
  (email OTP verified)") and `last_login_at`. Same response shape as a trusted-device login, without
  the refresh token.
- `logout_view` (`POST /api/auth/logout`, `AllowAny`) - audit row, revokes the `pt_refresh` jti
  (best-effort: an already-invalid token is ignored), flushes the session, deletes `pt_access` and
  `pt_refresh` (with matching path/samesite). Keeps `pt_device`.
- `logout_everywhere_view` (`POST /api/auth/logout-everywhere`, `IsAuthenticated`) -
  `revoke_all_sessions(request.user)`, `sessions_revoked` audit row, then clears this browser's
  session and cookies like logout.

### apps/api/routers/device_urls.py

[device_urls.py](../apps/api/routers/device_urls.py) - routes `auth/device-verify`, `auth/logout`,
`auth/logout-everywhere`; included under `/api/` from `apps/api/urls.py`. No trailing slashes.

### apps/api/routers/google_oauth_views.py

[google_oauth_views.py](../apps/api/routers/google_oauth_views.py) - "Sign in with Google". Plain
Django views for login/callback (DRF's request wrapper interfered with session saves before a
redirect). No auto-registration: the Google address must already be an active `PTUser`.

- `_make_flow()` - builds the `google_auth_oauthlib` `Flow` from `GOOGLE_CLIENT_ID`/`SECRET`/
  `GOOGLE_OAUTH_REDIRECT_URI` with scopes `openid email profile`. The module sets
  `OAUTHLIB_RELAX_TOKEN_SCOPE=1` because Google sometimes returns full-URI scope names.
- `google_login` (`GET /api/auth/google/login/`) - stores `state` and the PKCE `code_verifier` in the
  session and force-saves it before redirecting to Google. Any failure redirects to
  `/login.html?oauth_error=start_failed` instead of a raw 500.
- `google_callback` (`GET /api/auth/google/callback/`, `csrf_exempt`) - each failure redirects to
  `/login.html?oauth_error=<code>`: `cancelled`, `state_mismatch` (the `state` check is the CSRF
  defence), `token_failed` (restores the code verifier first, or Google says "Missing code
  verifier"), `userinfo_failed`, `unverified_email`, `domain_not_allowed`, `not_registered`,
  `account_locked`, `login_failed`, `email_failed`. Trusted device: mints tokens, sets both cookies,
  stashes the JWT payload in `session["oauth_delivery"]`, audits and redirects to
  `/login.html?oauth_ready=1`. New device: sends the OTP, sets `pending_user_id`, redirects to
  `/login.html?step=device_verify`, after which `device_verify` completes it.
- `oauth_session_token` (`GET /api/auth/google/session-token`, `AllowAny`) - pops and returns
  `oauth_delivery` once, so the token never travels in a URL (history, logs, Referer). 400 when
  nothing is pending.

### apps/api/routers/google_oauth_urls.py

[google_oauth_urls.py](../apps/api/routers/google_oauth_urls.py) - `auth/google/login/`,
`auth/google/callback/` (both with trailing slashes; the callback must match the redirect URI
registered in Google Cloud Console) and `auth/google/session-token`.

### apps/api/routers/password_views.py

[password_views.py](../apps/api/routers/password_views.py) - self-service password change, OTP-gated
like a new-device login. Only ever touches `request.user`'s own row; resetting someone else's password
is the admin `PATCH /api/auth/users/<id>`.

- `PasswordChangeRequestThrottle` (scope `password_change_request`, 5/min) and
  `PasswordChangeConfirmThrottle` (scope `otp_verify`, 10/min), both per user.
- `request_password_change` (`POST /api/auth/change-password/request`, `IsAuthenticated`) - emails a
  code to the caller's own address and always answers 202 `{"status": "sent"}`.
- `confirm_password_change` (`POST /api/auth/change-password/confirm`, body `{"otp", "newPassword"}`)
  - verifies the OTP, applies `_validate_password_strength`, saves the new hash, `revoke_all_tokens()`
  (device trust kept), re-reads the user and re-issues both cookies, writes a `user_updated` audit row,
  returns `{"status": "ok", "sessionsRevoked": true}`.

### apps/api/routers/users_views.py

[users_views.py](../apps/api/routers/users_views.py) - in-app user management (feature detail in
[api-and-features.md](api-and-features.md)). Auth-relevant behaviour only here. Every endpoint is
`IsAdmin`.

- `_validate_password_strength(password, email)` - the app's one password policy (10+ chars, not
  all digits, not the email local-part); raises DRF `ValidationError` (400). Imported by
  `password_views.py`; mirrored by `create_pt_user`.
- `_hash_password(plain)` - bcrypt, `rounds=12`.
- `_clean_plants(raw)` - must be a list of `hrs`/`achhad`/`vapi`.
- `create_user` (`POST /api/auth/users/create`, `AdminWriteThrottle`) - domain check, 409 on a
  duplicate email, password policy, admin role forces `plants=[]`, audit `user_created`.
- `_DELETE_USER_ALLOWED_EMAIL` - from `DELETE_USER_ALLOWED_EMAIL`, defaulting to the project owner's
  address. Note the `PermissionDenied` message still names that default address even if the env var
  points elsewhere.
- `_assert_not_last_active_admin(exclude_pk)` - locks every active admin row; must be called inside
  `transaction.atomic()`. See [Roles and plant scoping](#roles-and-plant-scoping).
- `update_user` (`PATCH`/`DELETE /api/auth/users/<id>`, `AdminWriteThrottle`) - one view for both
  methods because `path()` matches URLs, not methods. DELETE: only the allowed email, last-admin guard,
  audit `user_deleted`, 204. PATCH: role/isActive/fullName/designation/plants/password; a non-blank
  password is policy-checked and triggers `revoke_all_tokens(user)` after save; audit detail names
  role/active changes and password resets.
- `list_user_devices` / `revoke_user_device` (`GET` / `DELETE /api/auth/users/<id>/devices[/<id>]`) -
  never return `device_token_hash`; revoking writes `device_revoked`. The revoked browser is
  re-challenged at its next sign-in (existing JWT cookies are unaffected until they expire).
- `admin_logout_everywhere` (`POST /api/auth/users/<id>/logout-everywhere`, `AdminWriteThrottle`) -
  `revoke_all_sessions(user)`, audit `sessions_revoked`. Does not deactivate or change the password.

### apps/api/routers/users_urls.py

[users_urls.py](../apps/api/routers/users_urls.py) - `auth/users`, `auth/users/create`,
`auth/users/<int:user_id>` (PATCH and DELETE), `.../devices`, `.../devices/<int:device_id>`,
`.../logout-everywhere`. List and create are split by path segment, not by trailing slash as in TDS,
because a trailing-slash distinction is fragile.

### apps/services/otp_service.py

[otp_service.py](../apps/services/otp_service.py) - Postgres-backed OTP store (`OTPCode`,
`pt_otp_codes`). Never sends anything itself.

- `generate_otp(email)` - `secrets.randbelow(1_000_000)` zero-padded to 6 digits; prunes every expired
  row; `update_or_create` on the unique lower-cased email with a bcrypt (`rounds=10`) hash, a 10-minute
  expiry and `attempts=0`, so there is at most one live code per address. Returns the plaintext code
  to the caller (never log it).
- `verify_otp(email, code)` - under `select_for_update()` + `atomic()`: missing -> False; expired ->
  delete, False; increments `attempts` first, and past `_MAX_ATTEMPTS = 5` deletes the row; wrong
  code saves the count; correct code deletes the row (single use). `_check_code()` fails closed on a
  corrupt hash.

### apps/services/password_service.py

[password_service.py](../apps/services/password_service.py) - one function,
`send_password_change_otp(user)`: `generate_otp()` for the user's own email, builds email 4 with
`render_email()`, sends it through `device_service._dispatch_email(..., priority=True)` with
`fail_silently=False` inside a closure that logs failures. Separate from `device_service.py` because it
is a different concern (an already-authenticated user), even though it shares the OTP store.

### apps/services/device_service.py

[device_service.py](../apps/services/device_service.py) - device trust, JWT cookie helpers, the
login/new-device emails and the shared email dispatcher.

- Constants: `DEVICE_COOKIE_NAME = "pt_device"`, `DEVICE_COOKIE_MAX_AGE` 365 days,
  `REFRESH_COOKIE_NAME = "pt_refresh"`, `REFRESH_COOKIE_PATH = "/api/auth/"`.
- `get_client_ip(request)` - the **last** `X-Forwarded-For` entry (the one Render's edge appended; the
  first is client-controlled), else `REMOTE_ADDR`. Used for audit rows, device rows and emails.
- `_hash_device_token(token)` - SHA-256 hex. Fast hash on purpose: the token has 256 bits of entropy
  and `is_trusted_device()` needs an indexed equality lookup, which bcrypt cannot give.
- `_get_device_name(request)` - User-Agent truncated to 512 chars with non-printable characters
  replaced, because it is attacker-controlled and lands in emails.
- `_email_pool(priority)` / `_dispatch_email(send_fn, priority=False)` / `_log_email_failure` - the
  two lazy bounded pools; inline under pytest; inline fallback at shutdown. See
  [Dispatch](#dispatch-two-bounded-thread-pools-not-the-task-queue). `security_alerts.py` and
  `password_service.py` import `_dispatch_email` directly.
- `set_access_cookie(response, token)` / `set_refresh_cookie(response, token)` - httpOnly, `Secure`
  per `PT_COOKIE_SECURE`, `SameSite` per `PT_COOKIE_SAMESITE`, `max_age` from `SIMPLE_JWT`.
- `is_trusted_device(request, user_id)` - looks up the cookie's hash for that user and bumps
  `last_used_at` via `.update()`.
- `register_device(response, user_id, request)` - `secrets.token_hex(32)`, stores only the hash, name
  and IP, sets `pt_device` (httpOnly, `SameSite=Lax`, `Secure` from `PT_DEVICE_COOKIE_SECURE` read
  directly so a removed setting fails loudly).
- `send_device_otp(user)` - email 1. Generates and commits the OTP synchronously, sends on the priority
  lane. In DEBUG, a failed send prints the code to the console so local dev never dead-ends.
- `send_new_device_notification(user, request)` - email 2 (bulk lane; failure logged at WARNING).
- `notify_admins_new_device_login(user, request)` - email 3 to every other active admin (bulk lane;
  skipped when there is no other admin; failure logged at ERROR). Its body promises a revoke option,
  which `users_views.revoke_user_device` provides.

### apps/services/token_revocation.py

[token_revocation.py](../apps/services/token_revocation.py) - refresh-token revocation and the two
"invalidate everything" levels.

- `revoke_refresh_jti(jti, expires_at)` - idempotent `get_or_create`.
- `is_refresh_jti_revoked(jti)` - existence check.
- `prune_expired_revoked_tokens()` - deletes rows past `expires_at`, returns the count. Shared by the
  management command and `reports_views.trigger_prune_revoked_tokens` (the view does not use
  `call_command()`, because `BaseCommand.execute()` writes a truthy int return to stdout and crashes).
- `revoke_all_tokens(user)` - `token_version = F("token_version") + 1`; kills every access and refresh
  token; keeps device trust. Used by both password-setting paths.
- `revoke_all_sessions(user)` - `revoke_all_tokens()` plus deleting every `TrustedDevice` for the user.
  Used by both "log out everywhere" endpoints.

### apps/services/security_alerts.py

[security_alerts.py](../apps/services/security_alerts.py) - emails 5, 6 and 7. Every function is
best-effort: sends through `_dispatch_email()` (bulk lane) with `fail_silently=False` inside a closure
that logs at ERROR.

- `_admin_emails(exclude_email="")` - active admins' addresses. Also imported by the report modules
  as the admin recipient list.
- `notify_admins_account_locked(user)` - email 5, only from `_register_failed_attempt()` at the moment
  of locking.
- `record_failed_login_and_maybe_alert()` - increments the 5-minute bucket counter and sends email 6
  when it reaches 15, once per bucket (see
  [Alerts, audit log, and logs](#alerts-audit-log-and-logs)). Called for wrong passwords and unknown
  emails.
- `notify_admins_sync_failure(plant_key, cmd_name, detail="")` - email 7 to the two fixed addresses,
  called from `sync_trigger.py`'s pipeline on a failed step (see [data-sync.md](data-sync.md)).

### apps/services/email_service.py

[email_service.py](../apps/services/email_service.py) - `render_email(greeting, body_paragraphs,
highlight_value=None, highlight_label="One-Time Password", after_highlight_paragraphs=None,
closing="Regards,", signature="Ravasco Transmission and Packing Pvt Ltd.")` returns
`(html_body, text_body)`. HTML-escapes every caller string, renders the highlight (the OTP) bold and
letter-spaced or on its own line, and always appends the "system generated" footer. It never sends
mail and knows nothing about SMTP. Callers currently send only `text_body`.

### apps/core/audit_log.py

[audit_log.py](../apps/core/audit_log.py) - lives in `apps/core` but outside the `models/` package.

- `PTAuditLog` (`pt_audit_log`) - `timestamp`, `action` (`login`, `logout`, `user_created`,
  `user_updated`, `user_deleted`, `device_revoked`, `sessions_revoked`), `actor_id` (a plain integer,
  not an FK, so a deleted user's rows survive), `actor_email` (denormalised), `ip_address`, `detail`.
  Indexed on `timestamp`, `action` and `(actor_id, timestamp)`; newest first. Registered read-only in
  `apps/core/admin.py`.
- `log_pt_action(request, action, detail="", actor=None)` - writes one row; `actor` falls back to
  `request.user` (anonymous users get a null `actor_id`); IP via `get_client_ip()`. Swallows and logs
  every exception.

### apps/core/models/auth.py

[models/auth.py](../apps/core/models/auth.py) - import these from `apps.core.models`, never the
submodule.

- `PTUser` (`pt_users`) - `user_id` AutoField PK, unique `email`, bcrypt `password_hash` (never
  serialized, excluded from the Admin form), `full_name`, `role` (`admin`/`editor`/`viewer`, default
  `viewer`), `designation`, `plants` (JSON list, empty = all plants, scopes reads and writes),
  `is_active`, `created_at`, `last_login_at` (written by all three login paths via `.update()`),
  `failed_login_attempts`, `locked_until`, `token_version`. Declares `is_authenticated`/`is_anonymous`.
  Its docstring's claim that `plants` "has no bearing on read access" predates read scoping and is
  stale.
- `OTPCode` (`pt_otp_codes`) - unique `email`, `code_hash`, `expires_at`, `attempts`, `created_at`.
- `RevokedRefreshToken` (`pt_revoked_refresh_tokens`) - unique `jti`, `revoked_at`, `expires_at`.
- `TrustedDevice` (`pt_trusted_devices`) - FK to `PTUser` (cascade), unique `device_token_hash`
  (SHA-256; migration `0015` hashed the previously plaintext tokens in place), `device_name`,
  `ip_address`, `created_at`, `last_used_at`.

### config/middleware.py

[middleware.py](../config/middleware.py) - four middleware classes and one WhiteNoise hook.

- `frontend_cache_headers(headers, path, url)` - `WHITENOISE_ADD_HEADERS_FUNCTION`; `.html/.js/.mjs/
  .css` get `Cache-Control: no-cache, public` so a deploy is picked up on the next load.
- `NoCacheMiddleware` - DEBUG only (inserted at index 1): no-store on everything. It is why dev never
  showed the production caching behaviour `ApiNoStoreMiddleware` fixes.
- `AdminOnlyCsrfMiddleware` - subclasses `CsrfViewMiddleware` and runs its unmodified `process_view`
  only for paths under `/admin/`. `test_security_headers_and_csrf_scope.py` tests `process_view()`
  directly in both directions, because a test through Django Admin's login view passed even with the
  prefix broken (that view has its own `@csrf_protect`).
- `SelectiveGZipMiddleware` - `GZipMiddleware` skipping `UNCOMPRESSED_PATH_PREFIXES =
  ("/api/auth/", "/admin/")` (BREACH). Pinned by `test_response_compression.py`.
- `ApiNoStoreMiddleware` - `Cache-Control: no-store` by `setdefault` on every `/api/` response, so a
  view's own header wins. Pinned by `test_api_no_store.py`.

### config/security_headers.py

[security_headers.py](../config/security_headers.py) - `SecurityHeadersMiddleware` sets the headers
listed in [Deploy-time checks and headers](#deploy-time-checks-and-headers) on every response,
including WhiteNoise's (hence its position). `_build_csp()` assembles the policy and appends
`settings.CSP_EXTRA_DIRECTIVES` when set. HSTS is added only when `DEBUG` is off. The module
docstring records what was moved where to drop `'unsafe-inline'`. The CSP contents are pinned by
`test_security_headers_and_csrf_scope.py`.

### config/settings.py (auth and security parts)

[settings.py](../config/settings.py):

- `SECRET_KEY` from `DJANGO_SECRET_KEY` (insecure dev default); `JWT_SIGNING_KEY =
  os.environ.get("JWT_SIGNING_KEY", SECRET_KEY)`. Because this uses a dict-style default, a
  **present-but-blank** `JWT_SIGNING_KEY=` (as `.env.example` ships it) yields an empty signing key,
  not the `SECRET_KEY` fallback, and W001 does not fire. Set a real value or delete the line.
- `SIMPLE_JWT` - access 12h, refresh 30 days, HS256, rotation + "blacklist" after rotation (served by
  `RevokedRefreshToken`), `USER_ID_FIELD`/`USER_ID_CLAIM` `user_id`, `UPDATE_LAST_LOGIN` False,
  `USER_AUTHENTICATION_RULE` `pt_user_authentication_rule`.
- `REST_FRAMEWORK` - `PTCookieJWTAuthentication`, default `IsAuthenticated`, the shared exception
  handler, JSON-only rendering, default anon/user throttles and the scoped rates listed above.
- Sessions: DB engine, save every request, httpOnly, `Lax`, `Secure` outside DEBUG, 30-minute age.
  Cookies: `PT_COOKIE_NAME = "pt_access"`, `PT_COOKIE_SAMESITE`, `PT_COOKIE_SECURE`,
  `PT_DEVICE_COOKIE_SECURE`, CSRF cookie `Secure`/`Lax` (Admin only).
- `AUTHENTICATION_BACKENDS` - `PTUserBackend`, then `ModelBackend` (Admin superuser only).
- SMTP settings and `EMAIL_TIMEOUT = 10`; `ALLOWED_EMAIL_DOMAIN` (default `ravasco.com`);
  `REPORT_CRON_SECRET`; `MISMATCH_REPORT_PLANT_HEADS_ENABLED`; the Google OAuth client settings
  (separate from the Drive service-account settings).
- Outside DEBUG: `SECURE_PROXY_SSL_HEADER`, `SECURE_SSL_REDIRECT` (forced off under pytest), HSTS one
  year with subdomains and preload. `SILENCED_SYSTEM_CHECKS = ["security.W003"]`.
- `CACHES` - `DatabaseCache` (`pt_cache_table`, needs `createcachetable`), `LocMemCache` under pytest;
  the login-burst counter lives here.

### apps/core/checks.py

[checks.py](../apps/core/checks.py) - `check_jwt_signing_key_is_independent` (W001, skipped in DEBUG)
and `check_no_unexpected_django_superuser` (W002, returns nothing if the DB is not migrated yet).
Registered by `apps/core/apps.py`'s `CoreConfig.ready()` import. Both are `Warning`s, so they only
fail a build under `--fail-level WARNING`. Tested by `test_deploy_checks.py`.

### apps/core/management/commands/create_pt_user.py

[create_pt_user.py](../apps/core/management/commands/create_pt_user.py) -
`manage.py create_pt_user --email ... --password ... [--role admin|editor|viewer] [--full-name]
[--designation]`. The only way to create the first account. Domain check, the same password policy as
the UI (duplicated by hand; `test_password_policy_is_stated_consistently.py` checks they agree),
bcrypt `rounds=12`, then `update_or_create` on the email, so re-running it is also a CLI password reset
or role change and re-activates the account. Unlike the in-app reset, it does **not** bump
`token_version`, so existing sessions survive a CLI reset, and it writes no audit row.

### apps/core/management/commands/prune_revoked_tokens.py

[prune_revoked_tokens.py](../apps/core/management/commands/prune_revoked_tokens.py) -
`manage.py prune_revoked_tokens`, a thin wrapper over `token_revocation.prune_expired_revoked_tokens()`
that prints the count.
