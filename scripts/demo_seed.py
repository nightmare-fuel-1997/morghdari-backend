"""QA demo seed — creates the actors the qa-test-plan.html tests need.

This repo snapshot carries NO demo seeding (the parent project's seed_demo
lives elsewhere), yet the QA plan's fixtures (Section 0 "roles" row) name
demo-owner/owner/Demo Farm. This script rebuilds that world from the models:
two orgs, tenant users + roles, farms/saloons/flocks, gateway slots with
MQTT creds + T1-T9 ACLs, sensor slots + pack mappings, and a published
threshold template + hash recache.

Run INSIDE the daphne container as the admin (BYPASSRLS) role — same
sanctioned bootstrap path as prod_bootstrap.sh:

    docker compose -p morghdari -f docker-compose.prod.yml \
      -f docker-compose.vps.yml run --rm --no-deps \
      -e DATABASE_URL="postgres://morghdari_admin:PW@postgres:5432/morghdari" \
      -v <repo>/scripts/demo_seed.py:/app/demo_seed.py \
      daphne python manage.py shell < /app/demo_seed.py

Idempotent: every write is get_or_create / update_or_create.
"""
import json
from datetime import timedelta

import bcrypt
from django.utils import timezone

from apps.accounts.models import User, PlatformStaffMFA, OrgMember
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule, SaloonOperationalState
from apps.compat.models import LegacySlugMap
from apps.devices.models import (
    GatewayHardwareRegistry, GatewaySaloonZone, GatewaySensorMapping,
    GatewaySlot, HardwareRegistry, SaloonPosition, SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.provisioning.api import gateway_acl_rule_items
from apps.tenants.models import Organization
from apps.telemetry.models import FlockCycle

PW = "password12345"  # QA-only password (>=10, not in COMMON_PASSWORDS)
TEHRAN_OFFSET = timedelta(hours=3, minutes=30)
now = timezone.now()


def mk_user(username, *, org=None, role=None, staff=False, staff_role=None, superuser=False):
    u, created = User.objects.get_or_create(username=username, defaults={"is_staff": staff or bool(superuser),
                                                                          "is_superuser": bool(superuser),
                                                                          "is_platform_staff": staff})
    if not u.is_platform_staff and staff:
        u.is_platform_staff = True; u.save(update_fields=["is_platform_staff"])
    if staff_role and u.staff_role != staff_role:
        u.staff_role = staff_role; u.save(update_fields=["staff_role"])
    u.set_password(PW)
    u.save()
    if org is not None and role is not None:
        OrgMember.objects.get_or_create(org=org, user=u, defaults={"role": role, "is_active": True})
    return u


demo, created = Organization.objects.get_or_create(name="Demo Farm", defaults={"subscription_tier": "pro"})
other, _ = Organization.objects.get_or_create(name="Other Co", defaults={"subscription_tier": "basic"})

owner = mk_user("demo-owner", org=demo, role="owner")
mk_user("manager1", org=demo, role="manager")
mk_user("engineer1", org=demo, role="engineer")
mk_user("worker1", org=demo, role="worker")
mk_user("doctor1", org=demo, role="doctor")
mk_user("installer1", org=demo, role="installer")
mk_user("reviewer1", org=demo, role="reviewer")
mk_user("other-owner", org=other, role="owner")
dual = mk_user("dual-member", org=other, role="worker")
OrgMember.objects.get_or_create(org=demo, user=dual, defaults={"role":"engineer"})
# deterministic oldest for test 1.7: the OTHER-co membership is the earliest
d1 = OrgMember.objects.get(org=other, user=dual); d1.joined_at = now - timedelta(days=9)
d2 = OrgMember.objects.get(org=demo, user=dual); d2.joined_at = now - timedelta(days=2)
OrgMember.objects.bulk_update([d1, d2], ["joined_at"])
gone = mk_user("offboarded", org=other, role="worker")
OrgMember.objects.filter(org=other, user=gone).update(is_active=False)
staff1 = mk_user("support1", staff=True, staff_role="support")
if not PlatformStaffMFA.objects.filter(user=staff1).exists():
    import pyotp
    PlatformStaffMFA.objects.create(user=staff1, secret=pyotp.random_base32(), confirmed_at=timezone.now())
super_u = mk_user("qasuper", superuser=True)
super_u.is_staff = True; super_u.is_superuser = True; super_u.save()

prov = Province.objects.first() or Province.objects.create(name="تهران")
city = City.objects.filter(province=prov).first() or City.objects.create(province=prov, name="tehran-city")

farm1, _ = Farm.objects.get_or_create(org=demo, name="Farm1", defaults={"province": prov, "city": city})
farm2, _ = Farm.objects.get_or_create(org=demo, name="Farm2", defaults={"province": prov, "city": city})
farm_o, _ = Farm.objects.get_or_create(org=other, name="FarmOther", defaults={"province": prov, "city": city})
sal1, _ = Saloon.objects.get_or_create(org=demo, farm=farm1, name="Saloon-A")
sal2, _ = Saloon.objects.get_or_create(org=demo, farm=farm2, name="Saloon-B")
sal_o, _ = Saloon.objects.get_or_create(org=other, farm=farm_o, name="Saloon-X")
for s in (sal1, sal2, sal_o):
    SaloonOperationalState.objects.get_or_create(saloon=s, org=s.org, state="ACTIVE_FLOCK")

now = timezone.now()
for flock_s, org_s in [(sal1, demo), (sal2, demo), (sal_o, other)]:
    FlockCycle.objects.get_or_create(saloon=flock_s, org=org_s, end_date=None,
                                     defaults={"breed_type": " Cobb-500 ".strip(),
                                               "start_date": now - timedelta(days=10),
                                               "placement_count": 1000, "day_old_weight_g": 40})
tmpl, tcreated = LifecycleTemplate.objects.get_or_create(
    org=demo, breed_type="Cobb-500", status="PUBLISHED",
    defaults={"name": "demo-standard", "is_default": True, "published_at": now})
for metric, mx, mn in [("temp", 30, 18), ("humidity", 80, 40), ("lux", 200, None),
                       ("nh3", 25, None), ("co2", 3000, None), ("co", 30, None),
                       ("water_l", None, 5), ("thi", 79, None)]:
    LifecycleThresholdRule.objects.get_or_create(template=tmpl, day_from=0, day_to=45, metric=metric,
                                                 defaults={"max_value": mx, "min_value": mn})
if tcreated and not LifecycleTemplate.objects.filter(org=demo, is_default=True, status="PUBLISHED").exists():
    tmpl.is_default = True; tmpl.save()

def provision_gateway(slot_code, org, farm, saloon, hardware_id):
    slot, _ = GatewaySlot.objects.get_or_create(slot_id=slot_code, defaults={"org": org, "farm": farm,
                                                                              "provisioning_status": "PROVISIONED"})
    GatewaySlot.objects.filter(pk=slot.pk).update(org=org, farm=farm, provisioning_status="PROVISIONED")
    GatewayHardwareRegistry.objects.update_or_create(hardware_id=hardware_id, org=org, removed_at=None,
                                                     defaults={"slot": slot})
    GatewaySaloonZone.objects.get_or_create(gateway_slot=slot, saloon=saloon, defaults={"org": org})
    pos, _ = SaloonPosition.objects.get_or_create(saloon=saloon, org=org, label=f"pos-{slot_code}")
    password = slot_code + "-mqtt-pw"
    MqttDeviceCredential.objects.update_or_create(
        device_id=slot_code,
        defaults={"username": slot_code, "password_hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                  "is_active": True, "expires_at": None})
    for item in gateway_acl_rule_items(str(org.org_id), farm.id, slot_code):
        MqttAclRule.objects.get_or_create(username=slot_code, topic=item["topic"], action=item["action"],
                                          defaults={"permission": item["permission"]})
    return slot, password

slot1, pw1 = provision_gateway("GW-QA0001", demo, farm1, sal1, "HWM-QA01")
slot2, pw2 = provision_gateway("GW-QA0002", demo, farm2, sal2, "HWM-QA02")
slot3, pw3 = provision_gateway("GW-QA0003", other, farm_o, sal_o, "HWM-QA03")
slot_susp, _ = provision_gateway("GW-QASUSP", demo, farm1, sal1, "HWM-QA04")
GatewaySlot.objects.filter(pk=slot_susp.pk).update(provisioning_status="SUSPENDED")
slot_pend, _ = provision_gateway("GW-QAPEND", demo, farm1, sal1, "HWM-QA05")
GatewaySlot.objects.filter(pk=slot_pend.pk).update(provisioning_status="PENDING_APPROVAL")

def map_pack(gw_hardware_id, saloon, pack, sensor_hw, slot_id):
    pos = SaloonPosition.objects.filter(saloon=saloon).first()
    ss, _ = SaloonSensorSlot.objects.get_or_create(slot_id=slot_id, defaults={"saloon": saloon, "org": saloon.org,
                                                                              "position": pos,
                                                                              "allowed_metric_keys": ["temp","humidity","lux","nh3","water_l","co","power_mains","power_gen"]})
    SaloonSensorSlot.objects.filter(pk=slot_id).update(saloon=saloon, org=saloon.org, is_active=True)
    HardwareRegistry.objects.update_or_create(hardware_id=sensor_hw, org=saloon.org, removed_at=None,
                                              defaults={"slot": ss})
    GatewaySensorMapping.objects.update_or_create(gateway_hardware_id=gw_hardware_id,
                                                  pack_id=pack, defaults={"slot": ss, "org": saloon.org})

for gw, sal in [(slot1, sal1), (slot2, sal2), (slot3, sal_o)]:
    gwhw = GatewayHardwareRegistry.objects.filter(slot=gw, removed_at=None).first().hardware_id
    for j, pack in enumerate(["P-A", "P-B", "P-C"]):
        map_pack(gwhw, sal, pack, f"SNS-{gw.slot_id[-4:]}{j}", f"SS-{gw.slot_id[-4:]}{j}")

for s, slug in [(farm1, f"farm-{farm1.id}"), (farm2, f"farm-{farm2.id}"), (sal1, f"device-{sal1.id}"),
                (sal2, f"device-{sal2.id}")]:
    LegacySlugMap.objects.update_or_create(slug=slug, defaults={"kind": "farm" if s in (farm1, farm2) else "device",
                                                                "org_id": s.org.org_id if hasattr(s,'org') else demo.org_id,
                                                                "farm_id": s.id if s in (farm1, farm2) else None,
                                                                "saloon_id": None if s in (farm1, farm2) else s.id})
LegacySlugMap.objects.update_or_create(slug=f"org-{str(demo.org_id)[:8]}", defaults={"kind":"org","org_id":demo.org_id})

manifest = {"org_id": str(demo.org_id), "farm_id": farm1.id,
            "saloons": [
                {"name": "Saloon-A", "farm_id": farm1.id, "slots": [{"hardware_id": "HWM-QA01", "slot_id": f"SS-{slot1.slot_id[-4:]}{j}", "pack": p} for j, p in enumerate(["P-A","P-B","P-C"])]},
                {"name": "Saloon-B", "farm_id": farm2.id, "slots": [{"hardware_id": "HWM-QA02", "slot_id": f"SS-{slot2.slot_id[-4:]}{j}", "pack": p} for j, p in enumerate(["P-A","P-B","P-C"])]},
            ]}
open("/tmp/qa_manifest.json", "w").write(json.dumps(manifest))

from apps.alerts.services.thresholds import recache_all_saloons
print("recached:", recache_all_saloons())
print("ORG1", demo.org_id, "| ORG2", other.org_id)
print("SALOONS", sal1.id, sal2.id, "| SALOON_OTHER", sal_o.id)
print("FARMS", farm1.id, farm2.id, "| FLOCK", FlockCycle.objects.filter(saloon=sal1).first().flock_id)
print("SLOTS", slot1.slot_id, slot2.slot_id, slot3.slot_id, slot_susp.slot_id, slot_pend.slot_id)
print("GW_PWS", json.dumps({slot1.slot_id: pw1, slot2.slot_id: pw2, slot3.slot_id: pw3}))
print("SSLOTS", json.dumps(manifest))
print("STAFF_MFA_SECRET", PlatformStaffMFA.objects.get(user=staff1).secret)
print("SEED-DONE")
