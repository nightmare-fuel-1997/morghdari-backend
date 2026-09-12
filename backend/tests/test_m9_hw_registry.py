"""M9 (H0-H2) acceptance — platform hardware registration (Hardware Team write path).

The Hardware Team actor had zero system presence: gateway/sensor/actuator
serials entered only via direct DB writes (only tests created registry rows).
These six endpoints (register + retire x three unit kinds) close that hole
behind the audited support session (staff-not-superuser + incident anchor),
following the M7-07 support-op conventions exactly:

- no session / tenant token / superuser-forged session -> 403, one deny;
- foreign-org objects -> 404 (no existence oracle);
- duplicate active registration -> 200 idempotent:true (force-resolve precedent);
- second live unit on an occupied slot -> 409 (retire first);
- retire is idempotent; every mutation writes SUPPORT_OP_HW_* with incident_id.
"""

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.auditing.models import AccountActivityLog
from apps.devices.models import (
    ActuatorHardwareRegistry,
    GatewayHardwareRegistry,
    GatewaySlot,
    HardwareRegistry,
    SaloonActuatorSlot,
    SaloonPosition,
    SaloonSensorSlot,
)
from apps.farms.models import Farm, Saloon
from apps.geography.models import City, Province
from apps.tenants.models import Organization

User = get_user_model()
PASSWORD = "strong-pass-123"

_SECRETS: dict[str, str] = {}


def _make_staff(username: str) -> User:
    from apps.accounts import mfa as mfa_mod

    user = User.objects.create_user(username=username, password=PASSWORD)
    user.is_platform_staff = True
    user.save(update_fields=["is_platform_staff"])
    row = mfa_mod.provision(user)
    assert mfa_mod.confirm(user, pyotp.TOTP(row.secret).now())
    _SECRETS[username] = row.secret
    return user


def _login(client: APIClient, username: str):
    body = {
        "username": username,
        "password": PASSWORD,
        "mfa_code": pyotp.TOTP(_SECRETS[username]).now(),
    }
    return client.post(reverse("accounts:auth-login"), body, format="json")


class HardwareRegistryBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="HW-ORG")
        self.staff = _make_staff("hw-staff")
        province = Province.objects.create(name="P-HW")
        city = City.objects.create(province=province, name="C-HW")
        self.farm = Farm.objects.create(org=self.org, name="F-HW", province=province, city=city)
        self.saloon = Saloon.objects.create(org=self.org, farm=self.farm, name="S-HW")
        self.slot = GatewaySlot.objects.create(
            slot_id="HW-GW-01", org=self.org, farm=self.farm, provisioning_status="PROVISIONED"
        )

    def _staff_token(self) -> str:
        token = _login(self.client, "hw-staff")
        self.assertEqual(token.status_code, 200, token.data)
        return token.data["access_token"]

    def _session_token(self, org=None) -> str:
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._staff_token()}")
        opened = self.client.post(
            reverse("accounts:support-session"),
            {"org_id": str((org or self.org).org_id), "incident_id": "INC-HW", "reason": "hw drill"},
            format="json",
        )
        self.assertEqual(opened.status_code, 200, opened.data)
        return opened.data["access_token"]

    def _authed(self, org=None):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._session_token(org)}")

    def _audit(self, action):
        return AccountActivityLog.objects.filter(action=action).order_by("-id").first()


