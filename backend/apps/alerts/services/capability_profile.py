"""Capability profile service — D23 made queryable at runtime (M6-03).

Decision 23 (Saas.md): the platform works with what the farm HAS. A missing
input makes a feature UNAVAILABLE — never silently degraded, never 0, never an
error. This module is the canonical, DECLARED form of that decision:

- ``FEATURE_MATRIX`` states each feature's requirements (the RULE).
- ``compute_profile()`` compares the rule against COMPUTED TRUTH — live metric
  keys (active sensor slots' allowed_metric_keys ∪ keys seen in recent
  telemetry) and manual-log availability (row existence). Nothing is stored:
  adding a feature later needs no migration (TUTORIAL §3.4).

Pure functions only: inputs are injected sets, so the full matrix is testable
without a database (engine-local capability.py keeps the per-reading D23
source gate; this module is the saloon-level profile the REST surface serves).
M6-05 (capability profile service) extends this matrix — the endpoint needs no
change.

فارسی: پروفایل توانمندی سالن — قانون در FEATURE_MATRIX اعلام می‌شود، واقعیت از
سنسورها و لاگ‌های موجود محاسبه می‌شود؛ ورودیِ غایب یعنی «در دسترس نیست»، نه صفر.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requires:
    """Declared requirements of ONE optional feature (D23 activation matrix).

    metrics      — ALL of these metric keys must be live for the saloon.
    any_metrics  — at least ONE of these must be live (power has two sources).
    logs         — ALL of these manual log types must have ≥ 1 row.
    computed     — ALL of these computed facts must hold (facts the caller
                   derives from real computations, e.g. "threshold bands exist
                   for the flock-age window" — not simple row existence).
    enable_path  — how the farmer turns the feature on (shown when unavailable).
    """

    metrics: frozenset = frozenset()
    any_metrics: frozenset = frozenset()
    logs: tuple = ()
    computed: tuple = ()
    enable_path: str = ""


# The D23 matrix as data — the ONLY place a feature's requirements live.
# M6-03 seeded the v1.4 surface (water, THI, CO, power, FCR, biosecurity);
# M6-05 completed the v1 set with the D22 KPI features (mortality %,
# compliance %). Phase-2 rows (disease early-warning, silo level, layer
# production, benchmarking) join when their tables/modules ship — no endpoint
# change needed, only entries here.
FEATURE_MATRIX = {
    "fcr": Requires(
        logs=("weight_samples", "feed_intake"),
        enable_path="Log a weight sample or connect a weigh station",
    ),
    "water_ratio": Requires(
        logs=("water_intake", "feed_intake"),
        enable_path="Log daily water intake — the earliest disease indicator",
    ),
    "thi": Requires(
        metrics=frozenset({"temp", "humidity"}),
        enable_path="Install temp + humidity sensor packs",
    ),
    "co": Requires(
        metrics=frozenset({"co"}),
        enable_path="Install a CO sensor pack",
    ),
    "power": Requires(
        any_metrics=frozenset({"power_mains", "power_gen"}),
        enable_path="Connect mains or generator power monitoring",
    ),
    # Manual log — no sensors, no config: any org member can record.
    "biosecurity_log": Requires(),
    # M6-05 — the D22 KPI features (D23 table row "FCR / mortality % /
    # compliance % (D22) | placement_count + feed/weight logs | v1").
    "mortality_pct": Requires(
        logs=("placement", "mortality_records"),
        enable_path="Register the flock's placement count and record daily mortality",
    ),
    # Compliance availability is COMPUTED (bands for the flock-age window +
    # hours-with-data) — the same facts kpi_service.compliance_pct reports.
    "compliance_pct": Requires(
        computed=("threshold_bands", "hourly_data"),
        enable_path="Add flock-age threshold rules and keep sensor packs reporting",
    ),
}

# Human labels for log requirements (they travel in missing_inputs).
LOG_LABELS = {
    "weight_samples": "weight samples",
    "feed_intake": "feed logs",
    "water_intake": "water logs",
    "placement": "flock placement count",
    "mortality_records": "mortality records",
}

# Human labels for computed availability facts (also travel in missing_inputs).
COMPUTED_LABELS = {
    "threshold_bands": "flock-age threshold rules",
    "hourly_data": "hourly sensor data",
}


def evaluate_feature(feature: str, requires: Requires, live_metrics: set, available_logs: dict) -> dict:
    """One feature's availability against computed truth — pure.

    Returns the contract shape: {feature, available, missing_inputs, enable_path}.
    missing_inputs carries metric keys verbatim and log labels in words, so a
    farmer can act on it without knowing table names.
    """
    missing_metrics: list = []
    if requires.metrics:
        missing_metrics = sorted(m for m in requires.metrics if m not in live_metrics)
    elif requires.any_metrics and not (requires.any_metrics & live_metrics):
        missing_metrics = sorted(requires.any_metrics)
    missing_logs = [LOG_LABELS.get(log, log) for log in requires.logs if not available_logs.get(log)]
    missing_computed = [
        COMPUTED_LABELS.get(fact, fact)
        for fact in requires.computed
        if not available_logs.get(fact)
    ]

    available = not missing_metrics and not missing_logs and not missing_computed
    return {
        "feature": feature,
        "available": available,
        "missing_inputs": missing_metrics + missing_logs + missing_computed,
        "enable_path": "" if available else requires.enable_path,
    }


def compute_profile(saloon_id: int, live_metrics: set, available_logs: dict) -> dict:
    """The full saloon profile — GET /saloons/{id}/capabilities/ body."""
    return {
        "saloon_id": saloon_id,
        "live_metrics": sorted(live_metrics),
        "features": [
            evaluate_feature(feature, requires, live_metrics, available_logs)
            for feature, requires in FEATURE_MATRIX.items()
        ],
    }
