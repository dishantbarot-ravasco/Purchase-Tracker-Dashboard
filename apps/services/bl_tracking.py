"""
apps/services/bl_tracking.py - live shipment lookup by Bill of Lading (BL)
number, backing the Import Purchases page's "Track" links next to a PO's BL
Number. Calls SafeCube's (Sinay's) public Container Tracking API v2 directly
- confirmed live against a real trial key and real BL numbers from this
app's own synced data 2026-09-07 (see track_bl()'s docstring for what that
confirmed).

This is a thin, single-call passthrough, not a persistence layer - nothing
here is stored in the DB. A successful lookup is kept in Django's cache for
_CACHE_SECONDS, keyed on the BL number: the call is synchronous and can hold
one of production's two gunicorn workers for the whole timeout, so a second
"Track" click on the same shipment should not pay for it again. Failures are
never cached (SafeCube downtime should clear on the next click). The view
also has its own throttle (permissions.BlTrackThrottle).
"""

import logging

import hashlib

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

_SHIPMENT_URL = "https://api.sinay.ai/container-tracking/api/v2/shipment"
_TIMEOUT_SECONDS = 15
_CACHE_SECONDS = 600


def _cache_key(bl_number: str) -> str:
    return "bl_tracking:" + hashlib.sha256(bl_number.upper().encode("utf-8")).hexdigest()


def track_bl(bl_number: str) -> dict:
    """Looks up live shipment status for a Bill of Lading number. Returns
    {"ok": True, "data": <raw SafeCube response dict>} on success, or
    {"ok": False, "error": "<human-readable message>"} otherwise - never
    raises, since a tracking lookup failing (unconfigured key, SafeCube
    downtime, or - very common in practice - a real BL number SafeCube's
    170+ carrier coverage simply doesn't have data for, e.g. an older or
    already-completed shipment) should surface as a clean message in the
    "Track" popup, not break the page.

    Confirmed live 2026-09-07 against 5 real BL numbers from this app's own
    synced Vapi import PO data: 1 resolved with full live tracking data (no
    sealine needed - SafeCube auto-detected it from the BL number's own
    carrier-prefix pattern), the other 4 returned SafeCube's own structured
    "can't auto-detect"/"can't find this document" errors - a real,
    expected coverage gap (not every BL stays trackable indefinitely, and
    not every carrier is covered), not a bug in this integration. Passing an
    explicit `sealine` (SCAC code) themselves wasn't attempted here - this
    app has no per-carrier SCAC mapping for Vapi's own vendors, and
    auto-detection already covers the case that matters most (a fresh,
    still-in-transit shipment)."""
    api_key = settings.SAFECUBE_API_KEY
    if not api_key:
        return {"ok": False, "error": "BL tracking is not configured (no SAFECUBE_API_KEY set)."}

    bl_number = (bl_number or "").strip()
    if not bl_number:
        return {"ok": False, "error": "No BL number to track."}

    cached = cache.get(_cache_key(bl_number))
    if cached is not None:
        return {"ok": True, "data": cached}

    try:
        resp = requests.get(
            _SHIPMENT_URL,
            params={"shipmentNumber": bl_number, "shipmentType": "BL"},
            headers={"API_KEY": api_key},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("bl_tracking.track_bl: request to SafeCube failed for %r: %s", bl_number, exc)
        return {"ok": False, "error": "Could not reach the tracking service - try again shortly."}

    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        payload = {}

    if resp.status_code != 200:
        message = payload.get("message") or f"HTTP {resp.status_code}"
        details = payload.get("details") or []
        if details:
            message = message + " - " + "; ".join(str(d) for d in details)
        return {"ok": False, "error": message}

    cache.set(_cache_key(bl_number), payload, _CACHE_SECONDS)
    return {"ok": True, "data": payload}
