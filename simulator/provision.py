#!/usr/bin/env python3
"""provision.py — create a NEW gateway the way a farm is really installed.

WHY THIS EXISTS
---------------
The twin drives gateways that already exist. This creates them, so the fleet can
grow without a human clicking through the install flow — and so that growth goes
through the SAME path a real installation does.

It is deliberately NOT part of the device. `twin.py` implements the firmware
contract and imports nothing from the backend; this module is the opposite — a
test harness that drives the backend's own API views, exactly as
`tests/test_m10_03_bootstrap_lane_live.py` proves them. Keeping the two apart is
what makes the twin an independent check rather than a tautology.

THE FLOW (one gateway)
----------------------
  1. the factory registers a unit, with no organisation     -> password + claim code
  2. a manager asks for a farm the org does not have yet    -> a request row
  3. support approves: the farm and the jobs appear, and NOTHING is minted
  4. support assigns the job to an installer
  5. the installer BINDS by claim code                      -> the slot exists
  6. a saloon, a sensor slot and a pack mapping are created -> config v2
  7. the installer submits                                  -> PROVISIONED

Steps 1-6 are what a person does. Step 7 is included because submit is
idempotent by design ("the Installer presses Done again when the HTTP response
was lost"), so automating it changes nothing about the semantics.

OUTPUT
------
Writes a credentials file the fleet merges into its own, so a newly provisioned
gateway starts reporting on the next fleet reload:

    {"GW-AUTO-000001": {"username": "...", "password": "...", "farm_id": 12,
                        "hardware_id": "AA:BB:CC:000001",
                        "packs": ["a1b2c3d4", "e5f6a7b8"]}, ...}

Run it inside the backend image, where Django lives:

    docker compose ... run --rm provisioner
    # or
    docker run --rm -e ... ghcr.io/morghdari/backend:vps-03 python /sim/provision.py --count 2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

LOG = logging.getLogger("sim.provision")

# The state directory is shared with the simulator container by a volume.
STATE_DIR = os.environ.get("SIM_STATE_DIR", "/sim-state")
OUTPUT_PATH = os.path.join(STATE_DIR, "provisioned.json")


def _setup_django():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
    import django

    django.setup()


def _make_caller():
    """The in-process HTTP caller the live test uses — same views, same auth."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    def _as(user, view, method, path, data=None, claims=None, **kwargs):
        request = getattr(APIRequestFactory(), method.lower())(
            path, data=data or {}, format="json"
        )
        force_authenticate(request, user=user)
        request.jwt_claims = claims or {}
        return view(request, **kwargs)

    return _as


