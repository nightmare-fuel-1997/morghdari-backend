"""v0.9 M9-08/B3 acceptance — granular panels (D24-2).

Resolution chain per panel: owner_only (owner role ONLY, fail-closed) →
user override → role rows (deny wins) → panel default_open. Reviewer reads
like everyone and writes nothing (D24-1 falls out: no write path admits
reviewer). Override writes are owner-only + audited; cross-org rows are
invisible (query-scoped AND RLS-backed).

Panel catalog rows are created per-test (the migration seed covers real
databases; --nomigrations never runs it — same honesty as metric seeds).
"""

import uuid

import pytest

PANELS = [
    ("dashboard", "Overview", True, False),
    ("telemetry", "Live telemetry", True, False),
    ("admin", "Administration", False, True),
]


def _world():
    from django.contrib.auth import get_user_model

    from apps.accounts.models import OrgMember
    from apps.alerts.models import PanelDefinition
    from apps.tenants.models import Organization

    tag = "pn" + uuid.uuid4().hex[:8]
    org = Organization.objects.create(name=f"org-{tag}")
    users = {}
    for username, role in (("owner", "owner"), ("manager", "manager"),
                           ("worker", "worker"), ("reviewer", "reviewer")):
        user = get_user_model().objects.create_user(username=f"{username}-{tag}", password="x")
        OrgMember.objects.create(user=user, org=org, role=role, is_active=True)
        users[role] = user
    for panel_id, title, default_open, owner_only in PANELS:
        PanelDefinition.objects.create(
            panel_id=f"{panel_id}-{tag}", title=title,
            default_open=default_open, owner_only=owner_only,
        )
    return {"org": org, "users": users, "tag": tag}


