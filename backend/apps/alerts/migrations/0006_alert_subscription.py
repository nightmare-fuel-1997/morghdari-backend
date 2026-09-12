"""AlertSubscription — org-wide category mutes (v0.9 M9-10/B2, GS-ROUTING).

Managed table (real DDL on all vendors — syncdb creates it under
--nomigrations). The FORCE-RLS tenant_isolation policy comes from
initdb/02_rls.sql (ARRAY entry added in the same wave — the house path for
managed tenant tables, same as maintenance_mutes/alerts-0003): the file runs
at cluster bootstrap, is re-runnable post-migrate by ops, and runs in the
test fixtures via seeded_rls_db.
"""

from django.db import migrations, models
from django.db.models import RESTRICT


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0005_lifecycle_template_write_surface"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlertSubscription",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("metric_key", models.CharField(max_length=20)),
                ("muted_at", models.DateTimeField(db_default=models.functions.Now())),
                ("reason", models.TextField(blank=True, null=True)),
                ("muted_by", models.ForeignKey(on_delete=RESTRICT, related_name="+", to="accounts.user")),
                ("org", models.ForeignKey(on_delete=RESTRICT, related_name="+", to="tenants.organization")),
            ],
            options={
                "db_table": "alert_subscriptions",
            },
        ),
        migrations.AddConstraint(
            model_name="alertsubscription",
            constraint=models.UniqueConstraint(fields=("org", "metric_key"), name="uq_alert_subscriptions_org_metric"),
        ),
    ]
