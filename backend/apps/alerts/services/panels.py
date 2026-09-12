"""Granular panel resolution — v0.9 M9-08/B3 (D24-2).

Resolution order per panel, first match wins:
  1. owner_only panel → visible to the owner role ONLY (fail-closed: neither
     user overrides nor role rows can widen the admin panel);
  2. user override for (panel, user) → allowed as stored;
  3. role rows for the caller's roles → DENY wins among roles (fail-closed);
  4. panel default_open.

Reviewer read-only (D24-1) needs no branch here: every panel surface is a
GET except the owner-only override writers, which already exclude reviewer.
"""

from apps.alerts.models import PanelDefinition, RolePanelPermission, UserPanelOverride

OWNER_ROLES = {"owner"}


def resolve_panels(org_id: str, user, roles: set) -> list:
    """Resolved visible panels for one caller: [{panel_id, title, visible, source}]."""
    roles = set(roles or [])
    overrides = {
        (o.panel_id, o.user_id): o.allowed
        for o in UserPanelOverride.objects.filter(org_id=org_id, user_id=user.pk)
    }
    role_rows: dict[str, list] = {}
    if roles:
        for row in RolePanelPermission.objects.filter(role__in=list(roles)).values("panel_id", "allowed"):
            role_rows.setdefault(row["panel_id"], []).append(row["allowed"])
    out = []
    for panel in PanelDefinition.objects.order_by("panel_id"):
        if panel.owner_only:
            out.append(_row(panel, "owner" in roles, "owner-only"))
            continue
        key = (panel.panel_id, user.pk)
        if key in overrides:
            out.append(_row(panel, overrides[key], "user-override"))
            continue
        allows = role_rows.get(panel.panel_id)
        if allows is not None:
            out.append(_row(panel, all(allows), "role"))
            continue
        out.append(_row(panel, panel.default_open, "default"))
    return out


def _row(panel, visible: bool, source: str) -> dict:
    return {
        "panel_id": panel.panel_id,
        "title": panel.title,
        "visible": bool(visible),
        "source": source,
    }
