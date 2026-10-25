"""Alert lifecycle sweeps — escalation heartbeat (M4-05), LOS watchdog (M4-07);
replay follows in M4-08. These functions run as thin Celery task bodies; all
business logic lives here. Every sweep is idempotent: calling twice has the
same effect as calling once.

Escalation canon (TUTORIAL §3.4 timeline): Tier 1 fires inline via evaluation;
the 2-minute sweep promotes Tier 2 at 15 min and Tier 3 at 30 min of
UNRESOLVED breaching — never demotes, skips ack-paused alerts, resolves when
track_breach deleted the breach key (hysteresis passed), and NEVER touches
REPLAY_ANALYSIS rows (labeled historical, never pushed — DEMO §2) nor
metric_type='los' rows (LOS has no breach key; its full lifecycle — firing,
exactly-once, recovery — is owned by the LOS watchdog below).
فارسی: ضربانِ ارتقا — سطح ۲ در دقیقهٔ ۱۵ و سطح ۳ در دقیقهٔ ۳۰؛ هرگز تنزل نیست،
هشدارهای بی‌صدا‌شده را رد می‌کند، ردیف‌های بازپخش و ردیف‌های LOS را لمس نمی‌کند.
"""

import logging
import time

from django.db import transaction
from django.utils import timezone as dj_tz

from apps.alerts.models import AlertInstance
from apps.alerts.services.breach import breach_value
from apps.alerts.services.redis_keys import (
    active_alert_key,
    breach_key,
    farm_correlation_key,
    farm_los_events_key,
    los_fired_key,
    paused_key,
    sensor_status_key,
)

logger = logging.getLogger(__name__)

# Delivery sinks per tier — REAL channels land in v0.6/v0.7; v0.4 logs the
# routing decision so the timeline is observable end-to-end.
DELIVERY_SINKS = {1: "push_workers", 2: "sms_engineer", 3: "manager_and_doctor"}


from apps.tenants.context import admin_alias


def _admin_alias() -> str:
    """BUG-9: RLS-bypassing read alias for CROSS-ORG discovery only.

    The worker connects as `morghdari_app` under FORCE RLS; a default-alias
    read with no `tenant_context` sees ZERO rows, so the escalation sweep and
    the LOS watchdog silently did nothing (QA 8.4b/9.x/10.9). Discovery runs
    on the sanctioned admin alias - the SAME pattern as
    alerts.services.replay._admin_alias / rebuild_state / thresholds.recache.
    Every per-alert read/write below still runs inside tenant_context on the
    default (RLS-governed) alias, so isolation is preserved.

    Thin wrapper over the shared apps.tenants.context.admin_alias (unified
    2026-09-15; server 2026-09-19 runs the shared import directly) — kept so
    existing tests and callers keep working.
    """
    return admin_alias()


def _config_int(key: str, default: int) -> int:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _config_float(key: str, default: float) -> float:
    from apps.system_config.models import SystemConfiguration

    raw = SystemConfiguration.objects.filter(key=key).values_list("value", flat=True).first()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def system_redis():
    """Redis connection for sweeps (same URL as consumers/subscriber)."""
    import os

    import redis as redis_lib

    url = os.environ.get("REDIS_URL", "redis://:dev_redis_password@localhost:6379")
    return redis_lib.Redis.from_url(url, decode_responses=True)


def clear_active_pointer(*, saloon_id, metric, client=None) -> None:
    """BUG-9b: drop the `alert:active:*` coalesce blob for (saloon, metric).

    Must run on EVERY path that closes an alert (sweep recovery AND the
    support force-resolve). The blob is the only record the engine keeps of
    an OPEN alert: a stale blob makes the next breach "coalesce" into a dead
    row, so that metric never pages again (QA 8.1/8.2b/8.8).
    """
    client = client or system_redis()
    client.delete(active_alert_key(saloon_id, metric))


def _save_alert(alert, fields: list[str]) -> None:
    """Persist inside the alert's own tenant context — sweeps cross orgs, and
    the worker connection runs RLS-gated exactly like request traffic."""
    from apps.tenants.context import tenant_context

    with transaction.atomic():
        with tenant_context(str(alert.org_id)):
            # PROD-FIX 2026-09-19 (live on VPS 82.115.17.252): an alert
            # discovered on the admin alias carries _state.db="admin" — a bare
            # save() would then write over the admin alias, outside the
            # RLS-enforced default path this fix exists to preserve. Pin the
            # write to the default alias.
            # فارسی: نوشتن روی alias پیشفرض تا GUC و RLS معتبر بماند.
            alert.save(update_fields=fields, using="default")


