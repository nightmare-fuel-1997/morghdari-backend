"""M6-11 acceptance — the v1.7.1 threshold-template WRITE surface (O13–O16).

Gates (owner decision 2026-09-12):

- **Scope, fail-closed.** An engineer authors drafts only inside granted farms;
  an engineer with NO scope row gets 403. `scoped_farms()` returning None means
  "the whole org" for reads — this surface inverts it to the EMPTY set, and
  this module pins that inversion, because getting it wrong turns "farm-scoped
  engineer" into "every farm in the org". (Before v1.7.1 nothing called
  `scoped_farms` at all: tested-but-unmounted dead code.)
- **Drafts never arm.** Resolution considers PUBLISHED rows only, so authoring
  is safe; publishing is owner/manager and fires the cache sweep; assigning is
  owner/manager and re-caches exactly that saloon.
- **Exact keys** and identical 404s for foreign/unknown (never an oracle).
- **Validation:** overlap, reversed day ranges, unknown metric, out-of-range
  values and an unbounded `nh3` band are all 400s.

Recache assertions are deliberately split in two: the *message* (which Celery
task a publish/assign fires) is captured by patching `.delay`, and the
*resolution* (what a hash is built from) is asserted through `effective_template`
/ `build_hash_for_saloon`. The writes under test ALSO fire those tasks for real
(eager Celery runs them inline inside the request), so the module autouse-patches
the cache client with a hash store: nothing here needs Redis semantics, and the
suite stays runnable on a laptop with no container. CI still has a real Redis for
the modules that exercise it.

فارسی: پذیرش سطحِ نوشتنِ قالب — دامنهٔ مزرعه برای مهندس (بی‌دامنه = بی‌اجازه)،
پیش‌نویس هرگز سالن را مسلح نمی‌کند، و انتشار/تخصیص با مدیر است و کش را تازه می‌کند.
"""

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone as dj_tz
from rest_framework.test import APIClient

from apps.accounts.models import AccountRoleScope, OrgMember
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.alerts.services import thresholds as thresholds_service
from apps.alerts.services.thresholds import (
    SOURCE_ASSIGNED,
    SOURCE_GLOBAL_DEFAULT,
    SOURCE_NONE,
    SOURCE_ORG_BREED,
    SOURCE_ORG_DEFAULT,
    build_hash_for_saloon,
    effective_template,
)
from apps.auditing.models import AccountActivityLog
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.telemetry.models import FlockCycle
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"


class _HashStore:
    """Minimal stand-in for the alerts threshold cache (a Redis hash store).

    `recache_saloon()` is the only cache reader/writer the writes in this module
    can reach: authoring a template, publishing one and assigning one to a
    saloon each fire the alerts fast-path tasks, which run INLINE
    (CELERY_TASK_ALWAYS_EAGER) inside the request under test. Without this fake
    every test would open a real socket and, with no Redis container, spend its
    runtime in connect retries instead of asserting anything.
    """

    def __init__(self):
        self.hashes: dict = {}
        self.strings: dict = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in (mapping or {}).items()}
        )
        return len(mapping or {})

    def setex(self, key, ttl, value):
        self.strings[key] = (ttl, value)

    def delete(self, *keys):
        return sum(1 for key in keys if self.hashes.pop(key, None) is not None)

    def pipeline(self):
        return self

    def execute(self):
        return []


@pytest.fixture(autouse=True)
def threshold_cache_hash_store(monkeypatch):
    """Patch the alerts cache client factory — no live Redis is needed here.

    The writes under test legitimately trigger the cache fast paths, and eager
    Celery runs them inside the request; asserting *which* task fires (patching
    `.delay`) or *what a hash is built from* (`build_hash_for_saloon`,
    `effective_template`) needs no Redis semantics. Real-Redis behaviour stays
    with the modules that exercise it in CI.
    """
    monkeypatch.setattr(thresholds_service, "_redis", lambda: _HashStore())


pytestmark = pytest.mark.django_db

# A minimal valid band list: one bounded sensor metric + a bounded NH3 band.
RULES = [
    {"metric_key": "temp", "day_from": 1, "day_to": 20, "min": 18.0, "max": 29.0},
    {"metric_key": "nh3", "day_from": 1, "day_to": 20, "min": 0.0, "max": 20.0},
]


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