class HardwareGateTests(HardwareRegistryBase):
    """All six endpoints share the support-op gate (spot-checked)."""

    ENDPOINTS = [
        ("accounts:hw-gateway-register", {}, {}),
        ("accounts:hw-gateway-retire", {}, {"hw_id": 999999}),
        ("accounts:hw-sensor-register", {}, {}),
        ("accounts:hw-sensor-retire", {}, {"hw_id": 999999}),
        ("accounts:hw-actuator-register", {}, {}),
        ("accounts:hw-actuator-retire", {}, {"hw_id": 999999}),
    ]

    def test_staff_without_session_gets_403_everywhere(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self._staff_token()}")
        for name, data, kwargs in self.ENDPOINTS:
            response = self.client.post(reverse(name, kwargs=kwargs or None), data, format="json")
            self.assertEqual(response.status_code, 403, name)

    def test_tenant_token_gets_403(self):
        user = User.objects.create_user(username="tenant-hw", password=PASSWORD)
        from apps.accounts.models import OrgMember

        OrgMember.objects.create(user=user, org=self.org, role="owner", is_active=True)
        token = AccessToken.for_user(user)
        token["org_id"] = str(self.org.org_id)
        token["roles"] = ["owner"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(token)}")
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-X", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_superuser_forged_session_gets_403(self):
        superuser = User.objects.create_user(username="root-hw", password=PASSWORD)
        superuser.is_platform_staff = True
        superuser.is_superuser = True
        superuser.save()
        forged = AccessToken.for_user(superuser)
        forged["support_session"] = True
        forged["org_id"] = str(self.org.org_id)
        forged["incident_id"] = "INC-X"
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(forged)}")
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-X", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class GatewayRegisterTests(HardwareRegistryBase):
    def test_register_creates_row_and_audits(self):
        self._authed()
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-FACTORY-1", "reason": "factory batch 7"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = GatewayHardwareRegistry.objects.get(hardware_id="HW-FACTORY-1")
        self.assertEqual(row.slot_id, "HW-GW-01")
        self.assertEqual(row.installed_by, self.staff)
        self.assertIsNone(row.removed_at)
        audit = self._audit("SUPPORT_OP_HW_REGISTER")
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details["incident_id"], "INC-HW")
        self.assertEqual(audit.details["hardware_id"], "HW-FACTORY-1")

    def test_duplicate_active_is_idempotent(self):
        self._authed()
        body = {"slot_id": "HW-GW-01", "hardware_id": "HW-DUP", "reason": "x"}
        first = self.client.post(reverse("accounts:hw-gateway-register"), body, format="json")
        second = self.client.post(reverse("accounts:hw-gateway-register"), body, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["idempotent"])
        self.assertEqual(
            GatewayHardwareRegistry.objects.filter(hardware_id="HW-DUP", removed_at__isnull=True).count(), 1
        )

    def test_second_live_unit_on_slot_is_409(self):
        self._authed()
        self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-ONE", "reason": "x"},
            format="json",
        )
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-TWO", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_foreign_slot_is_404(self):
        other = Organization.objects.create(name="HW-OTHER")
        GatewaySlot.objects.create(slot_id="HW-GW-OTHER", org=other, provisioning_status="PROVISIONED")
        self._authed()
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-OTHER", "hardware_id": "HW-X", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_missing_fields_and_reason_are_400(self):
        self._authed()
        url = reverse("accounts:hw-gateway-register")
        self.assertEqual(
            self.client.post(url, {"slot_id": "HW-GW-01", "reason": "x"}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                url, {"slot_id": "HW-GW-01", "hardware_id": "HW-X"}, format="json"
            ).status_code,
            400,
        )


class GatewayRetireTests(HardwareRegistryBase):
    def _registered(self, hw="HW-R1"):
        self._authed()
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": hw, "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return response.data["id"]

    def test_retire_closes_row_and_audits(self):
        hw_id = self._registered()
        response = self.client.post(
            reverse("accounts:hw-gateway-retire", kwargs={"hw_id": hw_id}),
            {"reason": "dead radio"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = GatewayHardwareRegistry.objects.get(pk=hw_id)
        self.assertIsNotNone(row.removed_at)
        self.assertEqual(row.removed_by, self.staff)
        audit = self._audit("SUPPORT_OP_HW_RETIRE")
        self.assertEqual(audit.details["incident_id"], "INC-HW")

    def test_retire_is_idempotent_and_unknown_is_404(self):
        hw_id = self._registered()
        url = reverse("accounts:hw-gateway-retire", kwargs={"hw_id": hw_id})
        self.assertEqual(
            self.client.post(url, {"reason": "x"}, format="json").status_code, 200
        )
        again = self.client.post(url, {"reason": "x"}, format="json")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.data["idempotent"])
        bad = self.client.post(
            reverse("accounts:hw-gateway-retire", kwargs={"hw_id": 424242}), {"reason": "x"}, format="json"
        )
        self.assertEqual(bad.status_code, 404)

    def test_retired_slot_accepts_new_unit(self):
        hw_id = self._registered("HW-OLD")
        self.client.post(
            reverse("accounts:hw-gateway-retire", kwargs={"hw_id": hw_id}), {"reason": "x"}, format="json"
        )
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": "HW-NEW", "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)