def _resolve_replay_alerts(client, now) -> int:
    """Close retroactive (REPLAY_ANALYSIS) alerts — they have no breach key.

    The main loop below EXCLUDES them on purpose: its recovery branch keys on
    the breach key, which a retroactive alert never arms, so it would close
    them within two minutes of firing and nobody would ever see them. The cost
    of that exclusion was that they never closed at all — found live on
    2026-10-25: an alert open since the previous day, its value silently
    re-coalescing with every later backlog, because the one-active-row rule
    means each new backlog updates the same immortal row.

    They now close on their own terms:
      * a visible dwell (replay_alert_min_age_min, default 30 min) AND the
        saloon's newest reading back inside the band, or
      * the age backstop (replay_alert_ttl_hours, default 24) regardless —
        the retroactive review has been available for a day and must not hang.
    فارسی: هشدارهای بازپخشِ گذشته‌نگر کلید نقض ندارند؛ چرخهٔ عمر جداگانه دارند.
    """
    from apps.alerts.services.redis_keys import thresholds_hash
    from apps.telemetry.models import SaloonTelemetry

    min_age_sec = max(_config_int("replay_alert_min_age_min", 30), 1) * 60
    max_age_sec = max(_config_int("replay_alert_ttl_hours", 24), 1) * 3600

    changed = 0
    rows = AlertInstance.objects.using(admin_alias()).filter(
        source="REPLAY_ANALYSIS", resolved_at__isnull=True
    )
    for alert in rows:
        age = (now - alert.triggered_at).total_seconds()
        if age < min_age_sec:
            continue  # still inside the visible dwell

        recovered = age >= max_age_sec  # backstop: always close eventually
        if not recovered:
            max_str = client.hgetall(thresholds_hash(alert.saloon_id)).get(
                f"{alert.metric_type}_max"
            )
            if max_str is not None:
                latest = (
                    SaloonTelemetry.objects.using(admin_alias())
                    .filter(saloon_id=alert.saloon_id)
                    .order_by("-device_ts")
                    .values_list("metrics", flat=True)
                    .first()
                )
                value = (latest or {}).get(alert.metric_type)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    recovered = float(value) <= float(max_str)
        if not recovered:
            continue

        alert.current_status = "RESOLVED"
        alert.resolved_at = now
        _save_alert(alert, ["current_status", "resolved_at"])
        logger.info("replay alert %s resolved (retroactive review closed)", alert.id)
        changed += 1
    return changed


def escalate_all(now_dt=None) -> int:
    """Main 2-minute sweep: promote tiers, resolve recovered, honor pauses.

    Returns number of alerts whose status changed.
    فارسی: چرخشِ دومین‌نامهٔ ارتقا؛ خروجی تعدادِ تغییرات است.
    """
    client = system_redis()
    now = now_dt or dj_tz.now()

    # GS-MAINT-MUTE: ended mute windows that suppressed alerts get their ONE
    # digest line here (the escalation loop is the doc-designated writer).
    from apps.alerts.services.mutes import write_maintenance_mute_digests

    write_maintenance_mute_digests(now)

    tier2_sec = max(_config_int("escalation_tier2_min", 15), 1) * 60
    tier3_sec = max(_config_int("escalation_tier3_min", 30), 1) * 60

    active = (
        AlertInstance.objects.using(admin_alias())  # BUG-9: cross-org discovery
        .filter(resolved_at__isnull=True)
        .exclude(source="REPLAY_ANALYSIS")
        # M4-07: LOS rows have NO breach key ever — the branch below would
        # falsely RESOLVE a live LOS alert within 2 minutes. LOS lifecycle
        # (fire once / recover) belongs entirely to los_watchdog().
        .exclude(metric_type="los")
    )
    changed = 0

    # ── REPLAY_ANALYSIS rows get their own lifecycle (see the helper) ──────
    changed += _resolve_replay_alerts(client, now)

    for alert in active:
        if client.exists(paused_key(str(alert.id))):
            logger.debug("alert %s paused — escalation timer frozen", alert.id)
            continue

        bkey = breach_key(alert.saloon_id, alert.metric_type)

        if not client.exists(bkey):
            # Recovery: track_breach deletes the key only after the value
            # dropped past hysteresis — seeing no key means genuinely better.
            alert.current_status = "RESOLVED"
            alert.resolved_at = now
            _save_alert(alert, ["current_status", "resolved_at"])
            clear_active_pointer(saloon_id=alert.saloon_id, metric=alert.metric_type, client=client)
            # M7-04 observability: resolved counter (Redis bridge — runbook §3.1).
            from apps.monitoring.metrics import bump_alert_resolved

            bump_alert_resolved(client, str(alert.highest_tier_reached or 1))
            logger.info("alert %s resolved (metric recovered)", alert.id)
            changed += 1
            continue

        latest = breach_value(client, alert.saloon_id, alert.metric_type)
        elapsed = (now - alert.triggered_at).total_seconds()

        # Promotion only — a tier already delivered is never taken back.
        current_tier = alert.highest_tier_reached or 1
        new_status = None
        new_tier = current_tier
        if elapsed >= tier3_sec and current_tier < 3:
            new_status, new_tier = "ACTIVE_TIER_3", 3
        elif elapsed >= tier2_sec and current_tier < 2:
            new_status, new_tier = "ACTIVE_TIER_2", 2

        if new_status:
            alert.current_status = new_status
            alert.highest_tier_reached = new_tier
            fields = ["current_status", "highest_tier_reached"]
            if latest is not None:
                alert.current_value = latest
                fields.append("current_value")
            _save_alert(alert, fields)
            _deliver_notification(alert, new_tier)
            changed += 1
        elif latest is not None and float(alert.current_value or 0) != latest:
            # Keep current_value fresh between promotions (exact rebuild data).
            alert.current_value = latest
            _save_alert(alert, ["current_value"])

    return changed


