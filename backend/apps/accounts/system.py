"""The system actor — for lifecycle transitions no human triggered (AD-17).

WHY this exists: removing farm-manager approval (AD-17) means the slot reaches
`PROVISIONED` automatically, when the gateway reports `applied == desired`.
`provisioning_audit_log.changed_by` is a NOT NULL FK
(`apps/provisioning/models.py`), so an automatic transition still needs an
actor. Making the column nullable would hollow out the audit trail — every row
must name someone — so the actor is a real, non-login user row instead.

The row is created by a data migration (accounts/0005_system_user) so production
has it even if the helper is never called. `get_system_user()` also creates it
on demand because the test suite runs with `--nomigrations`, where data
migrations never execute.

فارسی: کاربرِ سیستمی برای گذارهای خودکار؛ ستونِ تغییردهنده تهی نمی‌شود چون
ارزشِ لاگِ ممیزی در نام‌بردنِ عاملِ هر تغییر است.
"""

SYSTEM_USERNAME = "system@morghdari"
SYSTEM_EMAIL = "system@morghdari.invalid"  # .invalid is reserved (RFC 2606)


def get_system_user():
    """Return the system actor, creating it if absent (idempotent)."""
    from apps.accounts.models import User

    user, created = User.objects.get_or_create(
        username=SYSTEM_USERNAME,
        defaults={
            "email": SYSTEM_EMAIL,
            "first_name": "System",
            "last_name": "Automation",
            # Never a login: unusable password + inactive. is_platform_staff
            # stays False so it can never satisfy a platform-staff gate.
            "is_active": False,
            "is_staff": False,
            "is_superuser": False,
            "is_platform_staff": False,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user
