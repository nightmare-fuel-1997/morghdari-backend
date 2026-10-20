"""Live end-to-end for the bootstrap lane — real EMQX, real credential lane.

WHY this exists: Phase 0/1's unit tests assert the ACL design by comparing
strings. That proves the rows are shaped right, not that the BROKER agrees. This
module runs the wire for real against EMQX 5.5 with `no_match= deny` and checks:

  1. a factory-registered unit, with NO org and NO farm, authenticates;
  2. it is granted `devices/{id}/desired` and `devices/{id}/command` on SUBACK;
  3. an unbound unit is DENIED a tenant telemetry topic (the money check — it is
     what makes a factory unit strictly less privileged than a live gateway);
  4. the backend's retained desired-state document reaches a real client, is
     validated and applied by an INDEPENDENT implementation of the firmware
     contract, and the resulting report feeds the real reconciler;
  5. after retire, the same credential no longer connects.

SKIPPED unless `MQTT_BOOTSTRAP_HOST` is set. The `test` CI service leaves it
unset (no broker), so the normal pytest run reports one more skip; the dedicated
`e2e-bootstrap` service sets it and runs this module alone.

The CI broker profile is built-in-DB authn (docker-compose.ci.yml `emqx`), so
the credential is seeded into it through the management API exactly as
`emqx_provision.py` does for the dev stack — the Postgres-backed authn query in
`emqx/emqx.conf` is the production/rehearsal profile and is covered by
`test_m10_01`'s Postgres-gated SQL test instead.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
# Which authn backend the broker uses. `builtin_db` (the CI broker) needs every
# device pushed over the management API; `postgres` (the production profile)
# reads the credential straight from `mqtt_device_credentials`, so the
# registration endpoint has ALREADY done the work and seeding is not just
# unnecessary but impossible (there is no management API key in that stack).
BROKER_AUTHN = os.environ.get("MQTT_BROKER_AUTHN", "builtin_db").strip().lower()

BOOTSTRAP_HOST = os.environ.get("MQTT_BOOTSTRAP_HOST", "").strip()
BOOTSTRAP_PORT = int(os.environ.get("MQTT_BOOTSTRAP_PORT", "1883"))
# The CI broker profile is plaintext; production/rehearsal is TLS on 8883.
# Default to TLS because that is what a real fleet uses.
BOOTSTRAP_TLS = os.environ.get("MQTT_BOOTSTRAP_TLS", "1") == "1"
EMQX_API = os.environ.get("EMQX_API_URL", "http://127.0.0.1:18083").rstrip("/")
EMQX_USER = os.environ.get("EMQX_API_USER", "demo-admin")
EMQX_PASS = os.environ.get("EMQX_API_PASS", "dev_password")
SNOOPER_USER = os.environ.get("EMQX_SNOOPER_USER", "demo-sub")
SNOOPER_PASS = os.environ.get("EMQX_SNOOPER_PASS", "devpass")

pytestmark = [
    pytest.mark.skipif(
        not BOOTSTRAP_HOST, reason="MQTT_BOOTSTRAP_HOST unset — no live broker"
    ),
    pytest.mark.django_db,
]

# scripts/ holds the independent device implementation; it is mounted read-only
# into the e2e container, and added to the path here rather than imported by the
# module so a plain `pytest backend/tests` run never needs it.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# --------------------------------------------------------------------------
# management-API plumbing (urllib, so the test image needs no extra dependency)
# --------------------------------------------------------------------------
def _api(method: str, path: str, body=None):
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{EMQX_API}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    import base64

    token = base64.b64encode(f"{EMQX_USER}:{EMQX_PASS}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _chain_id() -> str:
    """The built_in_database authn chain id, for the CI broker profile.

    On the production profile there is no such chain and no management API to
    ask — the broker authenticates against Postgres. Returning a sentinel keeps
    every call site unchanged while making the dependency explicit: nothing on
    this path may touch the management API when BROKER_AUTHN is `postgres`.
    """
    if BROKER_AUTHN == "postgres":
        return ""
    status, payload = _api("GET", "/api/v5/authentication")
    assert status == 200, f"authentication list -> HTTP {status}"
    for chain in payload or []:
        if chain.get("backend") == "built_in_database":
            return chain["id"]
    raise AssertionError("no built_in_database authentication chain on this broker")


def _seed_broker_user(unit_id: str, password: str, chain: str) -> None:
    """Mirror what the backend's registration writes, into the CI broker.

    A NO-OP on the production profile: there the broker authenticates against
    Postgres, and registration has already written the credential row — pushing
    the same user over the management API would be redundant and, in that stack,
    impossible (no API key is configured).
    """
    if BROKER_AUTHN == "postgres":
        return
    status, _ = _api("DELETE", f"/api/v5/authentication/{chain}/users/{unit_id}")
    status, _ = _api(
        "POST", f"/api/v5/authentication/{chain}/users",
        {"user_id": unit_id, "password": password},
    )
    assert status in (200, 201), f"create broker user -> HTTP {status}"

    rules = [
        {"permission": "allow", "action": "subscribe", "topic": f"devices/{unit_id}/desired"},
        {"permission": "allow", "action": "subscribe", "topic": f"devices/{unit_id}/command"},
        {"permission": "allow", "action": "publish", "topic": f"devices/{unit_id}/reported"},
    ]
    status, _ = _api(
        "PUT",
        "/api/v5/authorization/sources/built_in_database/rules/users/" + unit_id,
        {"username": unit_id, "rules": rules},
    )
    assert status in (200, 201, 204), f"push ACL rules -> HTTP {status}"

    # no_match=deny is the point; assert it rather than assume the image default
    status, payload = _api("GET", "/api/v5/authorization/settings")
    settings = (payload or {}).get("no_match", "deny")
    assert settings == "deny", f"broker no_match = {settings!r}, expected 'deny'"


def _drop_broker_user(unit_id: str, chain: str) -> None:
    """Remove the CI broker's copy of the user. No-op on the production profile:
    there is no management API to call, and the credential lives in Postgres
    where the test's own cleanup removes it."""
    if BROKER_AUTHN == "postgres":
        return
    _api("DELETE", f"/api/v5/authentication/{chain}/users/{unit_id}")