def _deliver_notification(alert, tier: int) -> None:
    """Delivery STUB (M4-05): log the routing decision per tier. Real push/SMS
    channels arrive with the v0.6 contract surface."""
    sink = DELIVERY_SINKS.get(tier, f"unknown_tier_{tier}")
    logger.info(
        "ALERT DELIVERY [%s]: saloon=%s metric=%s value=%s tier=%d",
        sink, alert.saloon_id, alert.metric_type, alert.current_value, tier,
    )


def los_watchdog(now_dt=None) -> int:
    """60-second LOS sweep (M4-07): a provisioned, ACTIVE slot whose
    sensor:status key has expired — no reading for the seeded
    sensor_status_ttl_sec (300 s) grace — raises EXACTLY ONE Tier-1 alert per
    outage episode, routed to installer + hardware team (Testing Strategy
    §2.2, Blueprint §5). Returns alerts fired this sweep.

    Two guards keep it honest:
    - Redis uptime < los_suppress_after_redis_restart_min (seeded 10, DR T3):
      after a Redis restart every sensor:status key vanishes at once — the
      sweep stays silent while keys repopulate, else one morning of false
      fleet-wide pages destroys trust in the alerting system.
    - SET NX on los:fired:{saloon}:{slot} (Blueprint §5 'setnx') plus a DB
      check for an unresolved saloon LOS row: the 60 s cadence would
      otherwise re-create the alert on every sweep.

    Recovery is owned HERE, not by escalate_all (excluded above): when every
    slot of the saloon reports again, the open LOS row resolves and the fired
    markers clear so the NEXT outage pages again. The alert row is
    saloon-scoped (alert_instances has no slot column) — the DB backstop
    keeps a second LOS slot in the same saloon from duplicating it.
    فارسی: سگمنتِ از دست رفتنِ دادهٔ حسگر — یک هشدار به ازای هر رخدادِ قطع، سکوتِ
    کامل پس از ری‌استارتِ ردیس، و بستنِ خودکار وقتی همهٔ اسلات‌ها دوباره داده دادند.
    """
    from apps.devices.models import SaloonSensorSlot
    from apps.tenants.context import tenant_context

    client = system_redis()

    suppress_min = _config_int("los_suppress_after_redis_restart_min", 10)
    uptime_sec = client.info("server").get("uptime_in_seconds", 0)
    if uptime_sec < suppress_min * 60:
        logger.debug(
            "redis recently restarted (%ds < %dm) — LOS sweep suppressed",
            uptime_sec, suppress_min,
        )
        return 0

    # LOS is a saloon-scoped alert row: group ACTIVE slots per saloon.
    # Plain cross-org reads match escalate_all's pattern; writes run inside
    # the saloon's tenant context for RLS consistency.
    by_saloon: dict = {}
    # BUG-9 (QA 9.x, LOS family): `saloon_sensor_slots` is FORCE-RLS'd too —
    # the context-free read returned ZERO slots, so the watchdog never fired.
    # Discovery runs on the admin alias; writes stay tenant-scoped below.
    # فارسی: کشفِ اسلاتهای فعال با alias ادمین.
    for slot in (
        SaloonSensorSlot.objects.using(admin_alias())
        .filter(is_active=True)
        .iterator()
    ):
        by_saloon.setdefault(slot.saloon_id, []).append(slot)

    # GS-CORRELATION pre-pass (M4-10): farms already inside their paging-
    # suppression window keep logging per-sensor rows but do NOT re-page.
    new_episodes_by_farm: dict = {}
    fired = 0
    farm_ids = {slots[0].saloon.farm_id for slots in by_saloon.values()}
    suppressed_farms = {
        fid for fid in farm_ids if client.exists(farm_correlation_key(fid))
    }

    for saloon_id, slots in by_saloon.items():
        org_id = str(slots[0].org_id)
        missing = [
            s for s in slots
            if not client.exists(sensor_status_key(saloon_id, s.pk))
        ]

        with transaction.atomic():
            with tenant_context(org_id):
                open_alert = (
                    AlertInstance.objects
                    .filter(
                        saloon_id=saloon_id,
                        metric_type="los",
                        resolved_at__isnull=True,
                    )
                    .first()
                )

                if missing:
                    if open_alert is not None:
                        continue  # episode already open — never duplicate

                    # Blueprint §5 'setnx': the first sweep to see the outage
                    # wins; concurrent sweeps find the marker and back off.
                    winners = [
                        s for s in missing
                        if client.set(los_fired_key(saloon_id, s.pk), "1", nx=True)
                    ]
                    if not winners:
                        continue

                    saloon = slots[0].saloon
                    AlertInstance.objects.create(
                        org_id=slots[0].org_id,
                        farm_id=saloon.farm_id,
                        saloon_id=saloon_id,
                        metric_type="los",
                        current_status="ACTIVE_TIER_1",
                        current_value=None,
                        source="REALTIME",
                    )
                    fired += 1
                    new_episodes_by_farm.setdefault(saloon.farm_id, []).extend(
                        s.pk for s in winners
                    )
                    # Per-sensor rows ALWAYS log; the PAGE is what correlation
                    # suppresses (GS-CORRELATION: "still log, do not re-page").
                    if saloon.farm_id in suppressed_farms:
                        logger.info(
                            "LOS row created (paging SUPPRESSED by farm "
                            "correlation): saloon=%s farm=%s slots=%s",
                            saloon_id, saloon.farm_id, [s.pk for s in missing],
                        )
                    else:
                        logger.warning(
                            "LOS ALERT [installer+hardware_team]: saloon=%s farm=%s "
                            "slots_without_data=%s (no reading for the status TTL)",
                            saloon_id, saloon.farm_id, [s.pk for s in missing],
                        )
                elif open_alert is not None:
                    # Recovery: every slot reports again → close the episode
                    # and re-arm the NX markers for the next outage.
                    open_alert.current_status = "RESOLVED"
                    open_alert.resolved_at = dj_tz.now()
                    open_alert.save(update_fields=["current_status", "resolved_at"])
                    for s in slots:
                        client.delete(los_fired_key(saloon_id, s.pk))
                    logger.info(
                        "LOS resolved: saloon=%s — all slots reporting again",
                        saloon_id,
                    )

    # ── GS-CORRELATION (M4-10): farm-wide simultaneous LOS = power outage ──
    # ≥ farm_power_outage_los_ratio of the farm's ACTIVE slots LOS within
    # farm_power_outage_window_min ⇒ ONE consolidated page, then
    # correlation_suppress_minutes of per-farm paging suppression.
    ratio = _config_float("farm_power_outage_los_ratio", 0.7)
    window_sec = max(_config_int("farm_power_outage_window_min", 3), 1) * 60
    suppress_sec = max(_config_int("correlation_suppress_minutes", 10), 1) * 60
    now_epoch = time.time()

    total_by_farm: dict = {}
    for slots in by_saloon.values():
        fid = slots[0].saloon.farm_id
        total_by_farm[fid] = total_by_farm.get(fid, 0) + len(slots)

    for fid, slot_pks in new_episodes_by_farm.items():
        zkey = farm_los_events_key(fid)
        pipe = client.pipeline()
        for pk in slot_pks:
            pipe.zadd(zkey, {str(pk): now_epoch})
        pipe.expire(zkey, window_sec * 2)  # self-cleaning window memory
        pipe.execute()

    for fid, total in total_by_farm.items():
        if total <= 0:
            continue
        zkey = farm_los_events_key(fid)
        client.zremrangebyscore(zkey, "-inf", now_epoch - window_sec)
        recent = client.zcard(zkey)
        if recent / total < ratio:
            continue
        # NX doubles as page-once AND the suppression window: the first sweep
        # crossing the ratio pages; while the key lives, nobody re-pages.
        if client.set(farm_correlation_key(fid), "1", nx=True, ex=suppress_sec):
            logger.warning(
                "FARM POWER OUTAGE [consolidated page]: farm=%s — %d/%d active "
                "slots LOS within the window; ONE page for the fleet event, "
                "per-sensor rows keep logging, re-pages suppressed %dm",
                fid, recent, total, suppress_sec // 60,
            )
        else:
            logger.info(
                "farm %s inside correlation suppression — no consolidated re-page",
                fid,
            )

    return fired
