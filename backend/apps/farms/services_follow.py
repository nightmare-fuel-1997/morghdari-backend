"""Farms follow-through services — the guards the F-routes share (contract v1.7.2).

WHY a service module instead of inline checks: F3/F6 (delete guards) and the F1
geography validation are the parts that must never be re-implemented per route.
`docs/api-followthrough-v1.md` §2 asks for the split, and the sibling
staff-only topology draft (`docs/api-topology-write-v1.md` §8) requires that
surface to IMPORT these same guards rather than duplicate them — the "two
surfaces, one rule set" property that keeps `409 farm not empty` meaning the
same thing on both.

Everything here is pure (no request, no response): the views own HTTP verdicts.

فارسی: نگهبان‌های مشترکِ مسیرهای توپولوژی — اعتبارسنجیِ جغرافیا و نگهبان‌های
حذف، تا هر دو سطحِ کاربری و کارکنان از یک قانون واحد استفاده کنند.
"""

from django.db import connection
from django.db.models.deletion import ProtectedError
from django.db.utils import OperationalError

TELEMETRY_UNMANAGED_NOTE = (
    "saloon_telemetry is an UNMANAGED hypertable that sqlite runs legitimately "
    "do not have; off Postgres the guard degrades to allow (documented, same "
    "pragmatism as the capability profile's live-metric read)."
)


def as_int(value):
    """int(value) or None — the tolerant scalar parse every guard starts with."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# F1 — geography validation
# ---------------------------------------------------------------------------


def resolve_geography(payload) -> tuple:
    """(province, city, village, error) for a farm-write body.

    The geography tables are global reference data (never org-scoped), so the
    only rule is referential honesty: province and city are REQUIRED and the
    village must belong to the city (a farm in a village of another city is a
    data error, not a preference).

    Returns `error` as a human message for the caller's 400 — the view never
    invents its own wording.
    """
    from apps.geography.models import City, Province, Village

    province_id = as_int(payload.get("province_id"))
    city_id = as_int(payload.get("city_id"))
    if province_id is None:
        return None, None, None, "province_id required (integer)"
    if city_id is None:
        return None, None, None, "city_id required (integer)"

    province = Province.objects.filter(pk=province_id).first()
    if province is None:
        return None, None, None, "unknown province_id"
    city = City.objects.filter(pk=city_id).first()
    if city is None:
        return None, None, None, "unknown city_id"
    if city.province_id != province.pk:
        return None, None, None, "city_id does not belong to province_id"

    raw_village = payload.get("village_id")
    village = None
    if raw_village not in (None, ""):
        village_id = as_int(raw_village)
        if village_id is None:
            return None, None, None, "village_id must be an integer"
        village = Village.objects.filter(pk=village_id).first()
        if village is None:
            return None, None, None, "unknown village_id"
        if village.city_id != city.pk:
            return None, None, None, "village_id does not belong to city_id"

    return province, city, village, None


# ---------------------------------------------------------------------------
# F3/F6 — delete guards (RESTRICT philosophy: never cascade)
# ---------------------------------------------------------------------------


def farm_delete_error(farm) -> str | None:
    """409 reason when a farm may not be deleted, else None.

    The spec's rule is `farm not empty` — the parent is removable only once it
    has no saloons. Anything else still holding a FK (alert history, command
    logs) is caught by the caller's ProtectedError handling and gets its own
    honest 409 message rather than a 500.
    """
    from apps.farms.models import Saloon

    if Saloon.objects.filter(farm_id=farm.pk).exists():
        return "farm not empty"
    return None


def saloon_has_telemetry(saloon_id) -> bool:
    """True when the saloon already has telemetry rows (Postgres only).

    Off Postgres the answer is False — the guard degrades to allow, documented
    on TELEMETRY_UNMANAGED_NOTE.
    """
    if connection.vendor != "postgresql":
        return False
    from apps.telemetry.models import SaloonTelemetry

    return SaloonTelemetry.objects.filter(saloon_id=saloon_id).exists()


REFERENCED = "still referenced by history"
UNVERIFIABLE = (
    "delete cannot be verified on this database backend (its reports/telemetry "
    "history tables are Postgres-only)"
)


def delete_row(row) -> str | None:
    """Delete `row`; return a 409 reason when the delete must be refused, else None.

    WHY this wraps the call instead of letting the view call `row.delete()`:

    * Django's delete collector walks EVERY reverse relation — including the
      UNMANAGED history models (`SaloonTelemetry`, `DailyReport`, …). Those
      tables exist only on Postgres (hypertable DDL + reports RunSQL), so on any
      other backend the collector dies with `no such table` before the delete
      can even be judged. Refusing is the safe direction: a row whose dependents
      could not be checked is never deleted.
    * a still-referencing relation surfaces as `ProtectedError` when the
      collector finds it, or as `IntegrityError` when the DB's own RESTRICT
      fires first (both mean the same thing to the caller).
    * the audit row is written by the CALLER after this returns None, so a
      refused delete cannot leave a `*_DELETED` line behind.
    """
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            row.delete()
    except (ProtectedError, IntegrityError):
        return REFERENCED
    except OperationalError as exc:
        if "no such table" in str(exc).lower():
            return UNVERIFIABLE
        raise
    return None


def saloon_delete_error(saloon) -> str | None:
    """409 reason when a saloon may not be deleted, else None.

    The spec (F6) words this as "no active flock AND no telemetry", but the
    SCHEMA is stricter than the spec: `flock_cycles.saloon` is ON DELETE
    RESTRICT, so a saloon that EVER housed a flock — closed ones included —
    cannot be hard-deleted at all. Postgres proves it; the sqlite loop hid it
    because Django's collector only emulates the constraint there. Promising a
    delete the database will always reject is the bug, so the guard reports the
    real rule instead: removable only while the saloon has no flock history.

    Three distinct reasons, so the caller can act rather than guess:

    * an ACTIVE flock — close the cycle first, then the saloon is still bound by
      its now-closed cycle (see the next point);
    * a CLOSED flock — the row IS the history the saloon's identity exists to
      key, and `farms_saloons` has no soft-delete column to retire it with, so
      this is honestly a 409 rather than a silent archive;
    * telemetry — the saloon was measured, so its id anchors time-series rows.

    BUG-10/BUG-18: audit rows are NOT in the list above on purpose — the
    `AccountActivityLog` farm/saloon FKs are SET_NULL, so a fresh saloon (no
    flock, no telemetry, no positions — audit lines only) IS truly deletable;
    its *_CREATED row survives with a NULL FK and intact details.

    فارسی (باگ ۱۰/۱۸): سطر حسابرسی مانع حذف نیست؛ موجودِ تازهٔ بدون سابقه
    واقعاً حذف می‌شود.
    """
    from apps.telemetry.models import FlockCycle

    cycles = FlockCycle.objects.filter(saloon_id=saloon.pk)
    if cycles.filter(end_date__isnull=True).exists():
        return "saloon has an active flock"
    if cycles.exists():
        return "saloon has flock history"
    if saloon_has_telemetry(saloon.pk):
        return "saloon has telemetry history"
    return None