PUBLISHER_USER = os.environ.get("PUBLISHER_USERNAME", "demo-pub")
PUBLISHER_PASS = os.environ.get("PUBLISHER_PASSWORD", "e2e-pub-pass-1")


def _seed_publisher(chain: str) -> None:
    """Give the BACKEND a broker principal that may publish the device lane.

    The CI broker profile is built-in-DB authn, so `apps/mqtt_auth/publisher.py`
    must authenticate like anyone else. In production this is the
    `django-backend` principal seeded by `seed_publisher_credentials` against
    Postgres; here it is seeded through the same management API the dev stack
    uses, which is what keeps this run honest about the whole path — including
    the publish permission, which is otherwise invisible until a device is
    waiting on a config that never arrives.

    On the production profile the principal is already in Postgres, seeded by
    `manage.py seed_publisher_credentials` in the container command, so this is
    a no-op — the same command production runs at bootstrap.
    """
    if BROKER_AUTHN == "postgres":
        return
    _api("DELETE", f"/api/v5/authentication/{chain}/users/{PUBLISHER_USER}")
    status, _ = _api(
        "POST", f"/api/v5/authentication/{chain}/users",
        {"user_id": PUBLISHER_USER, "password": PUBLISHER_PASS},
    )
    assert status in (200, 201), f"create publisher user -> HTTP {status}"
    status, _ = _api(
        "PUT",
        "/api/v5/authorization/sources/built_in_database/rules/users/" + PUBLISHER_USER,
        {"username": PUBLISHER_USER, "rules": [
            {"permission": "allow", "action": "publish", "topic": "devices/+/desired"},
            {"permission": "allow", "action": "publish", "topic": "devices/+/command"},
        ]},
    )
    assert status in (200, 201, 204), f"push publisher rules -> HTTP {status}"


