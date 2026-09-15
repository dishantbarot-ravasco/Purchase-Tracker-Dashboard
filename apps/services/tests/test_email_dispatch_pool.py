"""
Tests for the bounded email dispatch pools (2026-09-15 audit follow-up).

THE GAP
-------
`_dispatch_email()` ran `threading.Thread(target=send_fn, daemon=False).start()`
per message - a fresh OS thread for every email, with nothing capping how many
could exist at once. A single login sends three (OTP, new-device notice, admin
alert), so twenty colleagues arriving at 9am was sixty threads, each holding an
SMTP socket for up to EMAIL_TIMEOUT seconds.

`password_service.send_password_change_otp()` was worse: it bypassed the shared
helper entirely and spawned its own `daemon=True` thread, so a worker restart
mid-send silently killed that OTP - the exact failure the shared helper's
non-daemon behaviour exists to prevent, and which the *login* OTP was already
protected from. Two OTP emails in one app with two different shutdown
behaviours, chosen by nobody.

THE FIX
-------
Two bounded ThreadPoolExecutors. Two rather than one because an OTP must never
queue behind a backlog of admin alerts: the whole reason this work stayed OFF
django-q2 was to protect OTP latency (queuing it would make login depend on
the qcluster worker being alive), and letting it queue behind bulk mail
in-process would hand that latency straight back.

WHAT THESE TESTS LOCK IN
------------------------
The bound itself, the lane separation under load, the preserved
inline-under-pytest behaviour, the shutdown fallback, and that
password_service is actually routed through the shared helper on the priority
lane. The load tests deliberately drive real threads by removing the "pytest"
marker from sys.modules - otherwise `_dispatch_email` takes its inline branch
and the pool under test is never exercised at all.
"""

import sys
import threading
import time
from unittest.mock import patch

import pytest

from apps.services import device_service as ds


@pytest.fixture
def production_dispatch():
    """Make `_dispatch_email` take its real (pooled) branch instead of the
    inline-under-pytest one, and tear the pools down afterwards so no test
    leaks worker threads into the rest of the suite."""
    had_pytest = sys.modules.pop("pytest", None)
    ds._otp_pool = None
    ds._bulk_pool = None
    try:
        yield
    finally:
        for pool in (ds._otp_pool, ds._bulk_pool):
            if pool is not None:
                pool.shutdown(wait=True)
        ds._otp_pool = None
        ds._bulk_pool = None
        if had_pytest is not None:
            sys.modules["pytest"] = had_pytest


class TestInlineUnderPytest:
    """The inline branch is load-bearing and must not regress: without it,
    every existing test that asserts against mail.outbox right after a request
    becomes a race."""

    def test_dispatch_runs_inline_when_pytest_is_loaded(self):
        ran_on = []
        ds._dispatch_email(lambda: ran_on.append(threading.current_thread().name))
        assert ran_on == [threading.current_thread().name], (
            "under pytest the send must run synchronously on the calling thread"
        )

    def test_priority_also_runs_inline(self):
        ran = []
        ds._dispatch_email(lambda: ran.append(1), priority=True)
        assert ran == [1]


class TestPoolIsBounded:
    def test_many_emails_do_not_create_many_threads(self, production_dispatch):
        """The actual defect: 40 emails used to mean 40 OS threads."""
        done = threading.Event()
        counter = {"n": 0}
        lock = threading.Lock()

        def work():
            with lock:
                counter["n"] += 1
                if counter["n"] == 40:
                    done.set()
            time.sleep(0.01)

        before = threading.active_count()
        for _ in range(20):
            ds._dispatch_email(work, priority=False)
        for _ in range(20):
            ds._dispatch_email(work, priority=True)
        peak = threading.active_count()

        assert done.wait(timeout=10), "not every queued email ran"
        cap = ds._OTP_POOL_WORKERS + ds._BULK_POOL_WORKERS
        assert peak - before <= cap, (
            f"dispatching 40 emails created {peak - before} threads; the pools "
            f"cap it at {cap}. The bound is the whole point of this change."
        )

    def test_every_queued_email_still_runs(self, production_dispatch):
        """Bounding must queue work, never drop it - a dropped OTP is a user
        who cannot log in."""
        seen = []
        lock = threading.Lock()

        def work():
            with lock:
                seen.append(1)

        for _ in range(25):
            ds._dispatch_email(work)
        ds._bulk_pool.shutdown(wait=True)
        ds._bulk_pool = None
        assert len(seen) == 25


