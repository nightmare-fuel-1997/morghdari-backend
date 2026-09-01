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


def pack_lwt_key(saloon_id, slot_id) -> str:
    """Last-known pack status mirrored from the farm/{farm}/gateways/{gw}/
    status MQTT topic: 'online' (retained publish on connect) or 'offline'
    (the pack's Last Will on unclean death, or its clean-shutdown publish).
    NO TTL by design — the retained MQTT state is the durable truth, so a
    dead pack stays visible on the board until its replacement says online.
    فارسی: آخرین وضعیت شناخته‌شدهٔ پک از تاپیک وضعیت — بدون انقضا، عینِ حالت باقی‌مانده."""
    return f"sensor:lwt:{saloon_id}:{slot_id}"


def los_fired_key(saloon_id, slot_id) -> str:
    """SET NX guard (Blueprint §5 'setnx') so the 60 s LOS sweep raises at most
    ONE alert per outage episode. Deleted by the sweep's recovery path once
    every sensor:status key of the saloon is live again, so the NEXT outage
    can page again. فارسی: قفلِ یک‌بارِ صدورِ هشدارِ LOS برای هر رخدادِ قطع."""
    return f"los:fired:{saloon_id}:{slot_id}"


def farm_los_events_key(farm_id) -> str:
    """GS-CORRELATION sliding window: ZSET of slot-LOS episode starts
    (score = epoch seconds) per farm — feeds the simultaneous-LOS ratio."""
    return f"farm:los:{farm_id}"


def farm_correlation_key(farm_id) -> str:
    """GS-CORRELATION paging suppression: while present (TTL =
    correlation_suppress_minutes) individual LOS rows still log but the farm
    does not re-page."""
    return f"farm:corr:muted:{farm_id}"


def maintenance_mute_count_key(mute_id) -> str:
    """GS-MAINT-MUTE: how many threshold/duration alerts the window suppressed
    (storage via config/Redis per Tracker §4i — zero schema change)."""
    return f"maintenance:mute:count:{mute_id}"


def maintenance_mute_digest_key(mute_id) -> str:
    """GS-MAINT-MUTE: NX guard so the escalation loop writes the digest line
    for an ended window exactly once."""
    return f"maintenance:mute:digest:{mute_id}"


def replay_pending(gateway_slot: str) -> str:
    return f"replay:pending:{gateway_slot}"


def replay_sweeping(gateway_slot: str) -> str:
    """NX marker so concurrent workers dispatch the sweep exactly once."""
    return f"replay:sweeping:{gateway_slot}"


def replay_covered_key(slot_id: str) -> str:
    """High-water mark (ISO device_ts) of buffered windows already swept for a
    sensor slot — lets a LATER outage open a NEW batch_id window instead of
    being swallowed by the first episode's covered check."""
    return f"replay:covered:{slot_id}"
