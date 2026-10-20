"""The production broker profile — the last unproven link (2026-10-20).

Every other live test in this repo runs against the CI broker, which
authenticates from EMQX's own built-in database. **Production does not do that.**
`emqx/emqx.conf` authenticates every client with a SQL query against Postgres:

    SELECT cred.password_hash FROM mqtt_device_credentials cred
    LEFT JOIN gateway_slots gs ON gs.slot_id = cred.device_id
    LEFT JOIN device_units du ON du.unit_id = cred.device_id
    WHERE cred.username = ${username} AND cred.is_active = true
      AND ( gs.provisioning_status IN (...)          -- bound device
            OR (gs.slot_id IS NULL AND du.state IN ('REGISTERED','BOUND')) )

That LEFT JOIN is the whole of AD-1: a unit registered at the factory must be
able to connect while belonging to nobody. It had been checked in isolation and
the config had booted, but no live client had ever authenticated through it.
This module closes that gap.

Two properties are being proven, and they are different:
  1. AUTHENTICATION — the query returns a row for an unbound unit, so the broker
     issues a CONNACK 0. If the LEFT JOIN were wrong, this is a rejection.
  2. AUTHORIZATION — the ACL is read from `mqtt_acl_rules` by the same Postgres
     backend, so an unbound unit still cannot touch tenant topics.

Deliberately does NOT use pytest-django's test database: the broker reads the
real schema, so the rows must land in the same database the broker queries. The
`django_db_blocker` fixture gives ORM access without creating `test_<name>`.
"""

import os
import sys
import time
from pathlib import Path

import pytest

# scripts/ holds the INDEPENDENT device implementation. It is mounted read-only
# into the container at /scripts and added to the path here rather than imported
# at module scope, so a plain `pytest backend/tests` run never needs it.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

BROKER_HOST = os.environ.get("MQTT_PROD_BROKER_HOST", "").strip()
BROKER_PORT = int(os.environ.get("MQTT_PROD_BROKER_PORT", "8883"))
BROKER_TLS = os.environ.get("MQTT_PROD_BROKER_TLS", "1") == "1"

pytestmark = pytest.mark.skipif(
    not BROKER_HOST,
    reason="production broker profile not available (set MQTT_PROD_BROKER_HOST)",
)


def _device(unit_id, password, *, state="REGISTERED", claim_code="claim-prod-1"):
    """A factory unit exactly as registration leaves it: credential + 3 ACL rows."""
    from apps.devices.models import DeviceUnit
    from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
    from apps.mqtt_auth.services import bootstrap_acl_rule_items, hash_password

    DeviceUnit.objects.update_or_create(
        unit_id=unit_id,
        defaults={
            "hardware_id": f"HW-{unit_id}",
            "claim_code_hash": hash_password(claim_code),
            "state": state,
        },
    )
    MqttDeviceCredential.objects.update_or_create(
        device_id=unit_id,
        defaults={
            "username": unit_id,
            "password_hash": hash_password(password),
            "is_active": True,
        },
    )
    for item in bootstrap_acl_rule_items(unit_id):
        MqttAclRule.objects.update_or_create(
            username=unit_id, topic=item["topic"],
            defaults={"permission": item["permission"], "action": item["action"]},
        )


def _drop(unit_id):
    from apps.devices.models import DeviceUnit
    from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential

    MqttAclRule.objects.filter(username=unit_id).delete()
    MqttDeviceCredential.objects.filter(username=unit_id).delete()
    DeviceUnit.objects.filter(pk=unit_id).delete()


def _connect(unit_id, password, *, protocol=4):
    """Return the CONNACK code (0 = accepted). 5 = not authorised."""
    from firmware_sim import SimulatedGateway

    device = SimulatedGateway(
        unit_id, password, host=BROKER_HOST, port=BROKER_PORT, tls=BROKER_TLS,
        protocol=protocol,
    )
    try:
        # connect() RETURNS the CONNACK code (0 = accepted, -1 = no answer).
        return device.connect()
    finally:
        device.disconnect()