class TestLaneSeparation:
    def test_otp_is_not_delayed_by_a_jammed_bulk_lane(self, production_dispatch):
        """The reason there are two pools rather than one.

        With a single shared lane, an OTP queued behind 40 stalled sends waits
        (40 / workers) * timeout seconds. A user staring at a code-entry screen
        does not have that long.
        """
        slow_seconds = 0.3
        released = threading.Event()
        otp_latency = []
        start = time.perf_counter()

        def slow():
            released.wait(timeout=5)

        def otp():
            otp_latency.append(time.perf_counter() - start)

        try:
            for _ in range(40):
                ds._dispatch_email(slow, priority=False)
            for _ in range(3):
                ds._dispatch_email(otp, priority=True)

            deadline = time.perf_counter() + 5
            while len(otp_latency) < 3 and time.perf_counter() < deadline:
                time.sleep(0.01)
        finally:
            released.set()

        assert len(otp_latency) == 3, "priority emails never ran"
        assert max(otp_latency) < slow_seconds, (
            f"an OTP waited {max(otp_latency):.2f}s behind bulk mail - the "
            f"priority lane is not isolated"
        )

    def test_the_two_lanes_use_different_threads(self, production_dispatch):
        names = {"otp": set(), "bulk": set()}
        lock = threading.Lock()

        def tag(kind):
            def _f():
                with lock:
                    names[kind].add(threading.current_thread().name)
                time.sleep(0.01)
            return _f

        for _ in range(8):
            ds._dispatch_email(tag("bulk"), priority=False)
            ds._dispatch_email(tag("otp"), priority=True)
        for pool in (ds._otp_pool, ds._bulk_pool):
            pool.shutdown(wait=True)
        ds._otp_pool = ds._bulk_pool = None

        assert names["otp"] and names["bulk"]
        assert not (names["otp"] & names["bulk"]), "the lanes shared a thread"
        assert all("pt-email-otp" in n for n in names["otp"])
        assert all("pt-email-bulk" in n for n in names["bulk"])


class TestShutdownFallback:
    def test_a_refused_submit_sends_inline_rather_than_dropping(self, production_dispatch):
        """At interpreter shutdown the pool refuses new work. Dropping the
        message there would mean a user holding a code that was never sent, so
        the fallback sends inline instead."""
        ran = []

        class _RefusingPool:
            def submit(self, fn):
                raise RuntimeError("cannot schedule new futures after shutdown")

        with patch.object(ds, "_email_pool", return_value=_RefusingPool()):
            ds._dispatch_email(lambda: ran.append("sent"), priority=True)

        assert ran == ["sent"], "the message was dropped instead of sent inline"

    def test_an_exception_in_the_fallback_does_not_propagate(self, production_dispatch):
        """A failed send must never bubble into the request that triggered it -
        losing a notification email is not a reason to fail someone's login."""
        class _RefusingPool:
            def submit(self, fn):
                raise RuntimeError("shutting down")

        def boom():
            raise ValueError("smtp exploded")

        with patch.object(ds, "_email_pool", return_value=_RefusingPool()):
            ds._dispatch_email(boom)  # must not raise


class TestPasswordServiceUsesTheSharedDispatcher:
    """password_service used to spawn its own daemon=True thread, bypassing
    all of the above."""

    def test_it_does_not_spawn_its_own_thread(self):
        import inspect

        from apps.services import password_service

        source = inspect.getsource(password_service)
        assert "threading.Thread" not in source, (
            "password_service is spawning its own thread again - it must route "
            "through device_service._dispatch_email() so it inherits the "
            "bound, the non-daemon shutdown behaviour, and the "
            "inline-under-pytest branch"
        )

    @pytest.mark.django_db
    def test_the_password_otp_goes_out_on_the_priority_lane(self):
        from apps.api.tests.factories import make_user
        from apps.services import password_service

        user = make_user(email="pool-priority@ravasco.com")
        # Patch where the name is USED, not where it is defined:
        # password_service does `from ...device_service import _dispatch_email`,
        # which binds the function into its own module namespace at import
        # time, so patching device_service's attribute would not affect it.
        with patch.object(password_service, "_dispatch_email") as dispatch:
            password_service.send_password_change_otp(user)

        assert dispatch.called, "the OTP email was never dispatched"
        assert dispatch.call_args.kwargs.get("priority") is True, (
            "a password-change OTP is a code a user is waiting on - it belongs "
            "on the priority lane, like the login OTP"
        )

    @pytest.mark.django_db
    def test_the_password_otp_is_actually_delivered(self):
        """End-to-end through the real (inline, under pytest) path."""
        from django.core import mail

        from apps.api.tests.factories import make_user
        from apps.services import password_service

        user = make_user(email="pool-delivery@ravasco.com")
        mail.outbox.clear()
        password_service.send_password_change_otp(user)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [user.email]
