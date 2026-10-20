"""Platform-staff gate for ORG-LESS endpoints (Phase 1, 2026-10-18).

WHY not `_support_op_context`: that helper (`accounts/views.py`) requires an
active support session carrying an `org_id` claim, because every support op acts
inside one tenant. Factory inventory work happens before any organization
exists — there is no org to scope to — so reusing it would make the Hardware
Team's own panel impossible (design doc §5.2, corpus contradiction X1).

The gate is therefore the same shape as `_can_review` in
`apps/provisioning/api.py`: superuser OR (platform staff AND an allowed
staff_role). A superuser is accepted here (unlike the support-op surface)
because this is platform infrastructure, not tenant data.
"""

from rest_framework.response import Response

# Who may mint factory identities and broker credentials.
HARDWARE_STAFF_ROLES = frozenset({"hardware", "admin"})

# Who may read the inventory (support needs visibility, not write access).
INVENTORY_READ_ROLES = frozenset({"hardware", "admin", "support"})


def staff_denied(request, roles):
    """Return a 403 Response when the caller is not allowed, else None.

    `roles=None` means "any platform staff". The response body is deliberately
    identical for every failure so the endpoint is not an oracle for which
    staff_role exists.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return Response({"error": "platform staff required"}, status=403)
    if user.is_superuser:
        return None
    if not user.is_platform_staff:
        return Response({"error": "platform staff required"}, status=403)
    if roles is not None and getattr(user, "staff_role", None) not in roles:
        return Response({"error": "platform staff required"}, status=403)
    return None
