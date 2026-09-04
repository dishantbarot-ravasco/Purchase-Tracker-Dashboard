"""
Create or update a PTUser account (bcrypt-hashes the password). This is the
only way to create the first admin account - without at least one PTUser,
nobody can log in to create more via Django Admin (/admin/).

Usage:
    python manage.py create_pt_user --email dishant.barot@ravasco.com --password '...' --role admin
    python manage.py create_pt_user --email someone@ravasco.com --password '...' --role viewer --full-name "Someone"

Re-running against an existing email updates that user's password/role/
full_name/designation instead of erroring - convenient for resetting a
forgotten password or promoting a viewer to editor/admin without needing DB
shell access.
"""

import bcrypt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.api.permissions import is_allowed_email_domain
from apps.core.models import PTUser


class Command(BaseCommand):
    help = "Create or update a PTUser account (bcrypt-hashes the password)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--role", choices=[c[0] for c in PTUser.Role.choices], default=PTUser.Role.VIEWER)
        parser.add_argument("--full-name", default="")
        parser.add_argument("--designation", default="")

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        if not is_allowed_email_domain(email):
            raise CommandError(
                f"'{email}' is outside the allowed domain (@{settings.ALLOWED_EMAIL_DOMAIN}) - refusing to create it."
            )

        password_hash = bcrypt.hashpw(options["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

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
