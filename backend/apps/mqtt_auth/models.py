"""
MQTT auth — device credentials + ACL rules (database.md Compartment 7).

فارسی: اعتبارنامهٔ دستگاه‌ها و قواعد ACL برای EMQX. این دو جدول global هستند
(org_id ندارند) چون EMQX مستقیم می‌خواندشان — بدون سیاست RLS.

password_hash MUST be bcrypt ($2b$...): the ONLY format EMQX 5.5's
password_based:bcrypt backend verifies natively; Django pbkdf2 strings are NOT
parseable by EMQX (AU-fix 2026-08-16). That is why bcrypt sits in
PASSWORD_HASHERS from day one.
"""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Now


class MqttDeviceCredential(models.Model):
    """device_id == gateway_slot_id. Staged rotation (AU-ROT): pending hash is
    swapped into password_hash on gateway ACK; alert if staged > 24 h."""

    device_id = models.CharField(max_length=100, unique=True)  # = gateway_slot_id
    username = models.CharField(max_length=100, unique=True)
    password_hash = models.CharField(max_length=255)  # bcrypt $2b$... (EMQX-native)
    pending_password_hash = models.CharField(max_length=255, null=True, blank=True)
    pending_since = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, null=True)
    created_at = models.DateTimeField(null=True, db_default=Now())
    last_rotated_at = models.DateTimeField(null=True, blank=True)  # drives 30-day rotation policy
    expires_at = models.DateTimeField(null=True, blank=True)  # NULL = never expires

    class Meta:
        db_table = "mqtt_device_credentials"


class MqttAclRule(models.Model):
    """Per-username topic rules read by EMQX's file/DB authorizer. Org-less:
    rules are keyed by device username, not by organization (TUTORIAL §3.9)."""

    username = models.CharField(max_length=100)
    topic = models.CharField(max_length=255)
    permission = models.CharField(max_length=10)
    action = models.CharField(max_length=10)

    class Meta:
        db_table = "mqtt_acl_rules"
        constraints = [
            models.CheckConstraint(check=Q(permission__in=["allow", "deny"]), name="ck_mqtt_acl_rules_permission"),
            models.CheckConstraint(check=Q(action__in=["publish", "subscribe", "all"]), name="ck_mqtt_acl_rules_action"),
        ]
        indexes = [
            models.Index(fields=["username"], name="idx_mqtt_acl_user"),
        ]
