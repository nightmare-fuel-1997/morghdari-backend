"""Desired-state document builder (AD-3/AD-4, 2026-10-18).

WHY the document is complete rather than a delta: it is published **retained**
on `devices/{slot_id}/desired`, so it must be sufficient to rebuild a gateway
from a wiped flash. A delta would need a baseline the device may not have.

WHY it carries no secrets: the MQTT password lives in the device's flash and as
a bcrypt hash server-side (the old HTTP config payload also returned only the
hash). Nothing here may ever contain a plaintext credential — `test_m10_03`
asserts that by grepping the serialized payload.

WHY `topics` is included even though the firmware derives the same strings
(AD-7): the device composes the topic grammar itself and compares. A mismatch
means a mis-addressed document, and the device refuses it instead of publishing
telemetry into a topic nobody reads.
"""

from __future__ import annotations

import os

from django.utils import timezone

SCHEMA_VERSION = "1.0"
DEFAULT_REPORT_INTERVAL_SEC = 60
REPORT_INTERVAL_CONFIG_KEY = "report_interval_sec"


def _report_interval_sec() -> int:
    from apps.system_config.models import SystemConfiguration

    raw = (
        SystemConfiguration.objects.filter(key=REPORT_INTERVAL_CONFIG_KEY)
        .values_list("value", flat=True)
        .first()
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REPORT_INTERVAL_SEC


def gateway_hardware_id(slot) -> str:
    """The active registry serial for this slot, else the bound factory unit's.

    Both are legitimate: a slot created through the new flow has a DeviceUnit
    with a MAC; a legacy slot has a gateway_hardware_registry row and no unit.
    """
    from apps.devices.models import GatewayHardwareRegistry

    hw = (
        GatewayHardwareRegistry.objects.filter(slot_id=slot.slot_id, removed_at__isnull=True)
        .values_list("hardware_id", flat=True)
        .first()
    )
    if hw:
        return hw
    from apps.devices.models import DeviceUnit

    unit = DeviceUnit.objects.filter(slot_id=slot.slot_id).values_list("hardware_id", flat=True).first()
    return unit or ""


def build_sensor_list(hardware_id: str) -> list[dict]:
    """Authorized sensors for this gateway, deterministically ordered.

    AD-23: `pack_id` is the 8-hex LoRa node id — the join key, not metadata.
    `metrics` comes from `SaloonSensorSlot.allowed_metric_keys`, the existing
    column that answers "what information should the gateway expect from this
    sensor" and previously had no writer.
    """
    from apps.devices.models import GatewaySensorMapping

    rows = (
        GatewaySensorMapping.objects.filter(gateway_hardware_id=hardware_id)
        .select_related("slot", "slot__saloon", "slot__position")
        .order_by("slot__saloon_id", "slot__position__label", "pack_id")
    )
    sensors = []
    for row in rows:
        slot = row.slot
        saloon_name = slot.saloon.name if slot.saloon_id and slot.saloon else ""
        position = slot.position.label if slot.position_id and slot.position else ""
        label = " - ".join(part for part in (saloon_name, position) if part) or row.pack_id
        sensors.append(
            {
                "pack_id": row.pack_id,
                "slot_id": slot.slot_id,
                "saloon_id": slot.saloon_id,
                "saloon_name": saloon_name,
                "position": position,
                "label": label,
                "metrics": list(slot.allowed_metric_keys or []),
            }
        )
    return sensors


def build_desired_state(slot) -> dict:
    """The complete configuration document for one gateway.

    Reads `gateway_slots`, `gateway_hardware_registry` / `device_units`, and
    `gateway_sensor_mapping`. Never writes.
    """
    from apps.provisioning import topics

    hw_id = gateway_hardware_id(slot)
    farm_name = ""
    if slot.farm_id:
        from apps.farms.models import Farm

        farm_name = Farm.objects.filter(pk=slot.farm_id).values_list("name", flat=True).first() or ""

    return {
        "schema_version": SCHEMA_VERSION,
        "config_version": slot.desired_config_version,
        "slot_id": slot.slot_id,
        "org_id": str(slot.org_id),
        "farm_id": slot.farm_id,
        "farm_name": farm_name,
        "gateway_hardware_id": hw_id,
        "mqtt": {
            "host": os.environ.get("MQTT_PUBLIC_HOST", "mqtt.morghdari.com"),
            "port": int(os.environ.get("MQTT_PUBLIC_PORT", "8883")),
            "tls": True,
        },
        "sensors": build_sensor_list(hw_id),
        "report_interval_sec": _report_interval_sec(),
        "topics": {
            "telemetry": f"{slot.org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/telemetry",
            "status": f"{slot.org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/status",
            "commands": f"{slot.org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/commands",
            "commands_response": f"{slot.org_id}/farms/{slot.farm_id}/gateways/{slot.slot_id}/commands/response",
            "desired": topics.desired_topic(slot.slot_id),
            "reported": topics.reported_topic(slot.slot_id),
            "command": topics.command_topic(slot.slot_id),
        },
        "server_ts": timezone.now().isoformat().replace("+00:00", "Z"),
    }
