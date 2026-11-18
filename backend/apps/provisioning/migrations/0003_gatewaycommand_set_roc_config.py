# Generated manually to allow SET_ROC_CONFIG in ck_gateway_commands_kind

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("provisioning", "0002_phase0_devices_provisioning"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="gatewaycommand",
            name="ck_gateway_commands_kind",
        ),
        migrations.AddConstraint(
            model_name="gatewaycommand",
            constraint=models.CheckConstraint(
                condition=models.Q((
                    "kind__in",
                    [
                        "PROVISION",
                        "ROTATE_CREDENTIAL",
                        "DIAGNOSTICS_REQUEST",
                        "REBOOT",
                        "REFRESH_CONFIG",
                        "SET_ROC_CONFIG",
                    ],
                )),
                name="ck_gateway_commands_kind",
            ),
        ),
    ]
