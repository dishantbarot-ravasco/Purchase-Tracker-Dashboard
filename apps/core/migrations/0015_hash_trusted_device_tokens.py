"""Security fix, 2026-09-04: TrustedDevice.device_token used to store the
raw plaintext device-trust token; renamed to device_token_hash and every
existing row's already-known plaintext value is hashed (SHA-256) in place -
see TrustedDevice's own docstring in apps/core/models.py for the full
reasoning. Existing trusted devices are NOT invalidated by this migration -
their plaintext token is still known at migration time, so it's hashed
in place rather than discarded, and every real request already used
hashlib.sha256 (see apps/services/device_service.py) by the time this
migration is deployed.
"""
import hashlib

from django.db import migrations, models


def hash_existing_tokens(apps, schema_editor):
    TrustedDevice = apps.get_model("core", "TrustedDevice")
    for device in TrustedDevice.objects.all():
        # At this point in the migration, device_token_hash still holds the
        # OLD column's raw plaintext value (RenameField only renames the
        # column, it never touches the stored bytes) - hash it in place.
        plaintext = device.device_token_hash
        device.device_token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        device.save(update_fields=["device_token_hash"])


def unhash_is_not_possible(apps, schema_editor):
    # Deliberately a no-op, not a real reversal: a SHA-256 hash cannot be
    # turned back into the original plaintext token. Reversing this
    # migration leaves every TrustedDevice row's value as a hash under the
    # old device_token column name - every existing trusted device would
    # need to re-verify via OTP once, the same one-time cost as if the rows
    # had been deleted outright. Accepted as the correct trade-off for a
    # migration that can't be losslessly reversed by construction.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_flagdismissal"),
    ]

    operations = [
        migrations.RenameField(
            model_name="trusteddevice",
            old_name="device_token",
            new_name="device_token_hash",
        ),
        migrations.RunPython(hash_existing_tokens, unhash_is_not_possible),
    ]
