"""Seed the subscriber-tier broker credentials (M7-03/M7-06, v0.7).

PROD authn is the postgresql backend over ``mqtt_device_credentials`` JOIN
``gateway_slots`` (emqx.conf §authentication) — the subscriber tier must exist
in that chain or the N≥2 topology (AU-SUBSCRIBER-N2) cannot connect at all.
This command models each subscriber service account as a PROVISIONED slot:

    manage.py seed_subscriber_credentials \
        --client-id subscriber-01 --username subscriber-svc --password '...'

Idempotent (update_or_create on the slot/credential); ACL allow rows cover
exactly what the subscriber subscribes (the shared telemetry group + the
status topic) — deny-by-default comes from the broker's no_match=deny.

Platform org: the subscriber tier is PLATFORM infrastructure, not tenant
data — with no --org-id given, the command resolves/creates a dedicated
"Morghdari Platform" organization so tenant orgs are never touched.

Hash: bcrypt $2b$ via apps.mqtt_auth.services.hash_password — the ONLY format
EMQX 5.5's password_based:bcrypt verifies natively (C7 / AU-fix).

فارسی: ساخت اعتبارنامهٔ حساب‌های سرویسِ مشترک‌شونده برای زنجیرهٔ احرازِ EMQX.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.devices.models import GatewaySlot
from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
from apps.mqtt_auth.services import hash_password
from apps.tenants.models import Organization

PLATFORM_ORG_NAME = "Morghdari Platform"

# The subscriber's exact subscriptions (morghdari/subscriber/main.py §S1).
SHARED_TOPIC = "$share/telemetry_workers/+/farms/+/gateways/+/telemetry"
STATUS_TOPIC = "+/farms/+/gateways/+/status"


class Command(BaseCommand):
    help = "Seed one subscriber service account into the EMQX authn chain (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", required=True, help="subscriber-01 / subscriber-02")
        parser.add_argument("--username", required=True, help="MQTT username (SUBSCRIBER_USERNAME)")
        parser.add_argument("--password", required=True, help="MQTT password (hashed, never stored raw)")
        parser.add_argument("--org-id", default=None, help="platform org UUID (default: resolve/create 'Morghdari Platform')")

    def handle(self, *args, **options):
        client_id = options["client_id"].strip()
        username = options["username"].strip()
        password = options["password"]
        if not client_id or not username or not password:
            raise CommandError("client-id, username and password are all required")

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
        self.stdout.write(f"gateway_slots[{client_id}] {'created' if created else 'updated'} (PROVISIONED)")

        cred, created = MqttDeviceCredential.objects.update_or_create(
            device_id=client_id,
            defaults={
                "username": username,
                "password_hash": hash_password(password),
                "is_active": True,
                "pending_password_hash": None,
                "pending_since": None,
            },
        )
        self.stdout.write(f"mqtt_device_credentials[{username}] {'created' if created else 'updated'}")

        for topic in (SHARED_TOPIC, STATUS_TOPIC):
            _, created = MqttAclRule.objects.update_or_create(
                username=username,
                topic=topic,
                defaults={"permission": "allow", "action": "subscribe"},
            )
            self.stdout.write(f"acl[{username}] {topic}: {'created' if created else 'updated'}")

        self.stdout.write(self.style.SUCCESS(f"subscriber {client_id} seeded"))
