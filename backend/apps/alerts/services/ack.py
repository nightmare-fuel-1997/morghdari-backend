"""Ack mechanism — acknowledgement row + escalation pause key (M4-06).

TASKS canon: the ROW and the PAUSE KEY are ONE transaction — the REST
acknowledge endpoint itself arrives with the v0.6 contract surface (M6-01);
until then this service is the mechanism. Ordering inside the atomic block
gives the strongest two-store contract available: the row inserts first, the
Redis SET happens BEFORE the block exits, so a Redis failure raises out of
the transaction and rolls the row back (both-or-neither). The reverse
residue — set succeeds, commit fails — leaves only a pause key that expires
naturally (≤ pause_minutes), which the sweep treats as a brief benign freeze.

Pause TTL always comes from system_configuration key
alert_acknowledgement_pause_minutes (Audit-M5 fix — the model field default
is a seed fallback ONLY, never hardcoded per-call; D21 spirit).
فارسی: رسیدنِ هشدار یعنی یک ردیفِ رسیده‌سازی و کلیدِ توقف با مهلتِ تنظیمی، هر دو
با هم؛ اگر ردیس خطا بدهد ردیف هم برمی‌گردد.
"""

import logging

from django.db import transaction

from apps.alerts.models import AlertAcknowledgement
from apps.alerts.services.redis_keys import paused_key
from apps.tenants.context import tenant_context

logger = logging.getLogger(__name__)

CONFIG_KEY = "alert_acknowledgement_pause_minutes"


def pause_minutes() -> int:
    """Runtime pause length from configuration (fallback = model default 10).
    فارسی: طولِ توقف از پیکربندی خوانده می‌شود، نه از کد."""
    from apps.system_config.models import SystemConfiguration

    raw = (
        SystemConfiguration.objects.filter(key=CONFIG_KEY)
        .values_list("value", flat=True)
        .first()
    )
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 10


def acknowledge_alert(alert, user, notes=None, redis_client=None):
    """Acknowledge an alert: ack row + pause key, atomically together.

    Re-acknowledging adds another audit row and REFRESHES the pause window
    (an extension is exactly what a second human 'still working on it'
    means). Returns the new AlertAcknowledgement.
    فارسی: با هر رسیده‌سازی، ردیفِ جدید ثبت و مهلتِ توقف تازه می‌شود."""
    if redis_client is None:
        # Late import keeps the module importable in settings-only contexts.
        from apps.alerts.services.sweeps import system_redis

        redis_client = system_redis()

    minutes = pause_minutes()
    with transaction.atomic():
        with tenant_context(str(alert.org_id)):
            ack = AlertAcknowledgement.objects.create(
                alert=alert,
                org_id=alert.org_id,
                acknowledged_by=user,
                pause_minutes=minutes,
                notes=notes,
            )
            # INSIDE the atomic block on purpose (see module docstring):
            # redis down ⇒ exception ⇒ row rolled back.
            redis_client.set(
                paused_key(str(alert.id)), "1", ex=minutes * 60
            )

    logger.info(
        "alert %s acknowledged by user=%s — escalation paused %d min",
        alert.id, user_id(user), minutes,
    )
    return ack


def user_id(user) -> str:
    """Stable identity for logs (username preferred, pk as fallback)."""
    return getattr(user, "username", None) or str(getattr(user, "pk", "?"))
