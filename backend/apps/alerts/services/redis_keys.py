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


def pack_lwt_key(saloon_id, slot_id) -> str:
    return f"sensor:lwt:{saloon_id}:{slot_id}"


def gateway_lwt_key(org_id, gateway_slot_id) -> str:
    """Gateway-level last-will status, keyed by org + slot (2026-10-18).

    WHY org is part of the key even though the slot id is unique: the topic the
    gateway publishes on is `{org}/farms/{farm}/gateways/{slot}/status`, and
    the org segment is the only unforgeable part (the ACL pins it). Keeping the
    org in the key means a message that arrives on a topic outside the gateway's
    own ACL — impossible today, but detectable in a log rather than silently
    overwriting another tenant's status — cannot do so.
    No TTL on purpose: LWT is durable state, refreshed by the next publish.
    """
    return f"gateway:lwt:{org_id}:{gateway_slot_id}"


def sensor_status_key(saloon_id, slot_id) -> str:
    # slot pk is a CharField; str() keeps the signature tolerant in tests.
    return f"sensor:status:{saloon_id}:{slot_id}"


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


def slot_saloon_cache_key(org_id: str, slot_id: str) -> str:
    """24h cache mapping (org_id, slot_id) -> saloon_id for the live fast-path."""
    return f"slot:saloon:{org_id}:{slot_id}"


def live_stream_active_key(saloon_id: int, slot_id: str) -> str:
    """Ephemeral marker + full live snapshot set by Subscriber during Auto-RoC.
    Holds JSON with stream_state, trigger_metric, roc_delta, metrics, etc. (60s TTL).
    NOTE: MUST remain populated for 60s even on stream_state == 'ENDED' so
    velocity.py's 60s periodic check can distinguish settled tests from blind spikes."""
    return f"sensor:live_stream:{saloon_id}:{slot_id}"


def live_stream_slots_set_key(saloon_id: int) -> str:
    """Set of currently streaming (non-ENDED) slot_ids in a saloon for instant
    WebSocket Late-Joiner snapshots without scanning Redis."""
    return f"sensor:live_slots:{saloon_id}"


def live_session_meta_key(saloon_id: int, slot_id: str) -> str:
    """Temporary per-session stats (started_at, peak_roc_delta, packet_count)
    aggregated until stream_state == 'ENDED' to emit one audit row."""
    return f"sensor:live_session_meta:{saloon_id}:{slot_id}"


def live_audit_guard_key(saloon_id: int, slot_id: str, session_id: str) -> str:
    """NX guard ensuring each live Auto-RoC session emits at most one
    SENSOR_LIVE_TEST_COMPLETED row in AccountActivityLog."""
    return f"sensor:live_audit_guard:{saloon_id}:{slot_id}:{session_id}"


def live_suppressed_audit_guard_key(saloon_id: int, slot_id: str, metric: str) -> str:
    """NX guard ensuring suppressed single-slot test spikes emit at most one
    SENSOR_LIVE_TEST_SUPPRESSED audit row per suppression episode."""
    return f"sensor:live_suppressed_audit:{saloon_id}:{slot_id}:{metric}"


def slot_last_metrics_key(saloon_id: int, slot_id: str) -> str:
    """Per-slot last 60s reading snapshot so velocity.py can compare a single
    slot's delta against peer slots in the same saloon."""
    return f"saloon:{saloon_id}:slot:{slot_id}:last_metrics"


def single_slot_spike_grace_key(saloon_id: int, slot_id: str, metric: str) -> str:
    """30s grace hold when a velocity breach occurs on a single isolated slot."""
    return f"saloon:{saloon_id}:spike_grace:{slot_id}:{metric}"
