from django.apps import AppConfig


class TenantsConfig(AppConfig):
    # default_auto_field: explicit so migrations never guess (BigAutoField =
    # project-wide DEFAULT_AUTO_FIELD anyway; restating here kills ambiguity).
    default_auto_field = "django.db.models.BigAutoField"
    # MUST equal the dotted path or Django raises duplicate-app errors and
    # migrations split-brain (v0.1 TUTORIAL §3.5 pitfall table).
    name = "apps.tenants"
