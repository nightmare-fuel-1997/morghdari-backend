"""
C-10 resolution (owner decision 2026-08-24): alert_acknowledgements gains
org_id like every tenant table — doc-first change in database.md Compartment 4.

فارسی: ستون org_id به تأییدیه‌ها اضافه می‌شود؛ ابتدا nullable، سپس پرکردن از
سازمانِ خودِ هشدار، و در نهایت اجباری‌سازی — الگوی استاندارد افزودن FK غیرقابل-
null که روی هر حالتی از دیتابیس قابل بازپخش است.

Three steps because a NOT-NULL FK cannot be added in one move on a table that
may already hold rows: AddField(nullable) → RunSQL backfill (each ack inherits
its alert's org) → AlterField(NOT NULL).
"""

from django.db import migrations, models
from django.db.models import RESTRICT


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0003_maintenancemute_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertacknowledgement",
            name="org",
            field=models.ForeignKey(
                null=True,
                on_delete=RESTRICT,
                related_name="+",
                to="tenants.organization",
            ),
        ),
        migrations.RunSQL(
            sql=(
                "UPDATE alert_acknowledgements a SET org_id = ai.org_id "
                "FROM alert_instances ai WHERE a.alert_id = ai.id;"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="alertacknowledgement",
            name="org",
            field=models.ForeignKey(
                on_delete=RESTRICT,
                related_name="+",
                to="tenants.organization",
            ),
        ),
    ]
