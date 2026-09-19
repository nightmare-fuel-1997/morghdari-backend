"""Accounts admin — full superuser CRUD (2026-09-19).

Roles are NOT a table: granting a role = creating/editing OrgMember /
AccountRoleScope rows below (role enum: owner, manager, engineer, doctor,
worker, installer, reviewer).
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as AuthUserAdmin

from apps.accounts.models import User
from apps.common.admin_support import BypassRLSModelAdmin, register_app_models


@admin.register(User)
class UserAdmin(BypassRLSModelAdmin, AuthUserAdmin):
    list_display = (
        "username",
        "email",
        "is_staff",
        "is_superuser",
        "is_platform_staff",
        "staff_role",
        "is_active",
    )
    list_filter = ("is_staff", "is_superuser", "is_platform_staff", "is_active")
    fieldsets = AuthUserAdmin.fieldsets + (
        ("Platform", {"fields": ("is_platform_staff", "staff_role")}),
    )
    add_fieldsets = AuthUserAdmin.add_fieldsets + (
        ("Platform", {"fields": ("is_platform_staff", "staff_role")}),
    )


register_app_models("accounts", skip=(User,))