def _make_world(tag=None):
    tag = tag or uuid.uuid4().hex[:6]
    province = Province.objects.create(name=f"twp-{tag}")
    city = City.objects.create(province=province, name=f"twc-{tag}")
    org = Organization.objects.create(name=f"tw-org-{tag}")
    other = Organization.objects.create(name=f"tw-other-{tag}")
    farm = Farm.objects.create(org=org, name=f"TWF1-{tag}", province=province, city=city)
    farm_b = Farm.objects.create(org=org, name=f"TWF2-{tag}", province=province, city=city)
    saloon = Saloon.objects.create(org=org, farm=farm, name=f"TWS1-{tag}")
    saloon_b = Saloon.objects.create(org=org, farm=farm_b, name=f"TWS2-{tag}")

    def member(username, role, org_row=org):
        user = User.objects.create_user(username=username, password=PASSWORD)
        return OrgMember.objects.create(user=user, org=org_row, role=role, is_active=True)

    # NOTE: no AccountRoleScope rows — an unscoped engineer must be REFUSED.
    users = {r: member(f"tw-{r}-{tag}", r) for r in ("owner", "manager", "engineer", "worker")}
    foreign = member(f"tw-foreign-{tag}", "owner", org_row=other)

    # The world's LIVE template (published org default, breed "ross").
    live = LifecycleTemplate.objects.create(
        org=org, breed_type="ross", name=f"Live-{tag}", is_default=True
    )
    LifecycleThresholdRule.objects.create(
        template=live, day_from=1, day_to=45, metric="temp", min_value=18, max_value=30
    )
    LifecycleThresholdRule.objects.create(
        template=live, day_from=1, day_to=45, metric="nh3", min_value=0, max_value=25
    )

    flocks = {}
    for row in (saloon, saloon_b):
        flocks[row.pk] = FlockCycle.objects.create(
            org=org,
            saloon=row,
            breed_type="ross",
            start_date=dj_tz.now() - timedelta(days=3),
            placement_count=100,
        )

    return {
        "tag": tag,
        "org": org,
        "other": other,
        "farm": farm,
        "farm_b": farm_b,
        "saloon": saloon,
        "saloon_b": saloon_b,
        "users": users,
        "foreign": foreign,
        "live": live,
        "flocks": flocks,
    }


def _login(username, password=PASSWORD):
    client = APIClient()
    res = client.post(
        reverse("accounts:auth-login"),
        {"username": username, "password": password},
        format="json",
    )
    assert res.status_code == 200, res.content
    return res.data["access_token"]


def _client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client


def _as(world, role):
    """An authenticated client for one of the world's members."""
    return _client(_login(world["users"][role].user.username))


def _grant(world, role, *, farm=None, saloon=None, province=None):
    """One scope row — the ONLY way an engineer gets template-write rights."""
    return AccountRoleScope.objects.create(
        user=world["users"][role].user,
        org=world["org"],
        role=role,
        farm=farm,
        saloon=saloon,
        province=province,
    )


def _draft(world, *, farm=None, breed="broiler", name="Draft", rules=None, status=None):
    """A draft straight through the ORM (for cases the API refuses to create)."""
    template = LifecycleTemplate.objects.create(
        org=world["org"],
        farm=farm,
        name=name,
        breed_type=breed,
        status=status or LifecycleTemplate.DRAFT,
    )
    for rule in rules if rules is not None else RULES:
        LifecycleThresholdRule.objects.create(
            template=template,
            day_from=rule["day_from"],
            day_to=rule["day_to"],
            metric=rule["metric_key"],
            min_value=rule.get("min"),
            max_value=rule.get("max"),
        )
    return template


def _bands(saloon):
    """The hash a saloon would be armed with, right now (no Redis needed).

    The saloon row is read back first. The write endpoints mutate the row THEY
    load, so grading the caller's in-memory object would read a stale
    `threshold_template_id` — vacuously green for a clear, and wrongly red for
    an assignment.
    """
    return build_hash_for_saloon(Saloon.objects.get(pk=saloon.pk))


# ---------------------------------------------------------------------------
# O13 — authoring a draft
# ---------------------------------------------------------------------------


