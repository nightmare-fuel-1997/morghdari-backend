"""D23 capability-gated rules — the alert engine works with what the farm has.

Decision 23 (Saas.md): "threshold rules evaluate only metrics with live
sources for that saloon" — a missing metric is UNAVAILABLE, never silently
"normal". Engine-local v0.4 scope (canonical capability_profile service +
`test_capability_profile` are M6, Tracker §4l-Q5):

- THI heat-stress index: DERIVED inside the engine from co-present `temp` +
  `humidity` averages (D23 activation matrix: "live temp + humidity — derived,
  no metric row"). If either input is missing the rule is unavailable for that
  reading — never computed from one input, never assumed normal.
- `co` / `power_mains` / `power_gen`: catalog-backed rules flow through the
  normal threshold-hash path (`{metric}_max` from lifecycle rules); they can
  only evaluate when an entry actually carries the key — a saloon without the
  sensor never produces the key, so the rule stays structurally inert (the
  source gate is live-readings-only, by construction).

THI formula (NRC form, T in °C, RH in %):
    THI = (1.8·T + 32) − (0.55 − 0.0055·RH)·(1.8·T − 26)
The corpus pins the RULE (activation matrix) but not the constant — recorded
in CHANGELOG [v0.4] for retrospective ratification (M4-12).
فارسی: موتور هشدار فقط با سنسورهای واقعی سالن کار می‌کند؛ سنجهٔ غایب «عادی» نیست،
«در دسترس نیست» است. شاخص THI فقط با وجود هم‌زمانِ دما و رطوبت محاسبه می‌شود.
"""

import logging

logger = logging.getLogger(__name__)

# Metric keys whose rules are DERIVED or sensor-scarce — documented gate
# family for D23; the derived rule is handled specially in evaluation.py.
DERIVED_METRICS = ("thi",)
SENSOR_GATED_METRICS = ("co", "power_mains", "power_gen")


def compute_thi(temp_c: float, relative_humidity_pct: float) -> float:
    """NRC Temperature-Humidity Index from °C + %RH (see module docstring)."""
    rh = relative_humidity_pct
    return (1.8 * temp_c + 32.0) - (0.55 - 0.0055 * rh) * (1.8 * temp_c - 26.0)


def metric_is_live(metrics: dict, metric_key: str) -> bool:
    """D23 source gate: a rule may evaluate only when THIS reading carries the
    key — the entry IS the live source proof. Missing key ⇒ unavailable (the
    rule stays silent), never a computed 'normal'."""
    values = metrics.get(metric_key)
    return isinstance(values, dict) and values.get("avg") is not None