# BUG-10 / BUG-18 (2026-09-15): the *_CREATED audit row kept a RESTRICT FK to
# its farm/saloon, so a hard-delete of an API-created (even empty) farm/saloon
# always failed with 409 "still referenced by history" - the delete endpoint
# could never succeed. Switching the two audit FKs to SET_NULL lets the delete
# succeed while the audit row (and its details JSON) survives with a NULL FK.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("auditing", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="accountactivitylog",
            name="farm",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="farms.farm",
            ),
        ),
        migrations.AlterField(
            model_name="accountactivitylog",
            name="saloon",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="farms.saloon",
            ),
        ),
    ]