class SensorHardwareTests(HardwareRegistryBase):
    def setUp(self):
        super().setUp()
        self.position = SaloonPosition.objects.create(saloon=self.saloon, org=self.org, label="North Wall")
        self.sensor_slot = SaloonSensorSlot.objects.create(
            slot_id="S01-PACK-A", saloon=self.saloon, org=self.org, position=self.position
        )

    def test_register_retire_cycle(self):
        self._authed()
        created = self.client.post(
            reverse("accounts:hw-sensor-register"),
            {"slot_id": "S01-PACK-A", "hardware_id": "SENS-1", "reason": "install"},
            format="json",
        )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertTrue(
            HardwareRegistry.objects.filter(hardware_id="SENS-1", removed_at__isnull=True).exists()
        )
        dup = self.client.post(
            reverse("accounts:hw-sensor-register"),
            {"slot_id": "S01-PACK-A", "hardware_id": "SENS-1", "reason": "install"},
            format="json",
        )
        self.assertTrue(dup.data["idempotent"])
        clash = self.client.post(
            reverse("accounts:hw-sensor-register"),
            {"slot_id": "S01-PACK-A", "hardware_id": "SENS-2", "reason": "install"},
            format="json",
        )
        self.assertEqual(clash.status_code, 409)
        retired = self.client.post(
            reverse("accounts:hw-sensor-retire", kwargs={"hw_id": created.data["id"]}),
            {"reason": "swap"},
            format="json",
        )
        self.assertEqual(retired.status_code, 200)
        self.assertIsNotNone(self._audit("SUPPORT_OP_HW_RETIRE"))


class ActuatorHardwareTests(HardwareRegistryBase):
    def setUp(self):
        super().setUp()
        self.position = SaloonPosition.objects.create(saloon=self.saloon, org=self.org, label="South Wall")
        self.act_slot = SaloonActuatorSlot.objects.create(
            slot_id="S01-HEATER-A",
            saloon=self.saloon,
            org=self.org,
            position=self.position,
            actuator_type="heater",
        )

    def test_register_retire_cycle(self):
        self._authed()
        created = self.client.post(
            reverse("accounts:hw-actuator-register"),
            {"slot_id": "S01-HEATER-A", "hardware_id": "ACT-1", "reason": "install"},
            format="json",
        )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertTrue(
            ActuatorHardwareRegistry.objects.filter(hardware_id="ACT-1", removed_at__isnull=True).exists()
        )
        retired = self.client.post(
            reverse("accounts:hw-actuator-retire", kwargs={"hw_id": created.data["id"]}),
            {"reason": "burnout"},
            format="json",
        )
        self.assertEqual(retired.status_code, 200)
        row = ActuatorHardwareRegistry.objects.get(pk=created.data["id"])
        self.assertIsNotNone(row.removed_at)


