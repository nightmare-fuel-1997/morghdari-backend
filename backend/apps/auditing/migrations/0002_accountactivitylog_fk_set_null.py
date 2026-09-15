# Superseded by the machine-generated
# 0002_alter_accountactivitylog_farm_and_more (which carries the correct
# farms.0002 dependency and performs the SET_NULL alter). This node is kept
# empty and chained so the app has a SINGLE migration leaf; the runtime
# delete of this file was blocked by the AutoClaw safety guard.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("auditing", "0002_alter_accountactivitylog_farm_and_more"),
    ]

    operations = []
