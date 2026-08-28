"""Redis key builders — ONE module owns every alert key name (D21 spirit).

Names mirror the canon exactly (Walkthrough §6 / Saas D8–D11); nothing else
may hardcode a key string. Centralising them means a rename is a one-line
change and grep-proof.
فارسی: تنها محلِ ساختِ کلیدهای ردیس برای هشدارها — هیچ جای دیگری رشتهٔ کلید
ننوشته شود.
"""


def thresholds_hash(saloon_id: int) -> str:
    return f"saloon:{saloon_id}:thresholds"


def transition_grace_key(saloon_id: int) -> str:
    """Written ONLY when a threshold hash actually changes (D9 / Walkthrough
    §4.1 line 663): suppresses non-velocity firing while rules settle.
    TTL comes from seeded threshold_transition_grace_sec (=300)."""
    return f"threshold:transition:{saloon_id}"


def breach_key(saloon_id: int, metric: str) -> str:
    return f"saloon:{saloon_id}:breach:{metric}"


def active_alert_key(saloon_id: int, metric: str) -> str:
    """JSON blob (org_id/farm_id/fired_at/current_tier/…) so sweeps can
    re-open a tenant context without touching Postgres first."""
    return f"alert:active:saloon:{saloon_id}:{metric}"


def paused_key(alert_id: str) -> str:
    return f"alert:paused:{alert_id}"


def sensor_status_key(saloon_id, slot_id) -> str:
    # slot pk is a CharField; str() keeps the signature tolerant in tests.
    return f"sensor:status:{saloon_id}:{slot_id}"


def los_fired_key(saloon_id, slot_id) -> str:
    """SET NX guard (Blueprint §5 'setnx') so the 60 s LOS sweep raises at most
    ONE alert per outage episode. Deleted by the sweep's recovery path once
    every sensor:status key of the saloon is live again, so the NEXT outage
    can page again. فارسی: قفلِ یک‌بارِ صدورِ هشدارِ LOS برای هر رخدادِ قطع."""
    return f"los:fired:{saloon_id}:{slot_id}"


def replay_pending(gateway_slot: str) -> str:
    return f"replay:pending:{gateway_slot}"


def replay_sweeping(gateway_slot: str) -> str:
    """NX marker so concurrent workers dispatch the sweep exactly once."""
    return f"replay:sweeping:{gateway_slot}"