class GatewayReplaceTests(HardwareRegistryBase):
    """H3 — the replacement ceremony: retire + fresh creds + broker continuity."""

    def _active_hw(self, hw="HW-OLD-1"):
        self._authed()
        response = self.client.post(
            reverse("accounts:hw-gateway-register"),
            {"slot_id": "HW-GW-01", "hardware_id": hw, "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        return response.data["id"]

    def _credential(self):
        from apps.mqtt_auth.models import MqttDeviceCredential
        from apps.mqtt_auth.services import hash_password

        return MqttDeviceCredential.objects.create(
            device_id="HW-GW-01", username="HW-GW-01", password_hash=hash_password("old-secret")
        )

    def test_replace_retires_old_issues_creds_and_audits(self):
        from unittest import mock

        old_id = self._active_hw()
        self._credential()
        with mock.patch(
            "apps.accounts.api_hardware.update_gateway_user_password", return_value=True
        ) as push, mock.patch(
            "apps.accounts.api_hardware.sync_gateway_acls",
            return_value={"synced": 9, "reachable": True},
        ) as sync:
            response = self.client.post(
                reverse("accounts:hw-gateway-replace", kwargs={"hw_id": old_id}),
                {"new_hardware_id": "HW-NEW-1", "reason": "lightning strike"},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["broker_synced"])
        self.assertEqual(response.data["username"], "HW-GW-01")
        self.assertTrue(response.data["password"])
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], "HW-GW-01")
        sync.assert_called_once()
        self.assertIsNotNone(GatewayHardwareRegistry.objects.get(pk=old_id).removed_at)
        new_row = GatewayHardwareRegistry.objects.get(
            hardware_id="HW-NEW-1", removed_at__isnull=True
        )
        self.assertEqual(new_row.slot_id, "HW-GW-01")
        self.assertEqual(response.data["new_id"], new_row.pk)
        audit = AccountActivityLog.objects.filter(action="SUPPORT_OP_HW_REPLACE").order_by("-id").first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details["incident_id"], "INC-HW")
        self.assertNotIn("password", audit.details)

    def test_replace_replay_does_not_reissue_password(self):
        from unittest import mock

        from apps.mqtt_auth.models import MqttDeviceCredential

        old_id = self._active_hw()
        self._credential()
        body = {"new_hardware_id": "HW-NEW-2", "reason": "x"}
        url = reverse("accounts:hw-gateway-replace", kwargs={"hw_id": old_id})
        with mock.patch("apps.accounts.api_hardware.update_gateway_user_password", return_value=True), mock.patch(
            "apps.accounts.api_hardware.sync_gateway_acls", return_value={"synced": 9, "reachable": True}
        ):
            first = self.client.post(url, body, format="json")
            issued_hash = MqttDeviceCredential.objects.get(username="HW-GW-01").password_hash
            second = self.client.post(url, body, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.data["password"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["idempotent"])
        self.assertEqual(second.data["password"], "")
        self.assertEqual(second.data["new_id"], first.data["new_id"])
        # no second issuance: the DB hash is untouched by the replay
        self.assertEqual(
            MqttDeviceCredential.objects.get(username="HW-GW-01").password_hash, issued_hash
        )

    def test_replace_broker_down_still_completes_ceremony(self):
        from unittest import mock

        old_id = self._active_hw()
        self._credential()
        with mock.patch(
            "apps.accounts.api_hardware.update_gateway_user_password", return_value=False
        ), mock.patch(
            "apps.accounts.api_hardware.sync_gateway_acls",
            return_value={"synced": 0, "reachable": False},
        ):
            response = self.client.post(
                reverse("accounts:hw-gateway-replace", kwargs={"hw_id": old_id}),
                {"new_hardware_id": "HW-NEW-3", "reason": "x"},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["broker_synced"])
        self.assertTrue(
            GatewayHardwareRegistry.objects.filter(hardware_id="HW-NEW-3", removed_at__isnull=True).exists()
        )

    def test_replace_validates(self):
        old_id = self._active_hw()
        url = reverse("accounts:hw-gateway-replace", kwargs={"hw_id": old_id})
        self.assertEqual(
            self.client.post(url, {"new_hardware_id": "HW-X"}, format="json").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                reverse("accounts:hw-gateway-replace", kwargs={"hw_id": 424243}), {"new_hardware_id": "HW-X", "reason": "x"}, format="json"
            ).status_code,
            404,
        )


class GatewayAclBackfillTests(HardwareRegistryBase):
    """H4 — pre-M8-04-H slots carry telemetry/status rules only; backfill the T1–T9 set."""

    def _legacy_rules(self):
        from apps.mqtt_auth.models import MqttAclRule, MqttDeviceCredential
        from apps.mqtt_auth.services import hash_password

        MqttDeviceCredential.objects.create(
            device_id="HW-GW-01", username="HW-GW-01", password_hash=hash_password("s3cret")
        )
        base = f"{self.org.org_id}/farms/{self.farm.pk}/gateways/HW-GW-01"
        MqttAclRule.objects.create(username="HW-GW-01", action="publish", topic=f"{base}/telemetry", permission="allow")
        MqttAclRule.objects.create(username="HW-GW-01", action="publish", topic=f"{base}/status", permission="allow")

    def test_backfill_adds_missing_downlink_rules(self):
        from unittest import mock

        self._legacy_rules()
        self._authed()
        with mock.patch(
            "apps.accounts.api_hardware.sync_gateway_acls",
            return_value={"synced": 9, "reachable": True},
        ) as sync:
            response = self.client.post(
                reverse("accounts:hw-gateway-acl-backfill", kwargs={"slot_id": "HW-GW-01"}),
                {"reason": "pre-H4 slot"},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["rules_total"], 9)
        self.assertEqual(response.data["added"], 7)
        self.assertTrue(response.data["broker_synced"])
        sync.assert_called_once()
        again = self.client.post(
            reverse("accounts:hw-gateway-acl-backfill", kwargs={"slot_id": "HW-GW-01"}),
            {"reason": "pre-H4 slot"},
            format="json",
        )
        self.assertEqual(again.data["added"], 0)
        self.assertEqual(again.data["rules_total"], 9)

    def test_backfill_foreign_slot_is_404(self):
        other = Organization.objects.create(name="HW-OTHER-2")
        GatewaySlot.objects.create(slot_id="HW-GW-OTHER", org=other, provisioning_status="PROVISIONED")
        self._authed()
        response = self.client.post(
            reverse("accounts:hw-gateway-acl-backfill", kwargs={"slot_id": "HW-GW-OTHER"}),
            {"reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
