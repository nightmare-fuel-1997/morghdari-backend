"""v1.7.1 — explicit saloon → threshold-template assignment (2026-09-12).

`farms.Saloon.threshold_template` is the manager's "this house, this template"
lever and the FIRST link of the resolution chain (assignment → org+breed
published → org default → global default). It is NULLable and RESTRICT:

  NULL      = inherit the breed/default chain — byte-identical to the behavior
              of every saloon before this column existed, so this migration is
              a no-op for data.
  RESTRICT  = a template is DEPRECATED, never deleted out from under the
              saloons (and the alert history) that point at it.

The alternative design was a new `saloon_threshold_assignments` table. It was
rejected deliberately: a new org-carrying table would have to be documented in
database.md and added to both RLS ARRAY lists (initdb/02_rls.sql and the frozen
tenants/0002) to satisfy scripts/check_rls_parity.py, for a 1:1 relationship
that fits on the row it describes. Assignment history already lives in
account_activity_logs (SALOON_TEMPLATE_ASSIGNED / _CLEARED).

فارسی: اتصالِ صریحِ سالن به قالبِ آستانه — نال یعنی زنجیرهٔ پیش‌فرضِ نژاد؛
RESTRICT یعنی قالب بازنشسته می‌شود ولی هرگز زیرِ پای تاریخچه حذف نمی‌شود.
"""

from django.db import migrations, models
from django.db.models import RESTRICT


class Migration(migrations.Migration):

    dependencies = [
        ("alerts", "0005_lifecycle_template_write_surface"),
        ("farms", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="saloon",
            name="threshold_template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=RESTRICT,
                related_name="assigned_saloons",
                to="alerts.lifecycletemplate",
            ),
        ),
    ]
