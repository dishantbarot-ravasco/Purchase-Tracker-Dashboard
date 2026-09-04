"""
apps/core/management/commands/create_pt_user.py — create or update a PTUser
account (bcrypt-hashes the password).

This is the only way to create the first admin account: without at least one
PTUser row, nobody can log in, and logging in is the only way to reach the
in-app admin Users panel (or Django Admin) to create further accounts — a
bootstrap chicken-and-egg problem this command exists solely to break.

Deliberately idempotent via update_or_create() keyed on email, rather than
erroring on an existing address: re-running it against an existing email
updates that user's password/role/full_name/designation in place instead of
raising an IntegrityError. That makes it double as a password-reset / role-
promotion tool for an admin with shell access, not just a first-run bootstrap
step — no separate "reset password" command was needed.

Usage:
    python manage.py create_pt_user --email dishant.barot@ravasco.com --password '...' --role admin
    python manage.py create_pt_user --email someone@ravasco.com --password '...' --role viewer --full-name "Someone"
"""

import bcrypt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.api.permissions import is_allowed_email_domain
from apps.core.models import PTUser


class Command(BaseCommand):
    """Create or update a PTUser account (bcrypt-hashes the password).

    Idempotent by design (see module docstring): the same command that
    bootstraps the first admin also serves as the password-reset / role-
    change path for any existing PTUser, keyed on --email.
    """

    help = "Create or update a PTUser account (bcrypt-hashes the password)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--role", choices=[c[0] for c in PTUser.Role.choices], default=PTUser.Role.VIEWER)
        parser.add_argument("--full-name", default="")
        parser.add_argument("--designation", default="")

    def handle(self, *args, **options):
        """Validate the email domain, hash the password, then upsert the PTUser row."""
        email = options["email"].strip().lower()
        if not is_allowed_email_domain(email):
            raise CommandError(
                f"'{email}' is outside the allowed domain (@{settings.ALLOWED_EMAIL_DOMAIN}) - refusing to create it."
            )

        # Never store the plaintext password - only the bcrypt hash goes to the DB.
        password_hash = bcrypt.hashpw(options["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        # update_or_create(): create the row if this is the first time we've seen
        # this email, otherwise overwrite the fields below on the existing row -
        # this is what makes re-running the command a password reset instead of
        # an error (see module docstring).
        user, created = PTUser.objects.update_or_create(
            email=email,
            defaults=dict(
                password_hash=password_hash,
                role=options["role"],
                full_name=options["full_name"] or None,
                designation=options["designation"] or None,
                is_active=True,
            ),
        )

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} PTUser {user.email} (role={user.role})"))
