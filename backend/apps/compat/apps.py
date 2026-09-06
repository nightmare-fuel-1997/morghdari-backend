"""App registry for the additive Flutter compat layer (M6-09).

فارسی: اپِ سازگاری — فقط یک جدولِ اضافه و ماژول‌های جدید؛ به جدول‌های اصلی
دست نمی‌زند.
"""

from django.apps import AppConfig


class CompatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.compat"
    verbose_name = "Flutter compat layer (additive)"