def test_unbound_factory_unit_authenticates_through_the_postgres_query(
    django_db_blocker,
):
    """AD-1's whole point: a unit with no org and no slot can connect.

    This is the assertion that could not be made anywhere else in the suite.
    The CI broker would pass it for the wrong reason (built-in DB users), so a
    failure here means the production query is wrong, not that the test rig is.
    """
    unit_id = "GW-PROD-UNBOUND"
    with django_db_blocker.unblock():
        _drop(unit_id)
        _device(unit_id, "prod-pass-1")
    try:
        assert _connect(unit_id, "prod-pass-1") == 0, (
            "an unbound factory unit was REFUSED by the production authn query — "
            "the device_units LEFT JOIN branch is not returning a row"
        )
    finally:
        with django_db_blocker.unblock():
            _drop(unit_id)


def test_a_bound_unit_still_authenticates(django_db_blocker):
    """The original slot-status gate must not have been broken by the LEFT JOIN.

    `gs.slot_id IS NULL AND du.state IN (...)` is an OR branch; if it had been
    written as an AND, every bound gateway would be locked out in production
    while the unbound case still passed.
    """
    from apps.devices.models import DeviceUnit, GatewaySlot
    from apps.farms.models import Farm
    from apps.geography.models import City, Province
    from apps.tenants.models import Organization

    unit_id = "GW-PROD-BOUND"
    with django_db_blocker.unblock():
        _drop(unit_id)
        province = Province.objects.create(name="P-PROD")
        city = City.objects.create(province=province, name="C-PROD")
        org = Organization.objects.create(name="prod-authn-org")
        farm = Farm.objects.create(org=org, name="prod-farm", province=province, city=city)
        slot = GatewaySlot.objects.create(
            slot_id=unit_id, org=org, farm=farm, provisioning_status="PROVISIONED",
            desired_config_version=1, applied_config_version=1,
        )
        _device(unit_id, "prod-pass-2", state="BOUND")
        DeviceUnit.objects.filter(pk=unit_id).update(slot=slot)
    try:
        assert _connect(unit_id, "prod-pass-2") == 0, (
            "a PROVISIONED bound gateway was refused — the OR branch that keeps "
            "bound devices working is broken"
        )
    finally:
        with django_db_blocker.unblock():
            _drop(unit_id)
            GatewaySlot.objects.filter(pk=unit_id).delete()
            Farm.objects.filter(name="prod-farm").delete()
            Organization.objects.filter(name="prod-authn-org").delete()
            City.objects.filter(name="C-PROD").delete()
            Province.objects.filter(name="P-PROD").delete()


def test_a_retired_unit_is_refused(django_db_blocker):
    """`du.state` is part of the gate: RETIRED must not be in the allow list."""
    unit_id = "GW-PROD-RETIRED"
    with django_db_blocker.unblock():
        _drop(unit_id)
        _device(unit_id, "prod-pass-3", state="RETIRED")
    try:
        assert _connect(unit_id, "prod-pass-3") != 0, (
            "a RETIRED unit authenticated against the production broker"
        )
    finally:
        with django_db_blocker.unblock():
            _drop(unit_id)


def test_an_unknown_username_is_refused(django_db_blocker):
    """The query has no OR on the credential side, so an unknown user is empty."""
    assert _connect("GW-PROD-NOBODY", "anything") != 0


def test_a_wrong_password_is_refused(django_db_blocker):
    unit_id = "GW-PROD-WRONGPW"
    with django_db_blocker.unblock():
        _drop(unit_id)
        _device(unit_id, "prod-pass-4")
    try:
        assert _connect(unit_id, "not-the-password") != 0, (
            "the broker accepted a wrong password — bcrypt verification is not "
            "running against the Postgres hash"
        )
    finally:
        with django_db_blocker.unblock():
            _drop(unit_id)