def provision_one(index: int, _as, models) -> dict:
    """Run the whole flow for one gateway. Returns its credentials."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    tag = f"{int(time.time()) % 100000}{index:02d}"
    unit_id = f"GW-AUTO-{tag}"
    hardware_id = f"AA:BB:CC:{int(tag) % 1000000:06X}"

    # A fresh org per gateway keeps runs independent and repeatable: nothing
    # here has to be unique across time except the usernames, which the tag
    # already guarantees.
    #
    # The org is created over the admin alias because `organizations` is FORCE
    # ROW LEVEL SECURITY and there is no tenant to scope it to yet — this is the
    # same cross-org posture the sweeps and the device config lookup use.
    from apps.tenants.context import admin_alias

    org = models.Organization.objects.using(admin_alias()).create(
        name=f"auto-org-{tag}")
    province = models.Province.objects.create(name=f"P-{tag}")
    city = models.City.objects.create(province=province, name=f"C-{tag}")

    manager = User.objects.create_user(username=f"auto-manager-{tag}", password="x")
    # Explicit ids, not object references: assigning a User fetched on the
    # default connection to a row being written on the admin alias trips the
    # database router's relation check.
    models.OrgMember.objects.using(admin_alias()).create(
        user_id=manager.pk, org_id=org.pk, role="manager", is_active=True)
    reviewer = models.staff(f"auto-reviewer-{tag}", "admin")
    support = models.staff(f"auto-support-{tag}", "support")
    installer = models.installer(f"auto-installer-{tag}", org)
    factory = models.staff(f"auto-factory-{tag}", "hardware")

    # 1. the factory registers a unit, with NO organisation
    registered = _as(factory, models.unit_collection, "POST",
                     "/api/v1/platform/devices/units/",
                     {"hardware_id": hardware_id, "unit_id": unit_id,
                      "model": "GW-4", "batch": "auto"})
    if registered.status_code != 201:
        raise RuntimeError(f"register failed: {registered.status_code} {registered.data}")
    password = registered.data["password"]
    claim_code = registered.data["claim_code"]
    LOG.info("%s registered", unit_id)

    # 2. a manager asks for a farm the org does not have yet
    request_row = _as(manager, models.gateway_requests, "POST",
                      "/api/v1/gateway-requests/",
                      {"farm_spec": {"name": f"Auto Farm {tag}",
                                     "province_id": province.pk, "city_id": city.pk},
                       "expected_gateways": 1},
                      claims={"org_id": str(org.org_id), "roles": ["manager"]})
    if request_row.status_code != 201:
        raise RuntimeError(f"request failed: {request_row.status_code} {request_row.data}")
    request_id = request_row.data["id"]

    # 3. approval materialises the farm and the job, and mints NOTHING
    approval = _as(reviewer, models.review_gateway_request, "POST",
                   f"/api/v1/gateway-requests/{request_id}/review/",
                   {"decision": "APPROVE"}, claims={}, request_id=request_id)
    if approval.status_code != 200:
        raise RuntimeError(f"approval failed: {approval.status_code} {approval.data}")
    farm_pk = approval.data["farm_id"]
    LOG.info("%s approved -> farm %s, %s job(s)", unit_id, farm_pk,
             approval.data["jobs_created"])

    from apps.provisioning.models import InstallationJob

    job = InstallationJob.objects.filter(request_id=request_id).first()
    if job is None:
        raise RuntimeError("no installation job was created")

    # 4. support assigns the job
    assigned = _as(support, models.install_job_assign, "POST",
                   f"/api/v1/provisioning/installations/{job.pk}/assign/",
                   {"installer_id": installer.pk}, claims={}, job_id=job.pk)
    if assigned.status_code != 200:
        raise RuntimeError(f"assign failed: {assigned.status_code} {assigned.data}")

    # 5. the bind by claim code — this is what creates the gateway slot
    bound = _as(installer, models.install_job_bind, "POST",
                f"/api/v1/provisioning/installations/{job.pk}/gateway/",
                {"unit_id": unit_id, "claim_code": claim_code},
                claims={}, job_id=job.pk)
    if bound.status_code != 200:
        raise RuntimeError(f"bind failed: {bound.status_code} {bound.data}")
    LOG.info("%s bound (slot %s)", unit_id, bound.data["job"]["gateway_slot_id"])

    # 6. a saloon, a sensor slot, and a pack mapping — without the mapping the
    #    gateway's telemetry is dead-lettered UNKNOWN_SLOT, so this is not
    #    optional decoration.
    packs = _attach_sensors(_as, models, org, job, installer, tag)

    # 7. the installer submits; idempotent by design
    submitted = _as(installer, models.install_job_submit, "POST",
                    f"/api/v1/provisioning/installations/{job.pk}/submit/",
                    {}, claims={}, job_id=job.pk)
    if submitted.status_code != 200:
        LOG.warning("%s submit returned %s %s", unit_id,
                    submitted.status_code, submitted.data)

    return {"username": unit_id, "password": password, "farm_id": farm_pk,
            "hardware_id": hardware_id, "packs": packs}


def _attach_sensors(_as, models, org, job, installer, tag) -> list:
    """Create a saloon + sensor slot and map two packs, so telemetry resolves."""
    from apps.devices.models import SaloonPosition, SaloonSensorSlot
    from apps.farms.models import Saloon

    saloon = Saloon.objects.create(org=org, farm_id=job.farm_id,
                                   name=f"Auto Saloon {tag}")
    position = SaloonPosition.objects.create(saloon=saloon, org=org, label="north")

    slot_id = f"SS-AUTO-{tag}"
    created = _as(installer, models.saloon_sensor_slot_create, "POST",
                  f"/api/v1/saloons/{saloon.pk}/sensor-slots/",
                  {"slot_id": slot_id, "position_id": position.pk,
                   "allowed_metric_keys": ["temp", "humidity", "nh3", "co",
                                           "lux", "water_l", "power_mains",
                                           "power_gen"]},
                  claims={"org_id": str(org.org_id)}, saloon_id=saloon.pk)
    if created.status_code != 201:
        raise RuntimeError(f"sensor slot failed: {created.status_code} {created.data}")

    packs = [f"{int(tag) % 100000000:08x}", f"{(int(tag) + 1) % 100000000:08x}"]
    for pack_id in packs:
        mapped = _as(installer, models.install_job_sensor_map, "POST",
                     f"/api/v1/provisioning/installations/{job.pk}/sensors/",
                     {"pack_id": pack_id, "slot_id": slot_id},
                     claims={}, job_id=job.pk)
        if mapped.status_code != 201:
            raise RuntimeError(f"pack map failed: {mapped.status_code} {mapped.data}")
    return packs


class _Models:
    """Late-bound references to the views and models the flow drives.

    Imported lazily inside `load()` so this module can be imported (and its
    docstring read) without Django configured.
    """

    def load(self):
        from apps.accounts.models import OrgMember
        from apps.devices.api_units import unit_collection
        from apps.farms.api import saloon_sensor_slot_create
        from apps.geography.models import City, Province
        from apps.provisioning.api import (
            gateway_requests,
            review_gateway_request,
        )
        from apps.provisioning.installations import (
            install_job_assign,
            install_job_bind,
            install_job_sensor_map,
            install_job_submit,
        )
        from apps.tenants.models import Organization

        self.Organization = Organization
        self.OrgMember = OrgMember
        self.Province = Province
        self.City = City
        self.unit_collection = unit_collection
        self.saloon_sensor_slot_create = saloon_sensor_slot_create
        self.gateway_requests = gateway_requests
        self.review_gateway_request = review_gateway_request
        self.install_job_assign = install_job_assign
        self.install_job_bind = install_job_bind
        self.install_job_sensor_map = install_job_sensor_map
        self.install_job_submit = install_job_submit

    @staticmethod
    def staff(username, role):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username, defaults={"is_staff": True, "is_active": True})
        if created:
            user.set_unusable_password()
            user.save()
        return user

    @staticmethod
    def installer(username, org):
        from django.contrib.auth import get_user_model

        from apps.accounts.models import OrgMember

        User = get_user_model()
        user = User.objects.create_user(username=username, password="x")
        from apps.tenants.context import admin_alias

        OrgMember.objects.using(admin_alias()).create(
            user_id=user.pk, org_id=org.pk, role="installer", is_active=True)
        return user


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=1,
                        help="how many gateways to install")
    parser.add_argument("--output", default=OUTPUT_PATH,
                        help="where to write the credentials the fleet reads")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    _setup_django()
    models = _Models()
    models.load()
    caller = _make_caller()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    existing = {}
    if os.path.exists(args.output):
        try:
            with open(args.output, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (ValueError, OSError) as exc:
            LOG.warning("ignoring unreadable %s (%s)", args.output, exc)

    for index in range(args.count):
        credentials = provision_one(index, caller, models)
        existing[credentials["username"]] = credentials
        LOG.info("provisioned %s (farm %s, packs %s)", credentials["username"],
                 credentials["farm_id"], credentials["packs"])

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(existing, handle, indent=2)
    os.chmod(args.output, 0o600)
    LOG.info("wrote %d gateway credential(s) to %s", len(existing), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
