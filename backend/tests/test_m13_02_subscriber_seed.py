"""The subscriber-credential seeder must authorize every lane it holds.

2026-10-20: the subscriber's subscription set grew to five lanes (telemetry
share, status, tenant command responses, and the two AD-3 device lanes) while
this command still seeded ACL rules for two. A subscription without an allow
rule is denied by the broker's `no_match=deny` — the SUBACK handler now logs
CRITICAL and resubscribes forever, but nothing in the test suite noticed, so
the defect reached production documentation as "Phase 3 done".

`test_emqx_provision.test_the_subscribers_rules_match_what_it_subscribes` pins
the builder lists against each other. These tests cover what the COMMAND
actually writes, which is a separate failure mode: the list can be right and
the write wrong (skipped rows, wrong action, missing idempotency).
"""

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db

EXPECTED_TOPICS = {
    "+/farms/+/gateways/+/telemetry",
    "+/farms/+/gateways/+/status",
    "+/farms/+/gateways/+/commands/response",
    "devices/+/reported",
    "devices/+/command/response",
}


def _seed(username="subscriber-svc"):
    call_command(
        "seed_subscriber_credentials",
        client_id="subscriber-01", username=username, password="pw-not-stored",
    )


def test_the_command_writes_an_allow_rule_for_every_lane():
    _seed()
    from apps.mqtt_auth.models import MqttAclRule

    rows = MqttAclRule.objects.filter(username="subscriber-svc")
    assert {r.topic for r in rows} == EXPECTED_TOPICS
    # subscribe-only: a subscriber that could publish could forge telemetry
    assert all(r.action == "subscribe" and r.permission == "allow" for r in rows)


def test_the_credential_is_active_with_no_pending_rotation():
    _seed()
    from apps.mqtt_auth.models import MqttDeviceCredential

    cred = MqttDeviceCredential.objects.get(username="subscriber-svc")
    assert cred.is_active is True
    assert cred.pending_password_hash is None
    # bcrypt, never the raw password (C7 / AU-fix): EMQX 5.5 verifies only $2b$
    assert cred.password_hash.startswith("$2b$")
    assert "pw-not-stored" not in cred.password_hash


def test_the_slot_is_provisioned_in_a_dedicated_platform_org():
    """Platform infrastructure, not tenant data: seeding must not need — or
    invent — a tenant org."""
    _seed()
    from apps.devices.models import GatewaySlot
    from apps.tenants.models import Organization

    slot = GatewaySlot.objects.get(pk="subscriber-01")
    assert slot.provisioning_status == "PROVISIONED"
    assert slot.org.name == "Morghdari Platform"
    assert Organization.objects.count() == 1


def test_reseeding_is_idempotent_and_can_rotate_the_password():
    """prod_bootstrap.sh runs this on every boot; duplicate rows or a lost
    rotation would both be silent."""
    _seed()
    _seed()  # second run, same everything
    from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential

    assert MqttAclRule.objects.filter(username="subscriber-svc").count() == 5

    call_command(
        "seed_subscriber_credentials",
        client_id="subscriber-01", username="subscriber-rotated",
        password="new-pw-not-stored",
    )

    cred = MqttDeviceCredential.objects.get(username="subscriber-rotated")
    assert cred.is_active is True
    assert "new-pw-not-stored" not in cred.password_hash
    # the rules follow the NEW username, and the old ones are gone
    assert {r.topic for r in MqttAclRule.objects.filter(username="subscriber-rotated")} == EXPECTED_TOPICS
    assert MqttAclRule.objects.filter(username="subscriber-svc").count() == 0