def _call(view, method, path, user, roles, org, data=None, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    request = getattr(APIRequestFactory(), method.lower())(path, data=data or {}, format="json")
    force_authenticate(request, user=user)
    request.jwt_claims = {"org_id": str(org.org_id), "roles": roles}
    return view(request, **kwargs)


def _pid(tag, name):
    return f"{name}-{tag}"


@pytest.mark.django_db
def test_me_defaults_open_admin_owner_only():
    from apps.alerts.api_panels import panels_me

    w = _world()
    seen = _call(panels_me, "GET", "/api/v1/panels/me/", w["users"]["worker"], ["worker"], w["org"])
    assert seen.status_code == 200
    by_id = {r["panel_id"]: r for r in seen.data["results"]}
    assert by_id[_pid(w["tag"], "dashboard")]["visible"] is True
    assert by_id[_pid(w["tag"], "dashboard")]["source"] == "default"
    assert by_id[_pid(w["tag"], "admin")]["visible"] is False
    assert by_id[_pid(w["tag"], "admin")]["source"] == "owner-only"
    owned = _call(panels_me, "GET", "/api/v1/panels/me/", w["users"]["owner"], ["owner"], w["org"])
    admin = {r["panel_id"]: r for r in owned.data["results"]}[_pid(w["tag"], "admin")]
    assert admin["visible"] is True


@pytest.mark.django_db
def test_role_deny_and_user_override_chain():
    from apps.alerts.api_panels import panels_me, role_panel_permissions, user_panel_overrides

    w = _world()
    owner, worker = w["users"]["owner"], w["users"]["worker"]
    oclaims = {"org_id": str(w["org"].org_id), "roles": ["owner"]}
    from rest_framework.test import APIRequestFactory, force_authenticate

    def owner_post(view, path, data):
        request = APIRequestFactory().post(path, data=data, format="json")
        force_authenticate(request, user=owner)
        request.jwt_claims = oclaims
        return view(request)

    denied = owner_post(
        role_panel_permissions, "/api/v1/panels/role-permissions/",
        {"panel_id": _pid(w["tag"], "telemetry"), "role": "worker", "allowed": False},
    )
    assert denied.status_code == 200, denied.data
    me = _call(panels_me, "GET", "/api/v1/panels/me/", worker, ["worker"], w["org"])
    tele = {r["panel_id"]: r for r in me.data["results"]}[_pid(w["tag"], "telemetry")]
    assert tele == {"panel_id": _pid(w["tag"], "telemetry"), "title": "Live telemetry",
                    "visible": False, "source": "role"}

    opened = owner_post(
        user_panel_overrides, "/api/v1/panels/user-overrides/",
        {"panel_id": _pid(w["tag"], "telemetry"), "user_id": worker.pk, "allowed": True},
    )
    assert opened.status_code == 200
    me2 = _call(panels_me, "GET", "/api/v1/panels/me/", worker, ["worker"], w["org"])
    tele2 = {r["panel_id"]: r for r in me2.data["results"]}[_pid(w["tag"], "telemetry")]
    assert tele2["visible"] is True and tele2["source"] == "user-override"


@pytest.mark.django_db
def test_owner_only_cannot_be_overridden_open():
    from apps.alerts.api_panels import panels_me, user_panel_overrides
    from rest_framework.test import APIRequestFactory, force_authenticate

    w = _world()
    owner, worker = w["users"]["owner"], w["users"]["worker"]
    request = APIRequestFactory().post(
        "/api/v1/panels/user-overrides/",
        {"panel_id": _pid(w["tag"], "admin"), "user_id": worker.pk, "allowed": True},
        format="json",
    )
    force_authenticate(request, user=owner)
    request.jwt_claims = {"org_id": str(w["org"].org_id), "roles": ["owner"]}
    assert user_panel_overrides(request).status_code == 200  # stored, but…
    me = _call(panels_me, "GET", "/api/v1/panels/me/", worker, ["worker"], w["org"])
    admin = {r["panel_id"]: r for r in me.data["results"]}[_pid(w["tag"], "admin")]
    assert admin["visible"] is False  # …fail-closed: the admin panel stays shut


@pytest.mark.django_db
def test_writes_owner_only_reviewer_read_only():
    from apps.alerts.api_panels import panels_me, role_panel_permissions, user_panel_overrides

    w = _world()
    body = {"panel_id": _pid(w["tag"], "dashboard"), "role": "worker", "allowed": False}
    for role in ("manager", "worker", "reviewer"):
        denied = _call(
            role_panel_permissions, "POST", "/api/v1/panels/role-permissions/",
            w["users"][role], [role], w["org"], body,
        )
        assert denied.status_code == 403, role
    urev = _call(
        user_panel_overrides, "POST", "/api/v1/panels/user-overrides/",
        w["users"]["reviewer"], ["reviewer"], w["org"],
        {"panel_id": _pid(w["tag"], "dashboard"), "user_id": w["users"]["worker"].pk, "allowed": True},
    )
    assert urev.status_code == 403
    seen = _call(panels_me, "GET", "/api/v1/panels/me/", w["users"]["reviewer"], ["reviewer"], w["org"])
    assert seen.status_code == 200
    assert seen.data["results"]


@pytest.mark.django_db
def test_override_validation_and_delete():
    from apps.alerts.api_panels import user_panel_override_delete, user_panel_overrides

    w = _world()
    owner = w["users"]["owner"]
    bad_panel = _call(
        user_panel_overrides, "POST", "/api/v1/panels/user-overrides/", owner, ["owner"], w["org"],
        {"panel_id": "nope", "user_id": 1, "allowed": True},
    )
    assert bad_panel.status_code == 400
    bad_user = _call(
        user_panel_overrides, "POST", "/api/v1/panels/user-overrides/", owner, ["owner"], w["org"],
        {"panel_id": _pid(w["tag"], "dashboard"), "user_id": 424242, "allowed": True},
    )
    assert bad_user.status_code == 404
    made = _call(
        user_panel_overrides, "POST", "/api/v1/panels/user-overrides/", owner, ["owner"], w["org"],
        {"panel_id": _pid(w["tag"], "dashboard"), "user_id": w["users"]["worker"].pk, "allowed": False},
    )
    gone = _call(
        user_panel_override_delete, "DELETE",
        f"/api/v1/panels/user-overrides/{made.data['id']}/", owner, ["owner"], w["org"],
        override_id=made.data["id"],
    )
    assert gone.status_code == 200
    again = _call(
        user_panel_override_delete, "DELETE",
        f"/api/v1/panels/user-overrides/{made.data['id']}/", owner, ["owner"], w["org"],
        override_id=made.data["id"],
    )
    assert again.status_code == 404


@pytest.mark.django_db
def test_cross_org_overrides_invisible():
    from apps.alerts.api_panels import user_panel_overrides
    from apps.tenants.models import Organization

    w = _world()
    other = Organization.objects.create(name=f"other-{uuid.uuid4().hex[:6]}")
    seen = _call(
        user_panel_overrides, "GET", "/api/v1/panels/user-overrides/",
        w["users"]["owner"], ["owner"], other,
    )
    assert seen.status_code == 200
    assert seen.data == {"results": [], "count": 0}