class TestO13Authoring:
    def test_unscoped_engineer_is_refused(self):
        """THE fail-closed gate: no scope row is not 'the whole org' here."""
        w = _make_world()
        res = _as(w, "engineer").post(
            reverse("ops:threshold-templates"),
            {"name": "Nope", "breed_type": "broiler", "rules": RULES},
            format="json",
        )
        assert res.status_code == 403
        assert not LifecycleTemplate.objects.filter(name="Nope").exists()

    def test_worker_is_refused(self):
        w = _make_world()
        res = _as(w, "worker").post(
            reverse("ops:threshold-templates"),
            {"name": "Nope", "breed_type": "broiler", "rules": RULES},
            format="json",
        )
        assert res.status_code == 403

    def test_scoped_engineer_creates_farm_draft(self):
        w = _make_world()
        _grant(w, "engineer", farm=w["farm"])
        res = _as(w, "engineer").post(
            reverse("ops:threshold-templates"),
            {"name": "Broiler plan", "breed_type": "broiler", "rules": RULES},
            format="json",
        )
        assert res.status_code == 201, res.content
        body = res.data
        assert set(body.keys()) == {
            "template_id",
            "org_id",
            "breed_type",
            "name",
            "is_default",
            "status",
            "farm_id",
            "created_by",
            "published_at",
            "rules",
        }
        assert body["status"] == "DRAFT"
        assert body["farm_id"] == w["farm"].pk  # single-farm scope ⇒ implied
        assert body["published_at"] is None
        assert body["created_by"] == w["users"]["engineer"].user.pk
        assert {r["metric_key"] for r in body["rules"]} == {"temp", "nh3"}
        assert set(body["rules"][0].keys()) == {
            "id",
            "metric_key",
            "template",
            "day_from",
            "day_to",
            "min",
            "max",
        }
        assert AccountActivityLog.objects.filter(
            action="THRESHOLD_TEMPLATE_CREATED", org=w["org"]
        ).exists()

    def test_engineer_cannot_author_outside_the_grant(self):
        w = _make_world()
        _grant(w, "engineer", farm=w["farm"])
        res = _as(w, "engineer").post(
            reverse("ops:threshold-templates"),
            {"name": "Other farm", "breed_type": "broiler", "farm_id": w["farm_b"].pk, "rules": RULES},
            format="json",
        )
        assert res.status_code == 403

    def test_engineer_with_two_farms_must_name_one(self):
        w = _make_world()
        _grant(w, "engineer", farm=w["farm"])
        _grant(w, "engineer", farm=w["farm_b"])
        client = _as(w, "engineer")
        url = reverse("ops:threshold-templates")

        ambiguous = client.post(url, {"name": "A", "breed_type": "broiler", "rules": RULES}, format="json")
        assert ambiguous.status_code == 400
        assert ambiguous.data == {"error": "farm_id is required when several farms are in scope"}

        named = client.post(
            url,
            {"name": "B", "breed_type": "broiler", "farm_id": w["farm_b"].pk, "rules": RULES},
            format="json",
        )
        assert named.status_code == 201 and named.data["farm_id"] == w["farm_b"].pk

    def test_owner_may_author_an_org_wide_draft(self):
        w = _make_world()
        res = _as(w, "owner").post(
            reverse("ops:threshold-templates"),
            {"name": "Org wide", "breed_type": "broiler", "rules": RULES},
            format="json",
        )
        assert res.status_code == 201 and res.data["farm_id"] is None

    def test_foreign_farm_id_is_404(self):
        w = _make_world()
        province = Province.objects.create(name=f"fr-p-{w['tag']}")
        city = City.objects.create(province=province, name=f"fr-c-{w['tag']}")
        foreign_farm = Farm.objects.create(
            org=w["other"], name=f"FRN-{w['tag']}", province=province, city=city
        )
        res = _as(w, "owner").post(
            reverse("ops:threshold-templates"),
            {"name": "Foreign", "breed_type": "broiler", "farm_id": foreign_farm.pk, "rules": RULES},
            format="json",
        )
        assert res.status_code == 404

    def test_worker_and_callers_without_role_are_refused_by_templates_path_only(self):
        """O13 shares the O4 path: the ROLE fence, not the route, is the gate."""
        w = _make_world()
        assert _as(w, "worker").get(reverse("ops:threshold-templates")).status_code == 403

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"breed_type": "broiler", "rules": RULES}, "invalid name"),
            ({"name": "x" * 101, "breed_type": "broiler", "rules": RULES}, "invalid name"),
            ({"name": "Ok", "breed_type": "b" * 51, "rules": RULES}, "invalid breed_type"),
            ({"name": "Ok", "breed_type": "broiler"}, "rules must be a non-empty list"),
            ({"name": "Ok", "breed_type": "broiler", "rules": []}, "rules must be a non-empty list"),
            (
                {"name": "Ok", "breed_type": "broiler", "rules": [{"metric_key": "nope", "day_from": 1, "day_to": 2}]},
                "rule 1: unknown metric_key",
            ),
            (
                {"name": "Ok", "breed_type": "broiler", "rules": [{"metric_key": "temp", "day_from": 10, "day_to": 2}]},
                "rule 1: invalid day range",
            ),
            (
                {"name": "Ok", "breed_type": "broiler", "rules": [{"metric_key": "temp", "day_from": 0, "day_to": 999}]},
                "rule 1: invalid day range",
            ),
            (
                {
                    "name": "Ok",
                    "breed_type": "broiler",
                    "rules": [{"metric_key": "temp", "day_from": 1, "day_to": 5, "max": 5000}],
                },
                "rule 1: threshold out of range",
            ),
            (
                {
                    "name": "Ok",
                    "breed_type": "broiler",
                    "rules": [{"metric_key": "temp", "day_from": 1, "day_to": 5, "min": 30, "max": 30}],
                },
                "rule 1: threshold out of range",
            ),
            (
                {
                    "name": "Ok",
                    "breed_type": "broiler",
                    "rules": [
                        {"metric_key": "temp", "day_from": 1, "day_to": 10, "max": 30},
                        {"metric_key": "temp", "day_from": 10, "day_to": 20, "max": 31},
                    ],
                },
                "rules 1 and 2 overlap for metric temp",
            ),
            (
                {
                    "name": "Ok",
                    "breed_type": "broiler",
                    "rules": [{"metric_key": "nh3", "day_from": 1, "day_to": 5, "min": 0}],
                },
                "nh3 band must carry a max (NH3 can never be silenced)",
            ),
            (
                {"name": "Ok", "breed_type": "broiler", "rules": "not-a-list"},
                "rules must be a non-empty list",
            ),
        ],
    )
    def test_band_validation_messages(self, payload, expected):
        w = _make_world()
        res = _as(w, "owner").post(reverse("ops:threshold-templates"), payload, format="json")
        assert res.status_code == 400, res.content
        assert res.data == {"error": expected}

    def test_too_many_rules_is_400(self):
        w = _make_world()
        rules = [
            {"metric_key": "temp", "day_from": i, "day_to": i, "max": 30}
            for i in range(0, 61)
        ]
        res = _as(w, "owner").post(
            reverse("ops:threshold-templates"),
            {"name": "Huge", "breed_type": "broiler", "rules": rules},
            format="json",
        )
        assert res.status_code == 400
        assert res.data["error"] == "too many rules (max 60)"

    def test_non_object_rule_is_400(self):
        w = _make_world()
        res = _as(w, "owner").post(
            reverse("ops:threshold-templates"),
            {"name": "Odd", "breed_type": "broiler", "rules": ["temp"]},
            format="json",
        )
        assert res.status_code == 400
        assert res.data == {"error": "rule 1: must be an object"}


