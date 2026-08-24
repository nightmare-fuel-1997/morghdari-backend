"""
M0-06 — the walking-skeleton proof.

فارسی: آزمون اسکلت — ثابت می‌کند پروژه «بوت» می‌شود: تنظیمات لود، ۱۲ اپ نصب،
ادمین مسیر دارد. همین و بس؛ هر چیز بیشتر در این مرحله نشتی دامنه است.

These tests look trivial ON PURPOSE. M0 sells exactly one thing: "the project
boots, with the right apps, in CI". Anything passing here that ISN'T in the
M0 scope is scope leakage the hardening rule must catch (WALKTHROUGH §"What
you should NOT see": no logins, no telemetry rows, no alerts).
"""

from django.apps import apps as django_apps
from django.test import TestCase

# The 12-app enumeration is Blueprint §1 law: model homes are decided ONCE.
# If this list drifts from config/settings/base.py INSTALLED_APPS, either a
# new app was invented (forbidden at M0) or one was renamed without a doc-first
# decision. Either way we want CI red.
PROJECT_APPS = [
    "apps.accounts",
    "apps.tenants",
    "apps.geography",
    "apps.farms",
    "apps.devices",
    "apps.telemetry",
    "apps.alerts",
    "apps.mqtt_auth",
    "apps.provisioning",
    "apps.auditing",
    "apps.system_config",
    "apps.monitoring",
]


class SkeletonTests(TestCase):
    def test_settings_load_with_project_apps_installed(self):
        """Every project app resolves through the app registry.

        فارسی: هر ۱۲ اپ باید در رجیستری جنگو ثبت شده باشند — یعنی AppConfig
        ها با نام نقطه‌دار درست لود شده‌اند (خطای نام = duplicate-app).
        """
        # The registry is keyed by LABEL ('accounts'); what M0-05 pins is the
        # dotted NAME ('apps.accounts') — a wrong AppConfig.name registers
        # label 'accounts' but name 'accounts', which this check rejects.
        registered_names = {cfg.name for cfg in django_apps.get_app_configs()}
        for dotted in PROJECT_APPS:
            self.assertIn(dotted, registered_names, f"{dotted} not installed with dotted name")

    def test_admin_url_namespace_exists(self):
        """The admin site mounts (system_configuration panel lands on it in v0.x).

        فارسی: فضای‌نام ادمین موجود است؛ پنل system_configuration (تصمیم D21)
        بعداً روی همین سوار می‌شود.
        """
        from django.urls import reverse

        url = reverse("admin:index")
        self.assertTrue(url.startswith("/admin"))

    def test_database_engine_matches_environment(self):
        """sqlite fallback in tests WITHOUT DATABASE_URL; postgres when set (CI).

        فارسی: بدون DATABASE_URL از sqlite استفاده می‌کنیم (سرعت)، ولی وقتی
        CI آن را ست کند باید واقعاً postgres باشد — وگرنه کل گیت بی‌معناست.
        """
        import os

        from django.conf import settings

        engine = settings.DATABASES["default"]["ENGINE"]
        if os.environ.get("DATABASE_URL"):
            self.assertIn("postgresql", engine)
        else:
            self.assertIn("sqlite", engine)
