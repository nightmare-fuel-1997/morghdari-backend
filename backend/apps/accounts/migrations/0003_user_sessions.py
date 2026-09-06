# M6-10 (contract v1.7.0): user_sessions — the per-refresh-token register
# behind O11/O12 (spec §7 design; see UserSession docstring for WHY a table
# instead of the never-installed token_blacklist app).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_user_staff_role"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserSession",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("refresh_jti", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(db_default=models.functions.Now(), null=True)),
                ("last_seen_at", models.DateTimeField(db_default=models.functions.Now(), null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=400, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    django.db.models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="+",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "db_table": "user_sessions",
                "indexes": [models.Index(fields=["user", "revoked_at"], name="idx_user_sessions_user")],
            },
        ),
    ]
