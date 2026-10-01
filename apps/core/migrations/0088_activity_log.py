"""
The activity log (2026-10-01): PTAuditLog grows from sign-ins only to every
change, download, page visit and refused sign-in - see apps/core/audit_log.py
and apps/services/activity_log.py. Existing rows keep their values; every new
column is blank or null for them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0087_document'),
    ]

    operations = [
        migrations.AddField(
            model_name='ptauditlog',
            name='duration_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='method',
            field=models.CharField(blank=True, max_length=8),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='path',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='payload',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='plant',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='route',
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='status_code',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ptauditlog',
            name='user_agent',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AlterField(
            model_name='ptauditlog',
            name='action',
            field=models.CharField(choices=[('login', 'Login'), ('logout', 'Logout'), ('user_created', 'User created'), ('user_updated', 'User updated'), ('user_deleted', 'User deleted'), ('device_revoked', 'Trusted device revoked'), ('sessions_revoked', 'All sessions revoked (log out everywhere)'), ('auth_failed', 'Sign-in refused'), ('change', 'Change'), ('download', 'Download'), ('page_view', 'Page visit')], db_index=True, max_length=32),
        ),
        migrations.AddIndex(
            model_name='ptauditlog',
            index=models.Index(fields=['action', 'timestamp'], name='pt_audit_lo_action_67d4bb_idx'),
        ),
    ]