# ---------------------------------------------------------------------------
# O14 — editing a draft
# ---------------------------------------------------------------------------


class TestO14Editing:
    def test_scoped_engineer_replaces_the_whole_band_list(self):
        w = _make_world()
        _grant(w, "engineer", farm=w["farm"])
        draft = _draft(w, farm=w["farm"])
        res = _as(w, "engineer").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"name": "Renamed", "rules": [{"metric_key": "humidity", "day_from": 2, "day_to": 9, "min": 40, "max": 70}]},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert res.data["name"] == "Renamed"
        assert [r["metric_key"] for r in res.data["rules"]] == ["humidity"]
        assert LifecycleThresholdRule.objects.filter(template=draft).count() == 1
        assert AccountActivityLog.objects.filter(
            action="THRESHOLD_TEMPLATE_UPDATED", org=w["org"]
        ).exists()

    def test_partial_edit_leaves_breed_and_rules_alone(self):
        w = _make_world()
        draft = _draft(w, breed="broiler")
        res = _as(w, "manager").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"name": "Only the name"},
            format="json",
        )
        assert res.status_code == 200
        assert res.data["breed_type"] == "broiler"
        assert len(res.data["rules"]) == len(RULES)

    def test_manager_edits_any_draft(self):
        w = _make_world()
        draft = _draft(w, farm=w["farm"])
        res = _as(w, "manager").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"breed_type": "cobb"},
            format="json",
        )
        assert res.status_code == 200 and res.data["breed_type"] == "cobb"

    def test_engineer_may_not_edit_org_wide_or_other_farm_drafts(self):
        w = _make_world()
        _grant(w, "engineer", farm=w["farm"])
        org_wide = _draft(w, farm=None, name="OrgWide")
        other_farm = _draft(w, farm=w["farm_b"], name="FarmB")
        client = _as(w, "engineer")
        for draft in (org_wide, other_farm):
            res = client.put(
                reverse("ops:threshold-template-update", args=[draft.pk]),
                {"name": "nope"},
                format="json",
            )
            assert res.status_code == 403

    def test_unscoped_engineer_may_not_edit_anything(self):
        w = _make_world()
        draft = _draft(w, farm=w["farm"])
        res = _as(w, "engineer").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"name": "nope"},
            format="json",
        )
        assert res.status_code == 403

    def test_published_template_is_conflict(self):
        w = _make_world()
        res = _as(w, "owner").put(
            reverse("ops:threshold-template-update", args=[w["live"].pk]),
            {"name": "nope"},
            format="json",
        )
        assert res.status_code == 409
        assert res.data == {"error": "template is published (tune its rules through /thresholds/)"}

    def test_foreign_and_global_templates_are_404(self):
        w = _make_world()
        foreign = LifecycleTemplate.objects.create(
            org=w["other"], breed_type="broiler", name="Foreign"
        )
        global_tpl = LifecycleTemplate.objects.create(
            org=None, breed_type=None, name="Builtin", is_default=True
        )
        client = _as(w, "owner")
        for template in (foreign, global_tpl, None):
            target = template.pk if template is not None else 999999
            res = client.put(
                reverse("ops:threshold-template-update", args=[target]),
                {"name": "x"},
                format="json",
            )
            assert res.status_code == 404 and res.data == {"error": "template not found"}

    def test_bad_rules_on_edit_are_400(self):
        w = _make_world()
        draft = _draft(w)
        res = _as(w, "owner").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"rules": [{"metric_key": "nh3", "day_from": 1, "day_to": 3}]},
            format="json",
        )
        assert res.status_code == 400
        assert res.data["error"] == "nh3 band must carry a max (NH3 can never be silenced)"


