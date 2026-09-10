"""
Regression test for a real TOCTOU (time-of-check-to-time-of-use) race found
during a full-codebase audit (2026-09-10): users_views.py's "can't remove/
deactivate/delete the last active admin" check used to be a plain read
(`PTUser.objects.filter(role=admin, is_active=True).exclude(pk=target).exists()`)
followed by a completely separate `.save()`/`.delete()`, with no locking
tying the two together. With exactly two active admins A and B, two
concurrent requests - one deactivating A while excluding A from its own
count (sees B, passes), the other deactivating B while excluding B (sees A,
passes) - could both succeed, since each transaction's own "exclude self"
query never looks at the same row the other transaction is about to lock.

The fix (users_views.py's `_assert_not_last_active_admin()`) locks EVERY
currently-active admin row via `select_for_update()`, not just "every active
admin other than this one" - so two concurrent callers always contend for
the same lock regardless of which admin each one is excluding.

This test needs `transaction=True` (a real, separate DB connection per
thread, not the single wrapping transaction pytest-django normally uses) -
`select_for_update()`'s row lock is only meaningful across two genuinely
separate connections/transactions, which the default `django_db` fixture's
"wrap everything in one atomic block, roll back at the end" behavior would
not exercise at all.
"""

import threading

import pytest
from django.db import close_old_connections, transaction
from rest_framework.exceptions import ValidationError

from apps.api.routers.users_views import _assert_not_last_active_admin
from apps.api.tests.factories import make_user
from apps.core.models import PTUser


@pytest.mark.django_db(transaction=True)
def test_concurrent_deactivation_of_two_admins_is_serialized_not_a_race():
    admin_a = make_user(email="admin-a@ravasco.com", role="admin")
    admin_b = make_user(email="admin-b@ravasco.com", role="admin")

    barrier = threading.Barrier(2)
    results = {}

    def try_deactivate(target_id):
        # Each thread must run on its own DB connection - Django connections
        # aren't thread-safe to share, and a shared connection would defeat
        # the whole point of testing two genuinely concurrent transactions.
        close_old_connections()
        try:
            with transaction.atomic():
                barrier.wait(timeout=5)  # maximize the chance both threads overlap
                _assert_not_last_active_admin(exclude_pk=target_id)
                # Simulate the real deactivation happening inside the same
                # transaction as the check, same as update_user()'s PATCH
                # branch does.
                PTUser.objects.filter(pk=target_id).update(is_active=False)
            results[target_id] = "succeeded"
        except ValidationError:
            results[target_id] = "rejected"
        finally:
            close_old_connections()

    t1 = threading.Thread(target=try_deactivate, args=(admin_a.user_id,))
    t2 = threading.Thread(target=try_deactivate, args=(admin_b.user_id,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Before the fix, both threads' "exclude self" queries locked
    # non-overlapping rows and could both pass - leaving zero active admins.
    # After the fix, exactly one must be rejected once the rows are
    # correctly serialized.
    outcomes = list(results.values())
    assert outcomes.count("succeeded") == 1, f"expected exactly one success, got {results}"
    assert outcomes.count("rejected") == 1, f"expected exactly one rejection, got {results}"

    remaining_active_admins = PTUser.objects.filter(
        pk__in=[admin_a.user_id, admin_b.user_id], role=PTUser.Role.ADMIN, is_active=True,
    ).count()
    assert remaining_active_admins == 1, "must never end with zero active admins"
