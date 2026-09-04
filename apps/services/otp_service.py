"""
apps/services/otp_service.py — PostgreSQL-backed OTP store with TTL.

Ported from the TDS Automation App's apps/services/otp_service.py (unchanged
design, PTUser's `pt_otp_codes` table instead of TDS's `otp_codes`).

Security design
----------------
- OTP codes are 6-digit strings generated with secrets.randbelow (CSPRNG).
- Only the bcrypt HASH of the code is stored in the DB - never the
  plaintext. Even direct DB access cannot reveal a valid code.
- One active OTP per email: `email` is a unique DB column and generate_otp()
  upserts it in a single atomic update_or_create(), so concurrent requests
  for the same address can't create duplicate rows.
- expires_at is a timezone-aware UTC datetime, enforced in Python before
  attempting bcrypt verification.
- Attempt counter increments on each wrong guess; at MAX_ATTEMPTS the row is
  deleted.
- On successful verification the row is deleted immediately (single-use).
- Expired rows for other emails are pruned opportunistically on each
  generate call.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

import bcrypt
from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)

_OTP_TTL_MINUTES = 10
_MAX_ATTEMPTS = 5


def _hash_code(code: str) -> str:
    return bcrypt.hashpw(code.encode(), bcrypt.gensalt(rounds=10)).decode()


def _check_code(code: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(code.encode(), hashed.encode())
    except Exception:
        return False


def generate_otp(email: str) -> str:
    """Generate a 6-digit OTP, persist its hash to the DB, and return the
    plaintext code (which gets emailed to the user)."""
    from apps.core.models import OTPCode

    now = timezone.now()
    code = str(secrets.randbelow(1_000_000)).zfill(6)

    key = email.lower()

    OTPCode.objects.filter(expires_at__lt=now).delete()

    with transaction.atomic():
        # update_or_create is a single atomic upsert keyed on the unique
        # `email` column, so two concurrent requests for the same address
        # can't both insert a row (the failure mode a prior delete-then-
        # create pattern was vulnerable to - see verify_otp()'s .get()).
        OTPCode.objects.update_or_create(
            email=key,
            defaults={
                "code_hash": _hash_code(code),
                "expires_at": now + timedelta(minutes=_OTP_TTL_MINUTES),
                "attempts": 0,
            },
        )

    log.info("OTP generated for %s (expires in %d min)", email, _OTP_TTL_MINUTES)
    return code


def verify_otp(email: str, code: str) -> bool:
    """True if `code` matches the stored hash for `email` and the OTP has
    not expired, been used, or exceeded the attempt limit.

    On success -> row is deleted (single-use).
    On failure -> attempt counter incremented; row deleted at MAX_ATTEMPTS.
    """
    from apps.core.models import OTPCode

    key = email.strip().lower()

    try:
        entry = OTPCode.objects.get(email=key)
    except OTPCode.DoesNotExist:
        log.debug("verify_otp: no OTP found for %s", key)
        return False

    now = timezone.now()

    if now > entry.expires_at:
        entry.delete()
        log.debug("verify_otp: OTP expired for %s", key)
        return False

    entry.attempts += 1

    if entry.attempts > _MAX_ATTEMPTS:
        entry.delete()
        log.warning("verify_otp: too many attempts for %s - OTP invalidated", key)
        return False

    if not _check_code(code.strip(), entry.code_hash):
        entry.save(update_fields=["attempts"])
        log.debug("verify_otp: wrong code for %s (attempt %d)", key, entry.attempts)
        return False

    entry.delete()
    log.info("verify_otp: success for %s", key)
    return True