# ---------------------------------------------------------------------------
# O15 — publish
# ---------------------------------------------------------------------------


class TestO15Publish:
    def test_only_owner_and_manager_publish(self):
        w = _make_world()
        draft = _draft(w)
        url = reverse("ops:threshold-template-publish", args=[draft.pk])
        for role in ("engineer", "worker"):
            assert _as(w, role).post(url, {}, format="json").status_code == 403
        assert _as(w, "manager").post(url, {}, format="json").status_code == 200

    def test_a_draft_never_arms_a_saloon_but_publishing_does(self):
        """THE safety property the whole write surface rests on."""
        w = _make_world()
        draft = _draft(w, breed="broiler", name="New plan")
        saloon = w["saloon"]

        # A draft exists, is referenced by nothing, and changes NOTHING.
        before_template, before_source = effective_template(w["flocks"][saloon.pk], saloon)
        assert before_template.pk == w["live"].pk
        assert before_source == SOURCE_ORG_BREED
        assert _bands(saloon)["temp_max"] == "30.00"

        res = _as(w, "manager").post(
            reverse("ops:threshold-template-publish", args=[draft.pk]), {}, format="json"
        )
        assert res.status_code == 200, res.content
        assert res.data["status"] == "PUBLISHED"
        assert res.data["published_at"] is not None
        assert AccountActivityLog.objects.filter(
            action="THRESHOLD_TEMPLATE_PUBLISHED", org=w["org"]
        ).exists()

        # Adopting the breed makes it the resolver's org+breed pick.
        w["flocks"][saloon.pk].breed_type = "broiler"
        w["flocks"][saloon.pk].save(update_fields=["breed_type"])
        after_template, after_source = effective_template(w["flocks"][saloon.pk], saloon)
        assert after_template.pk == draft.pk
        assert after_source == SOURCE_ORG_BREED

    def test_publishing_fires_the_cache_sweep(self, monkeypatch):
        from apps.alerts import tasks

        calls = []
        monkeypatch.setattr(tasks.cache_daily_thresholds, "delay", lambda *a, **k: calls.append(a))
        w = _make_world()
        draft = _draft(w)
        res = _as(w, "manager").post(
            reverse("ops:threshold-template-publish", args=[draft.pk]), {}, format="json"
        )
        assert res.status_code == 200
        assert calls, "publishing must re-cache the affected saloons"

    def test_editing_a_draft_does_not_sweep(self, monkeypatch):
        from apps.alerts import tasks

        calls = []
        monkeypatch.setattr(tasks.cache_daily_thresholds, "delay", lambda *a, **k: calls.append(a))
        w = _make_world()
        draft = _draft(w)
        calls.clear()  # ignore whatever the world's own writes triggered
        _as(w, "owner").put(
            reverse("ops:threshold-template-update", args=[draft.pk]),
            {"name": "quiet edit"},
            format="json",
        )
        assert calls == []

    def test_duplicate_breed_is_conflict(self):
        w = _make_world()  # the live template already covers breed "ross"
        draft = _draft(w, breed="ross", name="Second ross")
        res = _as(w, "owner").post(
            reverse("ops:threshold-template-publish", args=[draft.pk]), {}, format="json"
        )
        assert res.status_code == 409
        assert "already covers this breed" in res.data["error"]
        draft.refresh_from_db()
        assert draft.status == LifecycleTemplate.DRAFT

    def test_publishing_as_default_demotes_the_incumbent(self):
        w = _make_world()
        draft = _draft(w, breed="broiler", name="New default")
        res = _as(w, "owner").post(
            reverse("ops:threshold-template-publish", args=[draft.pk]),
            {"is_default": True},
            format="json",
        )
        assert res.status_code == 200 and res.data["is_default"] is True
        w["live"].refresh_from_db()
        assert w["live"].is_default is False
        draft.refresh_from_db()
        assert draft.is_default is True

    def test_second_publish_is_conflict(self):
        w = _make_world()
        draft = _draft(w)
        url = reverse("ops:threshold-template-publish", args=[draft.pk])
        assert _as(w, "owner").post(url, {}, format="json").status_code == 200
        res = _as(w, "owner").post(url, {}, format="json")
        assert res.status_code == 409 and res.data == {"error": "template is already published"}

    def test_publishing_a_ruleless_draft_is_400(self):
        w = _make_world()
        draft = _draft(w, rules=[])
        res = _as(w, "owner").post(
            reverse("ops:threshold-template-publish", args=[draft.pk]), {}, format="json"
        )
        assert res.status_code == 400 and res.data == {"error": "template has no rules"}

    def test_foreign_global_and_unknown_are_404(self):
        w = _make_world()
        foreign = LifecycleTemplate.objects.create(
            org=w["other"], breed_type="broiler", name="F", status=LifecycleTemplate.DRAFT
        )
        global_tpl = LifecycleTemplate.objects.create(
            org=None, breed_type="broiler", name="G", status=LifecycleTemplate.DRAFT
        )
        client = _as(w, "owner")
        for target in (foreign.pk, global_tpl.pk, 999999):
            res = client.post(
                reverse("ops:threshold-template-publish", args=[target]), {}, format="json"
            )
            assert res.status_code == 404 and res.data == {"error": "template not found"}


