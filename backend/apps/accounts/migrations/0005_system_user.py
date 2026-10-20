"""Create the system actor (AD-17, 2026-10-18).

WHY a data migration rather than lazy creation only: production must have the
row before the first automatic transition, and the reconciler runs in a Celery
worker where an on-demand INSERT inside a hot path is a race waiting to happen.
`apps/accounts/system.get_system_user()` still creates it on demand, because the
test suite runs with `--nomigrations` and never executes this file.

Reverse is a deliberate no-op: `provisioning_audit_log.changed_by` is
`ON DELETE RESTRICT`, so deleting the actor after it has been used would fail
(and silently leaving it is correct — the audit rows still name it).
"""

from django.contrib.auth.hashers import make_password
from django.db import migrations

SYSTEM_USERNAME = "system@morghdari"
SYSTEM_EMAIL = "system@morghdari.invalid"


def create_system_user(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    user, created = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={
            "email": SYSTEM_EMAIL,
            "first_name": "System",
            "last_name": "Automation",
            "is_active": False,
            "is_staff": False,
            "is_superuser": False,
            "is_platform_staff": False,
        },
    )
    if created:
        # Unusable password: this account must never authenticate.
        #
        # NOT `user.set_unusable_password()`: inside a migration
        # `apps.get_model()` returns a HISTORICAL model rendered from the
        # migration state, which is a plain Model subclass — it carries the
        # fields but none of AbstractUser's methods, so that call raises
        # AttributeError. `make_password(None)` produces the same `!`-prefixed
        # unusable hash and only needs the field.
        #
        # This went unnoticed because the test suite runs `--nomigrations`, so
        # this file had never actually executed anywhere. The production-broker
        # rehearsal (2026-10-20) was the first real `migrate` and it failed here.
        user.password = make_password(None)
        user.save(update_fields=["password"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_platformstaffmfa"),
    ]

    operations = [
        migrations.RunPython(create_system_user, migrations.RunPython.noop),
    ]
