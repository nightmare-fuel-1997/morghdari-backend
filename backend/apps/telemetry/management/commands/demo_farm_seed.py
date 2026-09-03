"""demo_farm_seed — idempotent LIVE-demo topology + alert rules (demo branches).

What it builds (re-runnable, no duplicates):
  1. "Demo Farm" org + demo-owner (owner role) — the same ritual the demo-ui
     README shelled by hand, now codified.
  2. THREE named farms (Baharestan / Morvarid / Nahavand), each with 2 saloons
     and 2 sensor packs (P-A / P-B) per saloon, mapped to slots — the exact
     shape scripts/simulate_farm.py publishes against (topic segment = slot id).
     Legacy single-farm saloons from older seeds ("Demo Saloon 0X" under a
     "Demo Farm" farm) are absorbed by RENAME+REASSIGN, never deleted (all FKs
     are RESTRICT) — so re-running on an already-seeded DB converges.
  3. Alert readiness: a global default LifecycleTemplate with day-1..45
     threshold rules (temp/humidity/nh3), one OPEN FlockCycle per saloon, and
     the Redis threshold hashes recached so inline evaluation actually fires.
  4. Demo pacing: breach_duration_sec -> 30 so a Tier-1 alert becomes VISIBLE
     during a showroom instead of after 15 minutes. `--restore` puts the
     seeded 900s back.

RLS note: this command must run on a RLS-bypassing connection (dev/CI both
are — dev's DATABASE_URL user is the cluster superuser). Writes are still
wrapped in tenant_context + atomic so the same code is correct under FORCE.

Persian: ساختِ توپولوژی مزرعه‌ی نمایشی با چندباراجرا-امن بودن؛ نسخهٔ کدگذاری‌شدهٔ
ریچوالِ README دمو.
"""

