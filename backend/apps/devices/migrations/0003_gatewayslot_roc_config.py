# Generated manually to match Django 5.1 schema for GatewaySlot.roc_config

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0002_phase0_devices_provisioning"),
    ]

    operations = [
        migrations.AddField(
            model_name="gatewayslot",
            name="roc_config",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