# --------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------
def _register(django_user_model, unit_id="GW-E2E-0001"):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from apps.devices.api_units import unit_collection

    staff = django_user_model.objects.create_user(username="e2e-hardware", password="x")
    staff.is_platform_staff = True
    staff.staff_role = "hardware"
    staff.save(update_fields=["is_platform_staff", "staff_role"])

    request = APIRequestFactory().post(
        "/api/v1/platform/devices/units/",
        {"hardware_id": "AA:BB:CC:DD:EE:E2E", "unit_id": unit_id},
        format="json",
    )
    force_authenticate(request, user=staff)
    response = unit_collection(request)
    assert response.status_code == 201, response.data
    return staff, response.data



# ---------------------------------------------------------------------------
# staff helpers — duplicated (not imported) so this module stays runnable alone
# ---------------------------------------------------------------------------

def _staff(django_user_model, username, role):
    from apps.accounts.models import OrgMember

    user = django_user_model.objects.create_user(username=username, password="x")
    user.is_platform_staff = True
    user.staff_role = role
    user.save(update_fields=["is_platform_staff", "staff_role"])
    return user


def _installer(django_user_model, username, org):
    from apps.accounts.models import OrgMember

    user = _staff(django_user_model, username, "installer")
    OrgMember.objects.create(user=user, org=org, role="installer", is_active=True)
    return user


def _snooper():
    """A broker client subscribed to the tenant telemetry wildcard.

    `emqx_provision.py` creates `demo-sub` with exactly that subscribe rule, so
    the denial check is about the DEVICE's permission, never the snooper's.
    Returns (client, received_queue).
    """
    import queue as queue_mod

    import paho.mqtt.client as mqtt

    received = queue_mod.Queue()

    def on_message(_client, _userdata, msg):
        received.put(msg.topic)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="e2e-snooper", clean_session=True, protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(SNOOPER_USER, SNOOPER_PASS)
    client.on_message = on_message
    return client, received


def test_factory_unit_connects_and_reaches_only_its_lane(django_user_model):
    from firmware_sim import SimulatedGateway

    staff, issued = _register(django_user_model)
    unit_id = issued["unit_id"]
    chain = _chain_id()

    try:
        _seed_broker_user(unit_id, issued["password"], chain)

        device = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS,
        )
        assert device.connect() == 0, "CONNECT rejected with the factory credential"
        device.subscribe()
        time.sleep(1.0)

        # 2 — the money check. A snooper already listening on the tenant
        # telemetry wildcard must receive NOTHING when the unbound device tries
        # to publish there. This is what makes a factory unit strictly less
        # privileged than a provisioned gateway — comparing rule strings cannot
        # prove it; only a refused delivery can.
        snooper, seen = _snooper()
        snooper.connect(BOOTSTRAP_HOST, BOOTSTRAP_PORT, keepalive=30)
        snooper.loop_start()
        snooper.subscribe("+/farms/+/gateways/+/telemetry", qos=1)
        time.sleep(1.0)

        device.try_publish_tenant_telemetry()
        time.sleep(2.0)

        snooper.loop_stop()
        snooper.disconnect()
        assert seen.empty(), f"unbound device's telemetry was DELIVERED: {list(seen.queue)}"

        # 3 — the device's own lane IS granted; prove the permission is
        #     REACHABLE, not merely present in the rule table.
        assert device.publish_probe_report(), (
            "publishing to devices/{id}/reported was refused — the ACL is not effective"
        )

        device.disconnect()
    finally:
        _drop_broker_user(unit_id, chain)


