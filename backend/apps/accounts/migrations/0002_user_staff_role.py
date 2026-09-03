# M5-02: the platform review role lives on the user (TASKS M5-02 default =
# admin/support); `_can_review` reads it from apps/provisioning/api.py.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="staff_role",
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
    ]