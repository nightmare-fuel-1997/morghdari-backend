"""Simulate a realistic 60-second Auto-RoC live stream (`STARTED -> ACTIVE -> SETTLING -> ENDED`).

Intended for Frontend & Flutter developers (and staging QA) to exercise
WebSocket `telemetry.live`, Late-Joiner instant snapshots, and `AccountActivityLog`
(`SENSOR_LIVE_TEST_COMPLETED`) on demand without flashing physical firmware.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.devices.models import (
    GatewayHardwareRegistry,
    GatewaySaloonZone,
    GatewaySensorMapping,
    GatewaySlot,
    SaloonSensorSlot,
)
from apps.farms.models import Saloon
from apps.telemetry import ws
from apps.tenants.context import admin_alias, tenant_context

_BASELINE = {
    "temp": 28.0,
    "humidity": 60.0,
    "nh3": 12.0,
    "lux": 200.0,
}

# Realistic 7-step trajectory (fractions of peak_delta relative to baseline)
_TRAJECTORY = [
    ("STARTED", 0.40),
    ("ACTIVE", 0.85),
    ("ACTIVE", 1.00),
    ("ACTIVE", 0.25),
    ("SETTLING", 0.20),
    ("SETTLING", 0.15),
    ("ENDED", 0.12),
]


class Command(BaseCommand):
    help = "Simulate a 7-frame Auto-RoC live telemetry session (STARTED -> ENDED) for a saloon."

    def add_arguments(self, parser):
        parser.add_argument("--saloon-id", type=int, required=True, help="Target saloon PK")
        parser.add_argument("--slot-id", type=str, default=None, help="Optional SaloonSensorSlot id")
        parser.add_argument(
            "--metric",
            type=str,
            default="temp",
            choices=sorted(_BASELINE.keys()),
            help="Metric triggering RoC (default: temp)",
        )
        parser.add_argument(
            "--peak-delta",
            type=float,
            default=3.2,
            help="Peak deviation from baseline (default: 3.2)",
        )
        parser.add_argument(
            "--step-sec",
            type=float,
            default=10.0,
            help="Seconds to sleep between frames (default: 10.0; use 0 for instant test runs)",
        )
        parser.add_argument(
            "--mode",
            type=str,
            default="pipeline",
            choices=("pipeline", "mqtt"),
            help="Dispatch via subscriber fast-path ('pipeline') or live EMQX ('mqtt')",
        )

    def handle(self, *args, **options):
        saloon_id = options["saloon_id"]
        slot_id = options["slot_id"]
        metric = options["metric"]
        peak_delta = float(options["peak_delta"])
        step_sec = max(0.0, float(options["step_sec"]))
        mode = options["mode"]

        alias = admin_alias()
        saloon = Saloon.objects.using(alias).filter(pk=saloon_id).first()
        if saloon is None:
            raise CommandError(f"Saloon {saloon_id} not found")

        org_id = str(saloon.org_id)
        farm_id = saloon.farm_id

        with transaction.atomic(), tenant_context(org_id):
            if slot_id:
                sensor_slot = SaloonSensorSlot.objects.filter(
                    pk=slot_id, org_id=org_id
                ).first()
                if sensor_slot is None:
                    raise CommandError(f"Sensor slot {slot_id!r} not found in saloon org")
            else:
                sensor_slot = (
                    SaloonSensorSlot.objects.filter(saloon_id=saloon_id, is_active=True)
                    .order_by("slot_id")
                    .first()
                )
            resolved_slot_id = sensor_slot.slot_id if sensor_slot else (slot_id or f"SIM-SLOT-{saloon_id}")

            mapping = (
                GatewaySensorMapping.objects.filter(slot_id=resolved_slot_id, org_id=org_id)
                .order_by("id")
                .first()
            )
            pack_id = mapping.pack_id if mapping else "SIM-PACK-A"
            gw_hw_id = mapping.gateway_hardware_id if mapping else f"SIM-HW-{saloon_id}"

            gw_slot_id = None
            if mapping:
                reg = GatewayHardwareRegistry.objects.filter(
                    hardware_id=mapping.gateway_hardware_id, removed_at__isnull=True
                ).first()
                if reg:
                    gw_slot_id = reg.slot_id
            if not gw_slot_id:
                zone = GatewaySaloonZone.objects.filter(saloon_id=saloon_id).first()
                if zone:
                    gw_slot_id = zone.gateway_slot_id
            if not gw_slot_id:
                gw = GatewaySlot.objects.filter(org_id=org_id, farm_id=farm_id).first()
                gw_slot_id = gw.slot_id if gw else f"SIM-GW-{saloon_id}"

        topic = f"{org_id}/farms/{farm_id}/gateways/{gw_slot_id}/telemetry/live"
        session_id = f"{gw_slot_id}-{pack_id}-{int(time.time())}"
        base_val = _BASELINE[metric]
        prev_val = base_val
        now_dt = datetime.now(timezone.utc)

        if mode == "pipeline":
            from morghdari.subscriber.main import handle_live_message

            redis_client = ws.ws_redis()
        else:
            from apps.mqtt_auth import publisher

            redis_client = None

        self.stdout.write(
            f"Simulating Auto-RoC session {session_id} -> saloon={saloon_id}, "
            f"slot={resolved_slot_id}, gw={gw_slot_id}, metric={metric}, mode={mode}"
        )

        for idx, (state, frac) in enumerate(_TRAJECTORY):
            cur_val = round(base_val + peak_delta * frac, 2)
            roc_delta = round(cur_val - prev_val, 2)
            prev_val = cur_val

            frame_metrics = dict(_BASELINE)
            frame_metrics[metric] = cur_val
            device_ts = (now_dt + timedelta(seconds=idx * 10)).strftime("%Y-%m-%dT%H:%M:%SZ")

            payload_dict = {
                "gateway_hardware_id": gw_hw_id,
                "gateway_slot_id": gw_slot_id,
                "schema_version": "2.0",
                "device_ts": device_ts,
                "stream_state": state,
                "session_id": session_id,
                "readings": [
                    {
                        "pack_id": pack_id,
                        "trigger_metric": metric,
                        "roc_delta": roc_delta,
                        "roc_window_sec": 10,
                        "metrics": frame_metrics,
                        "sensor_errors": {},
                    }
                ],
            }

            if mode == "pipeline":
                handle_live_message(
                    redis_client,
                    topic,
                    json.dumps(payload_dict).encode("utf-8"),
                    db_fetch=lambda _o, _h, _p, sid=resolved_slot_id: sid,
                    meta_fetch=lambda _o, _f, _g, fid=farm_id: {
                        "farm_id": fid,
                        "provisioning_status": "PROVISIONED",
                    },
                    saloon_fetch=lambda _o, _s, sid=saloon_id: sid,
                )
            else:
                ok = publisher.publish(topic, payload_dict, qos=0, retain=False)
                if not ok:
                    raise CommandError(f"MQTT publish failed on frame {idx} ({state})")

            self.stdout.write(
                f"  [{idx + 1}/{len(_TRAJECTORY)}] {state:8s} | {metric}={cur_val} (delta={roc_delta:+})"
            )
            if idx < len(_TRAJECTORY) - 1 and step_sec > 0:
                time.sleep(step_sec)

        self.stdout.write(self.style.SUCCESS(f"Session {session_id} finished."))
