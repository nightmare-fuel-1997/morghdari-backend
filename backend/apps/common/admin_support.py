"""Shared Django-admin base — break-glass superuser access (2026-09-19).

Why this module exists: the project's 46 RLS-protected tables fail closed for
the app DB role whenever no tenant GUC (`app.current_org_id`) is set — and
admin requests never set one. Routing admin ORM traffic through the `admin`
DB alias (morghdari_admin, BYPASSRLS) gives the superuser true cross-org
add/change/delete while request traffic stays on the RLS-enforced `default`
alias, so tenant isolation is untouched.

The alias only exists where DATABASE_ADMIN_URL is set (prod daphne/worker);
everywhere else (CI, laptop, tests) we fall back to `default` so admin code
never KeyErrors at import.
"""

from django.contrib import admin
from django.db import connections
from django.db.models import BooleanField, CharField, ForeignKey, TextField

USING = "admin" if "admin" in connections else "default"


class BypassRLSModelAdmin(admin.ModelAdmin):
    """ModelAdmin that reads/writes through the BYPASSRLS alias.

    Covers changelist, add/change forms, single + bulk delete, and FK/M2M
    dropdowns (which otherwise query the RLS-enforced alias and render
    empty for tenant tables).
    """

    using = USING
    list_per_page = 100

    def get_queryset(self, request):
        return super().get_queryset(request).using(self.using)

    def save_model(self, request, obj, form, change):
        obj.save(using=self.using)

    def delete_model(self, request, obj):
        obj.delete(using=self.using)

    def delete_queryset(self, request, queryset):
        # Bulk delete must run on the bypass alias; collect PKs first because
        # a subquery cannot span two database connections.
        pks = list(queryset.values_list("pk", flat=True))
        queryset.model._default_manager.using(self.using).filter(pk__in=pks).delete()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        kwargs["queryset"] = db_field.remote_field.model._default_manager.using(self.using).all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        kwargs["queryset"] = db_field.remote_field.model._default_manager.using(self.using).all()
        return super().formfield_for_manytomany(db_field, request, **kwargs)


def register_app_models(app_label, skip=()):
    """Auto-register every concrete model of an app with a sane ModelAdmin.

    list_display = PK + first 5 concrete fields; text fields become searchable;
    FKs use raw-id widgets (tenant tables are large); booleans become filters.
    `skip` holds models with a hand-written ModelAdmin (e.g. the custom User).
    Idempotent — already-registered models are left alone.
    """
    from django.apps import apps as dj_apps

    skip_ids = {m._meta.label_lower for m in skip}
    for model in dj_apps.get_app_config(app_label).get_models():
        meta = model._meta
        if meta.proxy or meta.auto_created or meta.label_lower in skip_ids:
            continue
        if model in admin.site._registry:
            continue
        concrete = [f for f in meta.concrete_fields if not f.primary_key]
        attrs = {
            "list_display": [meta.pk.name]
            + [f.name for f in concrete[:5]],
            "search_fields": [
                f.name for f in concrete if isinstance(f, (CharField, TextField))
            ][:4],
            "raw_id_fields": [f.name for f in concrete if isinstance(f, ForeignKey)],
            "list_filter": [f.name for f in concrete if isinstance(f, BooleanField)][
                :3
            ],
        }
        admin_class = type(
            f"{model.__name__}AutoAdmin", (BypassRLSModelAdmin,), attrs
        )
        admin.site.register(model, admin_class)