def test_an_inactive_credential_is_refused(django_db_blocker):
    """`cred.is_active = true` is a real condition, not decoration."""
    from apps.mqtt_auth.models import MqttDeviceCredential

    unit_id = "GW-PROD-INACTIVE"
    with django_db_blocker.unblock():
        _drop(unit_id)
        _device(unit_id, "prod-pass-5")
        MqttDeviceCredential.objects.filter(username=unit_id).update(is_active=False)
    try:
        assert _connect(unit_id, "prod-pass-5") != 0
    finally:
        with django_db_blocker.unblock():
            _drop(unit_id)


def test_an_unbound_unit_still_cannot_reach_tenant_topics(django_db_blocker):
    """AUTHORIZATION, not authentication: the ACL comes from Postgres too.

    The first version of this test was WRONG and would have passed for the wrong
    reason: it used the unbound unit itself as the witness, and that unit has no
    subscribe rule for tenant topics — so a denied publish and a witness that
    never subscribed look identical from outside. The witness here is a separate
    credential granted `#` subscribe, so if the unbound unit's publish is
    permitted, the message provably arrives.
    """
    import ssl
    import time as _t

    import paho.mqtt.client as mqtt

    unit_id = "GW-PROD-ACL"
    witness_id = "GW-PROD-WITNESS"

    def _client(cid, username, password):
        c = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=cid, clean_session=True, protocol=mqtt.MQTTv311,
        )
        c.username_pw_set(username, password)
        if BROKER_TLS:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c.tls_set_context(ctx)
        return c

    with django_db_blocker.unblock():
        _drop(unit_id)
        _drop(witness_id)
        _device(unit_id, "prod-pass-6")
        # A witness that is ALLOWED to subscribe to everything.
        #
        # It must be a full device (unit + credential), not a bare credential:
        # the production authn query returns a row only for a unit that is
        # either bound to a slot or present in device_units as REGISTERED/BOUND.
        # A credential-only witness cannot authenticate at all — which is how
        # this test failed the first time, and it failed with a message that
        # said the witness could not observe, not that it could not connect.
        _device(witness_id, "prod-pass-w")
        from apps.mqtt_auth.models import MqttAclRule

        MqttAclRule.objects.update_or_create(
            username=witness_id, topic="#",
            defaults={"permission": "allow", "action": "subscribe"},
        )

    publisher = _client("acl-pub", unit_id, "prod-pass-6")
    witness = _client("acl-witness", witness_id, "prod-pass-w")
    seen = []
    witness.on_message = lambda _c, _u, m: seen.append((m.topic, m.payload))
    codes = []
    publisher.on_connect = lambda _c, _u, _f, rc, _p=None: codes.append(str(rc))

    try:
        publisher.connect(BROKER_HOST, BROKER_PORT, keepalive=15)
        publisher.loop_start()
        deadline = _t.time() + 5
        while _t.time() < deadline and not codes:
            _t.sleep(0.05)
        assert codes and codes[0] in ("Success", "0"), f"unit refused: {codes}"

        witness.connect(BROKER_HOST, BROKER_PORT, keepalive=15)
        witness.loop_start()
        _t.sleep(0.5)
        witness.subscribe("#", qos=1)
        _t.sleep(0.5)

        # the unit's own lane is allowed and must arrive
        publisher.publish(f"devices/{unit_id}/reported", "ok", qos=1)
        _t.sleep(0.8)
        assert any(t == f"devices/{unit_id}/reported" for t, _ in seen), (
            "the unit's own report lane did not reach the witness — the witness "
            "is not actually able to observe, so the negative below is meaningless"
        )

        # a tenant topic is not in its ACL
        seen.clear()
        publisher.publish("anyorg/farms/1/gateways/x/telemetry", "leak", qos=1)
        _t.sleep(1.0)
        assert seen == [], f"an unbound unit reached a tenant topic: {seen}"
    finally:
        for c in (publisher, witness):
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        with django_db_blocker.unblock():
            _drop(unit_id)
            _drop(witness_id)
