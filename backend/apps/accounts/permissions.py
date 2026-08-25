"""RBAC choke point — require_role + farm/saloon/province narrowing (M2-06).

WHY: Security Architecture §4.1b freezes WHO the token says you are; THIS
module is the single place deciding WHAT that token may do (TUTORIAL §3.4:
scattered `if user.role ==` checks are how privilege escalation bugs are born).

Two shapes, one rule source:
- require_role(*roles): plain-Django decorator, byte-for-byte the TUTORIAL
  §3.4 pattern (403 JSON on no intersection with the token's roles claim).
- RequireRole: DRF permission-class twin for API views, reading the SAME
  validated claims (middleware stash first, token payload second) — never the
  DB, because roles are frozen at token birth.

Scope narrowing (account_role_scopes, database.md Compartments 1–2):
a scope ROW narrows with NULL = "ALL at that level":
    saloon set     → exactly that saloon (farm view: its parent farm only)
    farm set       → that farm / its saloons
    province set   → farms inside that province (Device Installer regional case)
    all NULL       → whole org
ZERO scope rows means UNNARROWED: the surface is governed by the member's base
role alone. This is forced by contract v1.4.3's invite endpoint, which creates
memberships WITHOUT scope rows — an owner invited that way must see the whole
org. Scopes therefore only ever NARROW, never widen.

فارسی: تنها محلِ تصمیمِ دسترسی — نقش‌ها از ادعاهای توکن خوانده می‌شوند و دامنهٔ
مزرعه/سالن/استان فقط از همین‌جا محدود می‌شود؛ نبودِ ردیفِ دامنه یعنی کل سازمان.
"""

from functools import wraps
from http import HTTPStatus

from django.db.models import Q
from django.http import JsonResponse

from apps.accounts.models import AccountRoleScope
from apps.farms.models import Farm, Saloon
from apps.tenants.middleware import CLAIMS_ATTR


def _claims(request) -> dict:
    """Validated JWT claims — middleware stash first, DRF token payload second."""
    claims = getattr(request, CLAIMS_ATTR, None)
    if claims is not None:
        return claims
    auth = getattr(request, "auth", None)
    return getattr(auth, "payload", {}) if auth is not None else {}


def require_role(*roles):
    """Plain-Django RBAC decorator — TUTORIAL §3.4 verbatim shape."""

    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not set(roles) & set(_claims(request).get("roles", [])):
                return JsonResponse({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
            return view(request, *args, **kwargs)

        return wrapped

    return deco


class RequireRole:
    """DRF permission twin of require_role — same claims, same verdict."""

    def __init__(self, *roles):
        self.roles = set(roles)

    def has_permission(self, request, view):
        return bool(self.roles & set(_claims(request).get("roles", [])))


def scoped_farms(user, org):
    """Farm queryset narrowed by scopes; None = unnarrowed (full org farms)."""
    rows = list(AccountRoleScope.objects.filter(user=user, org=org))
    if not rows:
        return None
    combined = Q()
    for row in rows:
        if row.saloon_id:
            combined |= Q(pk=row.farm_id)  # the parent farm of that saloon only
        elif row.farm_id:
            combined |= Q(pk=row.farm_id)
        elif row.province_id:
            combined |= Q(province_id=row.province_id)
        else:
            combined |= Q()  # fully NULL row = whole org
    return Farm.objects.filter(org=org).filter(combined)


def scoped_saloons(user, org):
    """Saloon queryset narrowed by scopes; None = unnarrowed (full org saloons)."""
    rows = list(AccountRoleScope.objects.filter(user=user, org=org))
    if not rows:
        return None
    combined = Q()
    for row in rows:
        if row.saloon_id:
            combined |= Q(pk=row.saloon_id)
        elif row.farm_id:
            combined |= Q(farm_id=row.farm_id)
        elif row.province_id:
            combined |= Q(farm__province_id=row.province_id)
        else:
            combined |= Q()  # fully NULL row = whole org
    return Saloon.objects.filter(org=org).filter(combined)
