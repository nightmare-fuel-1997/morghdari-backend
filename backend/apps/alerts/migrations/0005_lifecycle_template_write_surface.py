"""v1.7.1 — the threshold-template WRITE surface (owner decision 2026-09-12).

Until now `lifecycle_templates` was seeded-only and the ops contract said so
out loud (O4: "no write surface for templates"). This migration adds the four
columns the draft → publish → assign flow needs, plus the two ambiguity guards
that make resolution deterministic the moment orgs own templates.

  status        DRAFT | PUBLISHED. Default PUBLISHED, so rows that predate the
                column — and the org-NULL built-in catalog — keep arming alarms
                exactly as before. A DRAFT is invisible to resolution by
                construction; that is what makes an engineer's draft safe.
  farm          the farm a draft belongs to (NULL = org-wide). Scope anchor for
                the farm-scoped engineer gate (fail-closed).
  created_by    author of the draft — the audit trail beside
                account_activity_logs' THRESHOLD_TEMPLATE_* rows.
  published_at  when a manager published it.

  uq_lifecycle_templates_org_breed    at most one PUBLISHED org template per
                                      breed — the resolver's org+breed step
                                      must not be a coin toss.
  uq_lifecycle_templates_org_default  at most one PUBLISHED org default.

Both constraints are org-scoped AND PUBLISHED-only: drafts stay unconstrained,
and the org-NULL built-in catalog is never in scope. On the rehearsal/prod
database the only pre-existing row is the org-NULL built-in template, so both
constraints are satisfied at ADD CONSTRAINT time with no data rewrite.

فارسی: سطحِ نوشتنِ قالبِ آستانه — پیش‌نویس/انتشار/تخصیص. ستون status پیش‌فرض
PUBLISHED دارد تا ردیف‌های قبلی و کاتالوگِ داخلی دست‌نخورده بمانند؛ قیدهای
یکتایی فقط روی ردیف‌های سازمان‌دارِ منتشرشده اعمال می‌شوند.
"""

from django.db import migrations, models
from django.db.models import RESTRICT


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("alerts", "0004_alertacknowledgement_org"),
        ("farms", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="lifecycletemplate",
            name="status",
            field=models.CharField(
                choices=[("DRAFT", "Draft"), ("PUBLISHED", "Published")],
                default="PUBLISHED",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="lifecycletemplate",
            name="farm",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=RESTRICT,
                related_name="+",
                to="farms.farm",
            ),
        ),
        migrations.AddField(
            model_name="lifecycletemplate",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=RESTRICT,
                related_name="+",
                to="accounts.user",
            ),
        ),
        migrations.AddField(
            model_name="lifecycletemplate",
            name="published_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="lifecycletemplate",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("breed_type__isnull", False),
                    ("org__isnull", False),
                    ("status", "PUBLISHED"),
                ),
                fields=("org", "breed_type"),
                name="uq_lifecycle_templates_org_breed",
            ),
        ),
        migrations.AddConstraint(
            model_name="lifecycletemplate",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("is_default", True),
                    ("org__isnull", False),
                    ("status", "PUBLISHED"),
                ),
                fields=("org",),
                name="uq_lifecycle_templates_org_default",
            ),
        ),
    ]