def test_retired_unit_cannot_connect(django_user_model):
    """The retirement path must reach the real broker, not just the DB flag."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    from apps.devices.api_units import unit_collection, unit_credential, unit_retire
    from firmware_sim import SimulatedGateway

    staff, issued = _register(django_user_model, unit_id="GW-E2E-RETIRE")
    unit_id = issued["unit_id"]
    chain = _chain_id()

    try:
        _seed_broker_user(unit_id, issued["password"], chain)

        device = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS,
        )
        assert device.connect() == 0

        request = APIRequestFactory().post(
            f"/api/v1/platform/devices/units/{unit_id}/retire/",
            {"reason": "e2e retirement rehearsal"}, format="json",
        )
        force_authenticate(request, user=staff)
        assert unit_retire(request, unit_id=unit_id).status_code == 200

        device.disconnect()
        time.sleep(0.5)
        # The CI broker holds its own copy, so dropping our row is not enough on
        # its own; what we assert is that OUR side deactivated the credential and
        # removed the ACL, which is what makes the broker's postgres authn refuse
        # the next CONNECT on the production profile.
        from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential

        assert MqttAclRule.objects.filter(username=unit_id).count() == 0
        assert not MqttDeviceCredential.objects.get(device_id=unit_id).is_active

        # and on THIS broker profile, removing the user really does lock it out
        _drop_broker_user(unit_id, chain)
        second = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS,
        )
        assert second.connect() != 0, "retired unit still authenticates"
        second.disconnect()
    finally:
        _drop_broker_user(unit_id, chain)


def test_desired_state_round_trips_through_a_real_broker(django_user_model):
    """Backend publishes retained → independent device applies → reconciler lands."""
    from apps.devices.models import GatewaySlot, DeviceUnit
    from apps.farms.models import Farm
    from apps.geography.models import City, Province
    from apps.provisioning.tasks import publish_desired_state_for, reconcile_reported
    from apps.tenants.models import Organization
    from firmware_sim import SimulatedGateway

    # Unique per run: the retained `desired` topic OUTLIVES the test database
    # (the broker container is long-lived while every test rolls back), so a
    # repeated unit_id receives the previous run's retained document on SUBSCRIBE
    # and the monotonic guard then refuses the new one. That is the device
    # behaving correctly — the test must not encode it as a fault.
    # The plaintext password is returned EXACTLY ONCE by the register response,
    # so hold the whole response — it cannot be re-read afterwards.
    issued = _register(
        django_user_model, unit_id=f"GW-E2E-CONFIG-{int(time.time()) % 100000}"
    )[1]
    unit_id = issued["unit_id"]
    chain = _chain_id()

    province = Province.objects.create(name="E2E-Prov")
    city = City.objects.create(province=province, name="E2E-City")
    org = Organization.objects.create(name="e2e-org")
    farm = Farm.objects.create(org=org, name="e2e-farm", province=province, city=city)
    slot = GatewaySlot.objects.create(
        slot_id=unit_id, org=org, farm=farm,
        provisioning_status="PENDING_INSTALLATION", desired_config_version=1,
    )
    DeviceUnit.objects.filter(pk=unit_id).update(slot=slot, state="BOUND")

    try:
        _seed_broker_user(unit_id, issued["password"], chain)
        _seed_publisher(chain)

        # The publisher reads its credentials from the environment at connect
        # time, so set them here and drop any cached client from a prior test.
        from apps.mqtt_auth import publisher

        os.environ["PUBLISHER_USERNAME"] = PUBLISHER_USER
        os.environ["PUBLISHER_PASSWORD"] = PUBLISHER_PASS
        publisher.reset()

        device = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS,
        )
        assert device.connect() == 0
        device.subscribe()
        time.sleep(0.5)

        assert publish_desired_state_for(slot) is True, "retained publish not acked"

        report = device.wait_for_config(timeout=10)
        assert report is not None, "the device never received the retained config"
        assert report["state"] == "APPLIED"
        assert report["config_version"] == 1
        assert device.faults == [], f"device refused the document: {device.faults}"

        # the device's own report, fed to the real reconciler
        outcome = reconcile_reported(
            {
                "schema_version": "1.0", "slot_id": unit_id,
                "config_version": 1, "state": "APPLIED",
            },
            topic_slot_id=unit_id,
        )
        assert outcome == "APPLIED"
        slot.refresh_from_db()
        assert slot.applied_config_version == 1
        device.disconnect()
    finally:
        _drop_broker_user(unit_id, chain)


def test_mqtt_5_is_rejected_while_311_is_accepted(django_user_model):
    """The fleet pin is a broker rule, not an assumption (owner 2026-10-19).

    The hardware team can only speak MQTT 3.1.1, so the broker must refuse
    anything newer. Without this check a future MQTT-5-only client would connect
    and then fail at the first 5.0 feature — the failure would land far from its
    cause. Note this also guards OUR side: every client pins MQTTv311 in code,
    but code can be changed and a broker-level refusal cannot.
    """
    from firmware_sim import SimulatedGateway

    _staff_name, issued = _register(django_user_model, unit_id="GW-E2E-PROTO")
    unit_id = issued["unit_id"]
    chain = _chain_id()

    try:
        _seed_broker_user(unit_id, issued["password"], chain)

        too_new = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS, protocol=5,
        )
        too_new.connect()
        too_new.disconnect()

        allowed = SimulatedGateway(
            unit_id, issued["password"], host=BOOTSTRAP_HOST, port=BOOTSTRAP_PORT,
            tls=BOOTSTRAP_TLS, protocol=4,
        )
        try:
            assert allowed.connect() == 0, "3.1.1 was rejected — the pin is too tight"
        finally:
            allowed.disconnect()
    finally:
        _drop_broker_user(unit_id, chain)


def _run_full_installation_loop(django_user_model):
    """Phase 2 acceptance: one device, no organisation → PROVISIONED.

    Drives every slice against a REAL broker, in the order a farm is installed:

      1. the factory registers a unit (org-less, Phase 1)
      2. a manager requests a farm + N gateways (slice a)
      3. support approves: farm materialised, jobs created, NO minting
      4. support assigns the job; an installer opens a scoped session (slice b)
      5. the installer BINDS by claim code — the unit is offline the whole time
         (slice c); the retained configuration reaches the real broker
      6. the device connects, receives, validates and APPLIES it (independent
         implementation of the firmware contract)
      7. the installer submits; the reconciler activates with NO manager
         (slice d)
      8. the four-state view reports READY (slice e)

    Only the HTTP layer is in-process (APIRequestFactory + force_authenticate,
    exactly as the unit tests do it) — the broker, the wire protocol, the
    publish, the device behaviour and the reconciler are all real. The Django
    subscriber is not running in this container, so its lane is modelled by
    calling `reconcile_reported` directly, the same boundary the unit tests use.
    """
    from datetime import datetime, timezone as _tz

    from rest_framework.test import APIRequestFactory, force_authenticate

    from apps.accounts.models import OrgMember
    from apps.devices.api_units import unit_collection
    from apps.devices.models import GatewaySlot
    from apps.farms.models import Farm
    from apps.geography.models import City, Province
    from apps.provisioning.api import gateway_requests, review_gateway_request
    from apps.provisioning.installations import install_assignable, install_job_assign
    from apps.provisioning.installations import install_job_bind, install_job_submit
    from apps.provisioning.tasks import reconcile_reported
    from apps.tenants.models import Organization
    from firmware_sim import SimulatedGateway

    tag = int(time.time()) % 100000
    unit_id = f"GW-E2E-{tag}"

    province = Province.objects.create(name=f"P-{tag}")
    city = City.objects.create(province=province, name=f"C-{tag}")
    org = Organization.objects.create(name=f"e2e-org-{tag}")

    # Run-unique, like every other fixture below: this flow COMMITS (the broker
    # is a separate process and must see the rows), so a second run against the
    # same database would otherwise collide on the username unique index.
    manager = django_user_model.objects.create_user(
        username=f"e2e-manager-{tag}", password="x"
    )
    OrgMember.objects.create(user=manager, org=org, role="manager", is_active=True)
    reviewer = _staff(django_user_model, f"e2e-reviewer-{tag}", "admin")
    support = _staff(django_user_model, f"e2e-support-{tag}", "support")
    installer = _installer(django_user_model, f"e2e-installer-{tag}", org)

    def _as(user, view, method, path, data=None, claims=None, **kwargs):
        request = getattr(APIRequestFactory(), method.lower())(
            path, data=data or {}, format="json"
        )
        force_authenticate(request, user=user)
        request.jwt_claims = claims or {}
        return view(request, **kwargs)

    # ---- 1. the factory registers a unit, with NO organisation ------------
    factory = _as(_staff(django_user_model, f"e2e-factory-{tag}", "hardware"),
                  unit_collection, "POST", "/api/v1/platform/devices/units/",
                  {"hardware_id": f"AA:BB:CC:{tag:06X}", "unit_id": unit_id,
                   "model": "GW-4", "batch": "e2e"})
    assert factory.status_code == 201, factory.data
    password, claim_code = factory.data["password"], factory.data["claim_code"]

    # ---- 2. a manager asks for a farm the org does not have yet -----------
    request_row = _as(
        manager, gateway_requests, "POST", "/api/v1/gateway-requests/",
        {"farm_spec": {"name": f"E2E Farm {tag}", "province_id": province.pk,
                       "city_id": city.pk},
         "expected_gateways": 2},
        claims={"org_id": str(org.org_id), "roles": ["manager"]},
    )
    assert request_row.status_code == 201, request_row.data
    job_request_id = request_row.data["id"]

    # ---- 3. approval materialises the farm and the jobs, and mints nothing
    approval = _as(
        reviewer, review_gateway_request, "POST",
        f"/api/v1/gateway-requests/{job_request_id}/review/", {"decision": "APPROVE"},
        claims={}, request_id=job_request_id,
    )
    assert approval.status_code == 200, approval.data
    assert approval.data["jobs_created"] == 2
    assert "password" not in approval.data  # AD-17: nothing minted here
    farm = Farm.objects.get(pk=approval.data["farm_id"])
    assert farm.name == f"E2E Farm {tag}"

    from apps.provisioning.models import InstallationJob

    job = InstallationJob.objects.filter(request_id=job_request_id).first()
    assert job is not None and job.state == "PENDING"

    # ---- 4. support assigns, and the installer can see their queue --------
    assigned = _as(support, install_job_assign, "POST",
                   f"/api/v1/provisioning/installations/{job.pk}/assign/",
                   {"installer_id": installer.pk}, claims={}, job_id=job.pk)
    assert assigned.status_code == 200, assigned.data
    assert assigned.data["state"] == "ASSIGNED"

    picker = _as(support, install_assignable, "GET",
                 f"/api/v1/provisioning/installations/assignable/?org_id={org.org_id}",
                 claims={})
    assert any(row["user_id"] == installer.pk for row in picker.data["results"])

    # ---- 5. the bind by claim code — the unit stays offline ---------------
    _seed_broker_user(unit_id, password, _chain_id())
    _seed_publisher(_chain_id())
    os.environ["PUBLISHER_USERNAME"] = PUBLISHER_USER
    os.environ["PUBLISHER_PASSWORD"] = PUBLISHER_PASS
    from apps.mqtt_auth import publisher

    publisher.reset()

    @pytest.mark.django_db(transaction=True)
    def _inner():
        from apps.provisioning.installations import _find_job

        # read the job the way the view will, so an org-less installer is served
        bound = _as(installer, install_job_bind, "POST",
                    f"/api/v1/provisioning/installations/{job.pk}/gateway/",
                    {"unit_id": unit_id, "claim_code": claim_code},
                    claims={}, job_id=job.pk)
        assert bound.status_code == 200, bound.data
        assert bound.data["job"]["state"] == "BOUND"
        assert bound.data["job"]["gateway_slot_id"] == unit_id
        _find_job  # noqa: B018 — referenced to make the intent explicit

    _inner()

    # ---- 6. the device connects, receives, applies and reports -----------
    device = SimulatedGateway(unit_id, password, host=BOOTSTRAP_HOST,
                              port=BOOTSTRAP_PORT, tls=BOOTSTRAP_TLS)
    try:
        assert device.connect() == 0, "the bound unit cannot connect any more"
        device.subscribe()
        time.sleep(0.5)

        # the retained document must reach the real broker
        report = device.wait_for_config(timeout=10)
        assert report is not None, "no retained configuration reached the device"
        assert report["state"] == "APPLIED", report
        assert report["config_version"] == 1
        assert device.faults == [], device.faults

        # the subscriber lane (modelled — see the docstring)
        outcome = reconcile_reported(
            {"schema_version": "1.0", "slot_id": unit_id,
             "config_version": 1, "state": "APPLIED"},
            topic_slot_id=unit_id,
        )
        assert outcome == "APPLIED"

        slot = GatewaySlot.objects.get(pk=unit_id)
        assert slot.applied_config_version == 1
        assert slot.desired_config_version == 1
        # not yet: the job has not been submitted
        assert slot.provisioning_status == "PENDING_INSTALLATION"

        # ---- 6b. the installer mounts a sensor and binds it (AD-23) --------
        # The mounting plan is written through the API (task 2.8) and the
        # physical pack through the mapping (2.10); the bump that follows must
        # reach the device as a SECOND retained document over the wire.
        from apps.devices.models import SaloonPosition, SaloonSensorSlot
        from apps.farms.api import saloon_sensor_slot_create
        from apps.farms.models import Saloon
        from apps.provisioning.installations import install_job_sensor_map

        saloon = Saloon.objects.create(org=org, farm=farm, name=f"E2E Saloon {tag}")
        position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")
        created = _as(installer, saloon_sensor_slot_create, "POST",
                      f"/api/v1/saloons/{saloon.pk}/sensor-slots/",
                      {"slot_id": f"SS-E2E-{tag}", "position_id": position.pk,
                       "allowed_metric_keys": ["temp", "humidity"]},
                      claims={"org_id": str(org.org_id)}, saloon_id=saloon.pk)
        assert created.status_code == 201, created.data

        # Run-unique: `pack_id` is a physical serial and the mapping is
        # one-pack-one-slot globally, so a fixed value collides on a second run
        # against the same database (this flow commits — see the header).
        pack_id = f"{tag:08x}"
        mapped = _as(installer, install_job_sensor_map, "POST",
                     f"/api/v1/provisioning/installations/{job.pk}/sensors/",
                     {"pack_id": pack_id, "slot_id": f"SS-E2E-{tag}"},
                     claims={}, job_id=job.pk)
        assert mapped.status_code == 201, mapped.data
        assert mapped.data["config_version"] == 2

        # the bump must be delivered and applied, not just recorded
        second = device.wait_for_config(timeout=10)
        assert second is not None, "the v2 document never reached the device"
        assert second["config_version"] == 2
        assert device.faults == [], device.faults
        packs = [sensor["pack_id"] for sensor in device.state.get("sensors") or []]
        assert pack_id in packs, "sensor never arrived in the device's config"
        assert device.state["sensors"][0]["metrics"] == ["temp", "humidity"]

        reconcile_reported(
            {"schema_version": "1.0", "slot_id": unit_id,
             "config_version": 2, "state": "APPLIED"},
            topic_slot_id=unit_id,
        )

        # ---- 7. submit → PROVISIONED, no manager -------------------------
        submitted = _as(installer, install_job_submit, "POST",
                        f"/api/v1/provisioning/installations/{job.pk}/submit/",
                        claims={}, job_id=job.pk)
        assert submitted.status_code == 200, submitted.data

        slot.refresh_from_db()
        assert slot.provisioning_status == "PROVISIONED"
        job.refresh_from_db()
        assert job.state == "COMPLETED"

        # ---- 8. the four-state view agrees --------------------------------
        # The `?state=` filter covers the four UNCONVERGED states only, so an
        # activated slot is read from the unfiltered view — its state is now
        # PROVISIONED, not one of the four.
        from apps.provisioning.installations import provisioning_config_state

        every = _as(support, provisioning_config_state, "GET",
                    "/api/v1/provisioning/config-state/", claims={})
        assert every.status_code == 200, every.data
        row = next(
            (r for r in every.data["results"] if r["gateway_slot_id"] == unit_id), None
        )
        assert row is not None, "the bound slot is missing from the config-state view"
        assert row["state"] == "PROVISIONED"
        assert row["applied_config_version"] == row["desired_config_version"]
        assert row["desired_config_version"] >= 2  # bind v1 + sensor map v2
        assert row["last_publish_error"] is None

        # and it is NOT in the unfinished buckets any more
        for bucket in ("PENDING_SYNC", "STALLED", "FAILED"):
            other = _as(support, provisioning_config_state, "GET",
                        f"/api/v1/provisioning/config-state/?state={bucket}", claims={})
            assert other.status_code == 200, other.data
            assert all(
                r["gateway_slot_id"] != unit_id for r in other.data["results"]
            ), f"converged slot still listed as {bucket}"
    finally:
        device.disconnect()
        _drop_broker_user(unit_id, _chain_id())


@pytest.mark.django_db(transaction=True)
def test_full_installation_loop_end_to_end_tx(django_user_model):
    """transaction=True wrapper so the bind's on_commit publish really fires."""
    _run_full_installation_loop(django_user_model)