# ---------------------------------------------------------------------------
# O16 — assignment
# ---------------------------------------------------------------------------


class TestO16Assignment:
    def test_manager_assigns_and_the_chain_follows(self, monkeypatch):
        from apps.alerts import tasks

        calls = []
        monkeypatch.setattr(tasks.cache_saloon_thresholds, "delay", lambda *a, **k: calls.append(a))
        w = _make_world()
        plan = _draft(w, breed="broiler", name="Assigned plan", status=LifecycleTemplate.PUBLISHED)
        saloon = w["saloon_b"]
        res = _as(w, "manager").put(
            reverse("ops:saloon-threshold-template", args=[saloon.pk]),
            {"template_id": plan.pk},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert res.data == {
            "saloon_id": saloon.pk,
            "assigned_template_id": plan.pk,
            "effective_template_id": plan.pk,
            "source": SOURCE_ASSIGNED,
        }
        assert calls, "assignment must re-cache that saloon immediately"
        assert AccountActivityLog.objects.filter(
            action="SALOON_TEMPLATE_ASSIGNED", org=w["org"], saloon=saloon
        ).exists()
        # ...and the assignment OUTRANKS the flock's breed, which is "ross".
        assert _bands(saloon)["temp_max"] == "29.00"
        saloon.refresh_from_db()  # the endpoint wrote its own loaded row
        template, source = effective_template(w["flocks"][saloon.pk], saloon)
        assert template.pk == plan.pk and source == SOURCE_ASSIGNED

    def test_clearing_returns_the_saloon_to_the_chain(self):
        w = _make_world()
        plan = _draft(w, breed="broiler", status=LifecycleTemplate.PUBLISHED)
        saloon = w["saloon"]
        client = _as(w, "manager")
        url = reverse("ops:saloon-threshold-template", args=[saloon.pk])

        client.put(url, {"template_id": plan.pk}, format="json")
        res = client.put(url, {"template_id": None}, format="json")
        assert res.status_code == 200
        assert res.data["assigned_template_id"] is None
        assert res.data["source"] == SOURCE_ORG_BREED
        assert res.data["effective_template_id"] == w["live"].pk
        assert AccountActivityLog.objects.filter(
            action="SALOON_TEMPLATE_CLEARED", org=w["org"], saloon=saloon
        ).exists()
        assert _bands(saloon)["temp_max"] == "30.00"

    def test_assignment_must_be_published(self):
        w = _make_world()
        draft = _draft(w)
        res = _as(w, "manager").put(
            reverse("ops:saloon-threshold-template", args=[w["saloon"].pk]),
            {"template_id": draft.pk},
            format="json",
        )
        assert res.status_code == 400
        assert res.data == {"error": "template is not published"}

    def test_read_is_open_to_any_member_write_is_not(self):
        w = _make_world()
        url = reverse("ops:saloon-threshold-template", args=[w["saloon"].pk])
        assert _as(w, "worker").get(url).status_code == 200
        for role in ("worker", "engineer"):
            res = _as(w, role).put(url, {"template_id": None}, format="json")
            assert res.status_code == 403

    def test_foreign_global_unknown_template_and_foreign_saloon(self):
        w = _make_world()
        foreign = LifecycleTemplate.objects.create(
            org=w["other"], breed_type="broiler", name="F", status=LifecycleTemplate.PUBLISHED
        )
        global_tpl = LifecycleTemplate.objects.create(
            org=None, breed_type="broiler", name="G", status=LifecycleTemplate.PUBLISHED
        )
        foreign_saloon = Saloon.objects.create(org=w["other"], farm=None, name="FS")
        client = _as(w, "manager")
        url = reverse("ops:saloon-threshold-template", args=[w["saloon"].pk])

        for target in (foreign.pk, global_tpl.pk, 999999):
            res = client.put(url, {"template_id": target}, format="json")
            assert res.status_code == 404 and res.data == {"error": "template not found"}
        assert client.put(url, {"template_id": "x"}, format="json").status_code == 400
        for probe in (foreign_saloon.pk, 999999):
            foreign_url = reverse("ops:saloon-threshold-template", args=[probe])
            assert client.get(foreign_url).status_code == 404
            assert client.put(foreign_url, {"template_id": None}, format="json").status_code == 404

    def test_chain_without_any_published_template_is_none(self):
        w = _make_world()
        # RESTRICT FKs: bands first, then the templates themselves.
        LifecycleThresholdRule.objects.all().delete()
        LifecycleTemplate.objects.all().delete()
        res = _as(w, "worker").get(
            reverse("ops:saloon-threshold-template", args=[w["saloon"].pk])
        )
        assert res.status_code == 200
        assert res.data["source"] == SOURCE_NONE
        assert res.data["effective_template_id"] is None


# ---------------------------------------------------------------------------
# Resolution chain (service level — no HTTP)
# ---------------------------------------------------------------------------


class TestResolutionChain:
    def test_org_default_then_global_default_then_none(self):
        w = _make_world()
        saloon, flock = w["saloon_b"], w["flocks"][w["saloon_b"].pk]

        # Step 2 wins while a published org+breed template exists.
        template, source = effective_template(flock, saloon)
        assert source == SOURCE_ORG_BREED and template.pk == w["live"].pk

        # Remove the breed match ⇒ step 3 (org default) wins.
        w["live"].breed_type = "cobb"
        w["live"].save(update_fields=["breed_type"])
        template, source = effective_template(flock, saloon)
        assert source == SOURCE_ORG_DEFAULT and template.pk == w["live"].pk

        # Demote the org default ⇒ step 4 (the built-in catalog) wins.
        builtin = LifecycleTemplate.objects.create(
            org=None, breed_type=None, name="Builtin", is_default=True
        )
        w["live"].is_default = False
        w["live"].save(update_fields=["is_default"])
        template, source = effective_template(flock, saloon)
        assert source == SOURCE_GLOBAL_DEFAULT and template.pk == builtin.pk

        # Nothing published anywhere ⇒ inert (RESTRICT FKs: bands first).
        LifecycleThresholdRule.objects.all().delete()
        LifecycleTemplate.objects.all().delete()
        template, source = effective_template(flock, saloon)
        assert template is None and source == SOURCE_NONE

    def test_drafts_are_invisible_at_every_step(self):
        w = _make_world()
        draft = _draft(w, breed="ross")  # same breed as the live one
        draft.is_default = True
        draft.save(update_fields=["is_default"])
        template, source = effective_template(w["flocks"][w["saloon"].pk], w["saloon"])
        assert template.pk == w["live"].pk and source == SOURCE_ORG_BREED

    def test_an_orphaned_assignment_falls_through_instead_of_going_silent(self):
        """A broken assignment (draft / foreign / gone) must degrade to the
        previous behaviour, never to no bands at all."""
        w = _make_world()
        saloon, flock = w["saloon"], w["flocks"][w["saloon"].pk]

        draft = _draft(w, breed="broiler")
        saloon.threshold_template = draft
        saloon.save(update_fields=["threshold_template"])
        template, source = effective_template(flock, saloon)
        assert source == SOURCE_ORG_BREED and template.pk == w["live"].pk

        foreign = LifecycleTemplate.objects.create(
            org=w["other"], breed_type="broiler", name="F"
        )
        saloon.threshold_template = foreign
        saloon.save(update_fields=["threshold_template"])
        template, source = effective_template(flock, saloon)
        assert source == SOURCE_ORG_BREED and template.pk == w["live"].pk

    def test_resolver_accepts_a_flock_only_caller(self):
        """KPI compliance only holds the flock — the saloon column is read for it."""
        w = _make_world()
        plan = _draft(w, breed="broiler", status=LifecycleTemplate.PUBLISHED)
        saloon = w["saloon"]
        saloon.threshold_template = plan
        saloon.save(update_fields=["threshold_template"])
        template, source = effective_template(w["flocks"][saloon.pk])
        assert template.pk == plan.pk and source == SOURCE_ASSIGNED

    def test_build_hash_uses_the_assigned_template(self):
        w = _make_world()
        plan = _draft(w, breed="broiler", status=LifecycleTemplate.PUBLISHED)
        saloon = w["saloon"]
        assert _bands(saloon)["temp_max"] == "30.00"
        saloon.threshold_template = plan
        saloon.save(update_fields=["threshold_template"])
        mapping = _bands(saloon)
        assert mapping["temp_max"] == "29.00"
        assert mapping["nh3_enabled"] == "1"
        assert mapping["operational_state"] == "ACTIVE_FLOCK"
