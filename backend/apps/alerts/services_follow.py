"""Alerts follow-through services — mute-window validation (contract v1.7.2).

WHY this exists as a service: `maintenance_mutes` had a reader
(`services/mutes.py`, consulted by every alert evaluation) and NO writer at all
until F11–F13 — the model's own docstring said "Managed via Django Admin in v1 —
no REST endpoint", and no admin registration ever shipped either. This module
owns the three rules the write surface must enforce, so the window the ENGINE
reads and the window the API accepts cannot disagree:

  1. `ends_at` strictly after `starts_at` (the table's CHECK constraint, echoed
     as a 400 instead of an IntegrityError);
  2. a window no longer than `MAX_MUTE_HOURS` — an unbounded mute is how a farm
     silently stops alarming, and the spec caps it at 72 h;
  3. no overlap with another window of the SAME saloon — overlapping rows would
     double-count suppressors in the digest and make the audit trail ambiguous.

Velocity semantics are deliberately untouched: `Decision 10` (NH₃/temp velocity
always pages) lives in the evaluation path, so no mute can ever silence it. That
property is asserted by test, not by convention.

The digest note in `services/mutes.py` (no `account_activity_logs` row for a
system-authored event because `actor` is NOT NULL) is unaffected here: THESE
writes are user-authored, so they audit like every other admin action.

فارسی: اعتبارسنجیِ پنجرهٔ بی‌صداکننده — پایان بعد از شروع، حداکثر ۷۲ ساعت و بدون
همپوشانی برای همان سالن؛ سرعتِ اضطراری هرگز بی‌صدا نمی‌شود.
"""

from datetime import timedelta

from django.utils import timezone as dj_tz

from apps.alerts.models import MaintenanceMute

MAX_MUTE_HOURS = 72
MAX_REASON = 200  # maintenance_mutes.reason VARCHAR(200)


def window_error(starts_at, ends_at, *, now=None) -> str | None:
    """Human 400 message for a bad window, else None.

    `now` defaults to the request clock; a fully-past window is refused (there
    is nothing left to mute) while an already-RUNNING one is accepted on
    purpose: an operator opening a mute in the middle of the work is the normal
    case, not an error.
    """
    now = now or dj_tz.now()
    if ends_at <= starts_at:
        return "ends_at must be after starts_at"
    if ends_at - starts_at > timedelta(hours=MAX_MUTE_HOURS):
        return f"mute window may not exceed {MAX_MUTE_HOURS} hours"
    if ends_at <= now:
        return "mute window is in the past"
    return None


def overlapping_mute(saloon_id, starts_at, ends_at, *, exclude_id=None):
    """The first existing window overlapping [starts_at, ends_at), else None.

    Half-open comparison (`starts_at < new_ends AND ends_at > new_starts`) so a
    window that merely ABUTS another is not an overlap — back-to-back
    maintenance on the same saloon is a legitimate schedule.
    """
    qs = MaintenanceMute.objects.filter(
        saloon_id=saloon_id, starts_at__lt=ends_at, ends_at__gt=starts_at
    )
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    return qs.order_by("starts_at").first()
