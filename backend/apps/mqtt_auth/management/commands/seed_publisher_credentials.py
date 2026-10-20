"""Seed the backend MQTT publisher principal (v0.5 E1, Phase 0 2026-10-18).

WHY this command exists: `django-backend` has been documented as the backend's
broker identity since the MQTT Topic Map was written (§3.3), but it was never
seeded — the only service accounts in the authn chain were the two subscribers.
That is why every downlink topic (T3–T9) and `add_dev` were contract-only: no
credential could publish to them.

Same shape as `seed_subscriber_credentials`: the principal is modelled as a
PROVISIONED slot in the dedicated platform organization, because the EMQX authn
query resolves credentials through `gateway_slots` and must not touch a tenant.

ACL: publish-only, on exactly the downlink topics the backend uses. It must NOT
be able to subscribe to telemetry — a publisher that can read every tenant's
data would be a far larger blast radius than the accepted risk already recorded
(MQTT Topic Map §3.3: this credential can publish to any org's topics, which is
required for fan-out).

Usage:
    manage.py seed_publisher_credentials --username backend-pub --password '...'
"""

from django.core.management.base import BaseCommand, CommandError

from apps.devices.models import GatewaySlot
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.mqtt_auth.services import hash_password
from apps.tenants.models import Organization

PLATFORM_ORG_NAME = "Morghdari Platform"

# Category A + device lane (AD-19): the two device-rooted topics this backend
# publishes to. `+` is deliberate — one publisher serves the whole fleet.
DEVICE_TOPICS = [
    "devices/+/desired",
    "devices/+/command",
]

# Category B + legacy downlink (MQTT Topic Map §3.3, verbatim).
TENANT_TOPICS = [
    "+/farms/+/gateways/+/commands",
    "+/farms/+/gateways/+/clock/sync",
    "+/farms/+/gateways/+/ota",
    "+/farms/+/gateways/+/diagnostics/request",
]


class Command(BaseCommand):
    help = "Seed the backend MQTT publisher principal (idempotent, publish-only)."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="MQTT username (PUBLISHER_USERNAME)")
        parser.add_argument("--password", required=True, help="MQTT password (hashed, never stored raw)")
        parser.add_argument(
            "--client-id",
            default="django-backend",
            help="Slot id used for the authn join (default: django-backend)",
        )
        parser.add_argument("--org-id", default=None, help="Platform org UUID (default: resolve/create)")

    def handle(self, *args, **options):
        username = options["username"].strip()
        password = options["password"]
        client_id = options["client_id"].strip()
        if not username or not password:
            raise CommandError("username and password are required")

        if options["org_id"]:
            org = Organization.objects.filter(org_id=options["org_id"]).first()
            if org is None:
                raise CommandError(f"org {options['org_id']} not found")
        else:
            org = Organization.objects.filter(name=PLATFORM_ORG_NAME).first()
            if org is None:
                org = Organization.objects.create(name=PLATFORM_ORG_NAME)
                self.stdout.write(f"created platform org {org.org_id}")

        slot, created = GatewaySlot.objects.update_or_create(
            slot_id=client_id,
            defaults={"org": org, "provisioning_status": "PROVISIONED"},
        )
        self.stdout.write(
            f"gateway_slots[{client_id}] {'created' if created else 'updated'} (PROVISIONED)"
        )

        _, created = MqttDeviceCredential.objects.update_or_create(
            device_id=client_id,
            defaults={
                "username": username,
                "password_hash": hash_password(password),
                "is_active": True,
                "pending_password_hash": None,
                "pending_since": None,
            },
        )
        self.stdout.write(
            f"mqtt_device_credentials[{username}] {'created' if created else 'updated'}"
        )

        for topic in DEVICE_TOPICS + TENANT_TOPICS:
            _, created = MqttAclRule.objects.update_or_create(
                username=username,
                topic=topic,
                defaults={"permission": "allow", "action": "publish"},
            )
            self.stdout.write(f"acl[{username}] publish {topic}: {'created' if created else 'updated'}")

        self.stdout.write(
            self.style.SUCCESS(
                f"publisher {username} seeded — publish-only on {len(DEVICE_TOPICS + TENANT_TOPICS)} topics"
            )
        )
