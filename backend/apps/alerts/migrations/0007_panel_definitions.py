"""Granular panels — v0.9 M9-08/B3 (D24-2, closes the §4n panels scope).

Three tables: panel_definitions (GLOBAL catalog, seeded below) +
role_panel_permissions (global per-role gates) + user_panel_overrides
(org-scoped, FORCE-RLS via initdb/02_rls.sql ARRAY — house path for managed
tenant tables, same as alert_subscriptions/alerts-0006).

Seed: the 8-panel catalog (admin is owner_only). ON CONFLICT DO NOTHING —
safe on re-run (metric-seed precedent, telemetry/0003).
"""

from django.db import migrations, models
from django.db.models import CASCADE, RESTRICT

SEED = """
INSERT INTO panel_definitions (panel_id, title, default_open, owner_only) VALUES
    ('dashboard',    'Overview dashboard',  true,  false),
    ('telemetry',    'Live telemetry',      true,  false),
    ('alerts',       'Alerts',              true,  false),
    ('flocks',       'Flocks and KPIs',     true,  false),
    ('reports',      'Daily reports',       true,  false),
    ('provisioning', 'Provisioning',        true,  false),
    ('members',      'Members',             true,  false),
    ('admin',        'Administration',      false, true)
ON CONFLICT (panel_id) DO NOTHING;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0006_alert_subscription"),
    ]

    operations = [
        migrations.CreateModel(
            name="PanelDefinition",
            fields=[
                ("panel_id", models.CharField(max_length=50, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=100)),
                ("default_open", models.BooleanField(default=True)),
                ("owner_only", models.BooleanField(default=False)),
            ],
            options={"db_table": "panel_definitions"},
        ),
        migrations.CreateModel(
            name="RolePanelPermission",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("role", models.CharField(max_length=30)),
                ("allowed", models.BooleanField()),
                ("panel", models.ForeignKey(on_delete=CASCADE, related_name="+", to="alerts.paneldefinition")),
            ],
            options={"db_table": "role_panel_permissions"},
        ),
        migrations.CreateModel(
            name="UserPanelOverride",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("allowed", models.BooleanField()),
                ("org", models.ForeignKey(on_delete=RESTRICT, related_name="+", to="tenants.organization")),
                ("panel", models.ForeignKey(on_delete=CASCADE, related_name="+", to="alerts.paneldefinition")),
                ("user", models.ForeignKey(on_delete=CASCADE, related_name="+", to="accounts.user")),
            ],
            options={"db_table": "user_panel_overrides"},
        ),
        migrations.AddConstraint(
            model_name="rolepanelpermission",
            constraint=models.UniqueConstraint(fields=("panel", "role"), name="uq_role_panel"),
        ),
        migrations.AddConstraint(
            model_name="userpaneloverride",
            constraint=models.UniqueConstraint(fields=("panel", "user"), name="uq_user_panel_override"),
        ),
        migrations.RunSQL(sql=SEED, reverse_sql=migrations.RunSQL.noop),
    ]