import json
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import OrgMember
from apps.alerts.models import LifecycleTemplate, LifecycleThresholdRule
from apps.alerts.services import thresholds as thresholds_svc
from apps.devices.models import (
    GatewaySensorMapping,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.system_config.models import SystemConfiguration
from apps.telemetry.models import FlockCycle
from apps.tenants.context import tenant_context
from apps.tenants.models import Organization

ORG_NAME = "Demo Farm"
OWNER_USERNAME = "demo-owner"
OWNER_PASSWORD = "demo-pass-123"
PACKS = ("P-A", "P-B")
# Named farms for the boss board — (farm name, saloon names). Realistic enough
# to read as a product, deterministic enough to stay idempotent.
FARMS = [
    ("Baharestan", ["Saloon A", "Saloon B"]),
    ("Morvarid", ["Saloon A", "Saloon B"]),
    ("Nahavand", ["Saloon A", "Saloon B"]),
]
BREACH_DURATION_KEY = "breach_duration_sec"
BREACH_TUNED_SEC = "30"      # showroom pace: Tier 1 fires after 30 s of breach
BREACH_SEED_SEC = "900"      # the seeded production value (restore target)

# Escalation demo pace: Tier 2 after 1 min, Tier 3 after 2 min (production:
# 15/30). escalate_all reads these from SystemConfiguration every sweep, so
# this is config-is-data — no code change needed for the showroom timeline.
TIER2_KEY = "escalation_tier2_min"
TIER3_KEY = "escalation_tier3_min"
TIER2_TUNED = "1"
TIER3_TUNED = "2"
TIER2_SEED = "15"
TIER3_SEED = "30"

# (metric, min, max) — day 1..45 band on the global default template.
RULE_BANDS = [
    ("temp", 18.0, 30.0),
    ("humidity", 40.0, 75.0),
    ("nh3", None, 25.0),
    # D23 derived THI heat-stress rule: the engine derives THI from co-present
    # temp+humidity and evaluates it against thi_max through the normal
    # breach/duration machinery. Demo simulator raises temp+humidity together
    # in scripted windows so this visibly fires.
    ("thi", None, 79.0),
]


class Command(BaseCommand):
    help = "Idempotently seed the LIVE-demo farms (org, farms, saloons, slots, thresholds, pacing)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest-out",
            dest="manifest_out",
            default=None,
            help="Write the topology manifest JSON to this path (consumed by scripts/simulate_farm.py)",
        )
        parser.add_argument(
            "--restore",
            action="store_true",
            help="Put breach_duration_sec back to its seeded value (900) instead of demo pace (30)",
        )

    def handle(self, *args, **options):
        restore = options["restore"]
        org = Organization.objects.filter(name=ORG_NAME).first()
        created_org = org is None
        if created_org:
            org = Organization.objects.create(name=ORG_NAME, subscription_tier="demo")

        import django.contrib.auth as auth

        UserModel = auth.get_user_model()
        owner = UserModel.objects.filter(username=OWNER_USERNAME).first()
        if owner is None:
            owner = UserModel.objects.create_user(username=OWNER_USERNAME, password=OWNER_PASSWORD)
            self.stdout.write(f"owner user '{OWNER_USERNAME}' created (password: {OWNER_PASSWORD})")
        member, member_created = OrgMember.objects.get_or_create(
            org=org, user=owner,
            defaults={"role": "owner", "is_active": True},
        )
        if not member_created and (member.role != "owner" or not member.is_active):
            member.role = "owner"
            member.is_active = True
            member.save(update_fields=["role", "is_active"])

        with tenant_context(org.org_id), transaction.atomic():
            province = Province.objects.filter(name="Demo Province").first() or Province.objects.create(name="Demo Province")
            city = City.objects.filter(name="Demo City").first() or City.objects.create(province=province, name="Demo City")

            # Legacy saloons from an older single-farm seed — absorbed into the
            # new named farms by rename+reassign (UPDATEs only; every FK here
            # is RESTRICT, so deletion is never safe).
            legacy = list(Saloon.objects.filter(org=org).order_by("id"))

            farms = []
            farm_saloon_pairs = []  # (farm, saloon) in seed order, for the manifest
            claimed = set()
            for farm_name, saloon_names in FARMS:
                farm, _ = Farm.objects.get_or_create(
                    org=org, name=farm_name,
                    defaults={"province": province, "city": city},
                )
                farms.append(farm)
                for saloon_name in saloon_names:
                    saloon = Saloon.objects.filter(org=org, farm=farm, name=saloon_name).first()
                    if saloon is None and legacy:
                        saloon = legacy.pop(0)
                        saloon.farm = farm
                        saloon.name = saloon_name
                        saloon.save(update_fields=["farm", "name"])
                        self.stdout.write(
                            f"absorbed legacy saloon {saloon.pk} -> {farm_name} / {saloon_name}"
                        )
                    if saloon is None:
                        saloon = Saloon.objects.create(org=org, farm=farm, name=saloon_name)
                    claimed.add(saloon.pk)
                    farm_saloon_pairs.append((farm, saloon))

            # Anything still unclaimed (a row that never matched a named pair —
            # shouldn't happen on a clean re-run) is kept but renamed so it
            # can't be mistaken for the current topology. Only rows OUTSIDE
            # `claimed` are touched: a re-run must never clobber the names of
            # saloons already absorbed into a named farm.
            for i, saloon in enumerate((s for s in legacy if s.pk not in claimed), start=1):
                saloon.name = f"Archive Saloon {i}"
                saloon.save(update_fields=["name"])
                self.stdout.write(self.style.WARNING(
                    f"kept unclaimed saloon {saloon.pk} as '{saloon.name}'"
                ))

            template = LifecycleTemplate.objects.filter(org__isnull=True, is_default=True).first()
            if template is None:
                template = LifecycleTemplate.objects.create(
                    org=None, breed_type=None, name="Demo Global Template", is_default=True,
                )
            for metric, mn, mx in RULE_BANDS:
                LifecycleThresholdRule.objects.get_or_create(
                    template=template, day_from=1, day_to=45, metric=metric,
                    defaults={"min_value": mn, "max_value": mx},
                )

            # Ordinals are GLOBAL across the org's saloons (id order) so slot /
            # hardware ids stay unique and deterministic across re-runs — a
            # legacy saloon absorbed at ordinal N keeps its original slot ids.
            ordinal_by_pk = {
                s.pk: i for i, s in enumerate(
                    Saloon.objects.filter(org=org).order_by("id"), start=1
                )
            }

            saloons = []
            for farm, saloon in farm_saloon_pairs:
                open_flock = (
                    FlockCycle.objects.filter(org=org, saloon=saloon, end_date__isnull=True)
                    .order_by("-start_date").first()
                )
                if open_flock is None:
                    FlockCycle.objects.create(
                        org=org,
                        saloon=saloon,
                        breed_type="ross",
                        start_date=datetime.now(timezone.utc) - timedelta(days=20),
                        placement_count=10000,
                        day_old_weight_g=720,
                    )

                slots = []
                ordinal = ordinal_by_pk[saloon.pk]
                for pack_index, pack in enumerate(PACKS, start=1):
                    position = (
                        SaloonPosition.objects.filter(saloon=saloon, org=org, label=f"position-{pack_index}").first()
                        or SaloonPosition.objects.create(saloon=saloon, org=org, label=f"position-{pack_index}")
                    )
                    slot_id = f"S{ordinal:02d}-PACK-{pack[-1]}"
                    slot = SaloonSensorSlot.objects.filter(slot_id=slot_id).first()
                    if slot is None:
                        slot = SaloonSensorSlot.objects.create(
                            slot_id=slot_id,
                            saloon=saloon,
                            org=org,
                            position=position,
                            allowed_metric_keys=["temp", "humidity", "nh3"],
                        )
                    elif slot.saloon_id != saloon.pk or slot.org_id != org.org_id or slot.position_id != position.pk:
                        slot.saloon = saloon
                        slot.org = org
                        slot.position = position
                        slot.save(update_fields=["saloon", "org", "position"])
                    hardware_id = f"FARM-{ordinal:02d}-GW-A-HW"
                    mapping = GatewaySensorMapping.objects.filter(
                        gateway_hardware_id=hardware_id, pack_id=pack,
                    ).first()
                    if mapping is None:
                        GatewaySensorMapping.objects.create(
                            gateway_hardware_id=hardware_id, pack_id=pack, slot=slot, org=org,
                        )
                    elif mapping.slot_id != slot.pk or mapping.org_id != org.org_id:
                        mapping.slot = slot
                        mapping.org = org
                        mapping.save(update_fields=["slot", "org"])
                    slots.append({"pack": pack, "slot_id": slot_id, "hardware_id": hardware_id})

                threshold_state = thresholds_svc.recache_saloon(saloon.pk)
                saloons.append({
                    "id": saloon.pk,
                    "name": saloon.name,
                    "farm_id": farm.pk,
                    "farm_name": farm.name,
                    "slots": slots,
                    "threshold_state": threshold_state,
                })

            target = BREACH_SEED_SEC if restore else BREACH_TUNED_SEC
            pacing, _ = SystemConfiguration.objects.get_or_create(
                key=BREACH_DURATION_KEY, defaults={"value": BREACH_SEED_SEC},
            )
            if pacing.value != target:
                pacing.value = target
                pacing.save(update_fields=["value"])
            self.stdout.write(
                self.style.WARNING(
                    f"{BREACH_DURATION_KEY} = {target} "
                    + ("(demo pace — Tier 1 after 30 s of breach)" if target == BREACH_TUNED_SEC else "(restored seed)")
                )
            )

            # Escalation demo pace (Tier 2 @ 1 min, Tier 3 @ 2 min; seed 15/30).
            for key, tuned, seed, label in (
                (TIER2_KEY, TIER2_TUNED, TIER2_SEED, "Tier 2"),
                (TIER3_KEY, TIER3_TUNED, TIER3_SEED, "Tier 3"),
            ):
                target_val = seed if restore else tuned
                row, _ = SystemConfiguration.objects.get_or_create(
                    key=key, defaults={"value": seed},
                )
                if row.value != target_val:
                    row.value = target_val
                    row.save(update_fields=["value"])
                self.stdout.write(
                    self.style.WARNING(
                        f"{key} = {target_val} "
                        + (f"(demo pace — {label} after {target_val} min)" if target_val == tuned else "(restored seed)")
                    )
                )

        manifest = {
            "org_id": str(org.org_id),
            "org_name": org.name,
            "farm_id": farms[0].pk if farms else None,
            "farm_name": farms[0].name if farms else None,
            "farms": [
                {
                    "id": farm.pk,
                    "name": farm.name,
                    "saloons": [
                        {"id": s.pk, "name": s.name}
                        for f2, s in farm_saloon_pairs if f2.pk == farm.pk
                    ],
                }
                for farm in farms
            ],
            "saloons": saloons,
            "breach_duration_sec": target,
        }
        if options["manifest_out"]:
            with open(options["manifest_out"], "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
            self.stdout.write(f"manifest written to {options['manifest_out']}")

        self.stdout.write("topology manifest:")
        self.stdout.write(json.dumps(manifest, indent=2, ensure_ascii=False))
        self.stdout.write(self.style.SUCCESS(
            "demo farms ready — feed them with:  python scripts/simulate_farm.py --manifest <file>"
        ))
