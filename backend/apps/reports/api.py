"""reports REST API — the v1.5 daily-report endpoints (v0.7 M7-15, §4n/§4s).

- GET/POST /saloons/{saloon_id}/reports/   (list w/ date+status filters; create)
- PATCH   /reports/{report_id}/            (same-day edit + platform-staff exemption)
- POST    /reports/{report_id}/vet-review/ (SUBMITTED|FLAGGED → APPROVED|REJECTED)

Conventions mirrored from farms/api.py: org comes from the VALIDATED org_id
claim and every queryset carries it (defense-in-depth — RLS is the production
backstop); writes require the roles claim (engineer+ for report writes,
manager/doctor/owner for review). The reviewer role is READ-ONLY by design.

The FLAGGED branch is D24-8 verbatim: any medication line on a created report
forces status=FLAGGED ATOMICALLY with (a) the auto support_tickets row
(category=medical, linked_report_id set, assigned to the org's first active
doctor — NULL stays unassigned when the org has none) and (b) the WS push to
the org's reports group (`reports_{org_id}` — same org-in-the-name tenancy
boundary as the telemetry groups, Decision 19).

Edit time-lock (D24-3): a tenant role may edit ONLY while the report is still
same-calendar-day in Asia/Tehran (cutoff 00:00 sharp, no grace). Platform
staff on an active support session (§4.5 — support_session claim + platform
identity, never a bare superuser) may edit past the window; every such edit
writes an auditable REPORT_ADMIN_EDIT row.

فارسی: نقاط پایانیِ گزارش روزانه — دارو یعنی FLAGGED با تیکت پزشکی خودکار و
ارسال وب‌سوکت؛ ویرایش فقط در همان روزِ تقویمی تهران مگر کارکنان پلتفرم با
نشستِ ممیزشده.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.permissions import _claims
from apps.auditing.models import AccountActivityLog
from apps.farms.models import Saloon
from apps.reports.models import (
    DailyReport,
    DailyReportExtras,
    ReportMedicationUsage,
    SupportTicket,
    VetMedicationCatalog,
)
from apps.telemetry.models import FlockCycle

TEHRAN = ZoneInfo("Asia/Tehran")

# Engineer-and-above write the report (the §4n work surface); reviewer is
# deliberately absent — read-only org-wide on this family.
WRITE_ROLES = {"engineer", "manager", "owner"}
# Review decision (vet-review): manager/doctor/owner; reviewer read-only.
REVIEW_ROLES = {"manager", "doctor", "owner"}
# The PATCH time-lock exempts exactly §4.5 support sessions — platform
# identity AND the session claim, never a bare superuser.


def _org_id(request):
    return _claims(request).get("org_id")


def _roles(request):
    return set(_claims(request).get("roles", []))


def _is_staff_session(request) -> bool:
    claims = _claims(request)
    return (
        claims.get("support_session") is True
        and getattr(request.user, "is_platform_staff", False)
        and not getattr(request.user, "is_superuser", False)
    )


def _mortality(request):
    """The yaml's `minimum: 0` — None normalizes to 0, negatives are 400s."""
    value = request.data.get("mortality_count", 0)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


# ---------------------------------------------------------------------------
# M7-16 — the §4s R-02 merge (docs/api-reports-merge-v1.md). Flutter names
# are accepted INPUT aliases; canonical names win on output; both-sent-with-
# different-values is a 400; new fields land in daily_report_extras lazily.
# ---------------------------------------------------------------------------

FIELD_ALIASES = {
    "mortality_count": "casualties_today",
    "feed_kg": "feed_consumed_kg",
    "water_l": "water_consumed_l",
    "avg_weight_g": "avg_sample_weight_g",
}
# Persian literals are input aliases of the English enums (§2) — client-side
# localization stays client-side; the wire enum is English.
VENTILATION_ALIASES = {"پایدار": "STABLE", "نیازمند بررسی": "NEEDS_REVIEW", "مشکل‌دار": "PROBLEM"}
HEALTH_ALIASES = {"عادی": "NORMAL", "هشدار": "WARNING", "بحرانی": "CRITICAL"}
VENTILATION_ENUM = ("STABLE", "NEEDS_REVIEW", "PROBLEM")
HEALTH_ENUM = ("NORMAL", "WARNING", "CRITICAL")
# Normalized-view keys that live on the 1:1 side table, never on daily_reports.
EXTRA_FIELDS = (
    "bird_count",
    "avg_temperature_c",
    "avg_humidity_pct",
    "ventilation_status",
    "health_status",
    "checklist_feed",
    "checklist_water",
    "checklist_device",
)


def _normalize_write(data) -> tuple[dict, list[str]]:
    """Fold a POST/PATCH body into the canonical view (§2 merge rules).

    Returns (fields, errors): fields carries ONLY keys the caller actually
    sent (aliases folded to canonical names); errors collects every problem
    (conflicts, bad enum literals) so one response can name the first.
    """
    errors: list[str] = []
    fields: dict = {}

    for canonical, alias in FIELD_ALIASES.items():
        has_c, has_a = canonical in data, alias in data
        if has_c and has_a and data[canonical] != data[alias]:
            errors.append(f"conflicting fields: {alias} vs {canonical}")
        elif has_c:
            fields[canonical] = data[canonical]
        elif has_a:
            fields[canonical] = data[alias]

    # status vs is_draft (bool): true→DRAFT, false→SUBMITTED; conflicting
    # with an explicit status is the same both-names-400 rule.
    status_value = data.get("status")
    if data.get("is_draft") is not None:
        mapped = "DRAFT" if data["is_draft"] else "SUBMITTED"
        if status_value is not None and status_value != mapped:
            errors.append("conflicting fields: is_draft vs status")
        else:
            status_value = mapped
    if status_value is not None:
        fields["status"] = status_value

    if data.get("bird_count") is not None:
        value = data["bird_count"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append("bird_count must be a non-negative integer")
        else:
            fields["bird_count"] = value

    for key, aliases, enum in (
        ("ventilation_status", VENTILATION_ALIASES, VENTILATION_ENUM),
        ("health_status", HEALTH_ALIASES, HEALTH_ENUM),
    ):
        if data.get(key) is not None:
            value = aliases.get(data[key], data[key])
            if value not in enum:
                errors.append(f"{key} must be one of {list(enum)} (Persian literals accepted)")
            else:
                fields[key] = value

    for key in ("avg_temperature_c", "avg_humidity_pct"):
        if data.get(key) is not None:
            fields[key] = data[key]

    checklist = data.get("checklist")
    if checklist is not None:
        if not isinstance(checklist, dict):
            errors.append("checklist must be an object {feed, water, device}")
        else:
            for part in ("feed", "water", "device"):
                if part in checklist:
                    fields[f"checklist_{part}"] = bool(checklist[part])

    return fields, errors


def _checklist_incomplete(fields: dict) -> bool:
    """§4 enforcement: a SUBMITTED transition needs all three checks. Any
    check the caller never sent reads as FALSE (the side-table defaults) —
    the Flutter rule mirrored server-side; DRAFT is exempt by construction
    (the gate only fires on a SUBMITTED target)."""
    return any(not fields.get(k, False) for k in ("checklist_feed", "checklist_water", "checklist_device"))


def _write_extras(report: DailyReport, fields: dict) -> None:
    """Lazy side-table write (§6): only fields actually sent create/update
    the row — canonical-only reports never get one (zero overhead)."""
    payload = {k: fields[k] for k in EXTRA_FIELDS if k in fields}
    if not payload:
        return
    DailyReportExtras.objects.update_or_create(
        report=report,
        defaults={"org_id": report.org_id, **payload},
    )


def _saloon_in_org(saloon_id: int, org_id) -> Saloon | None:
    return (
        Saloon.objects.filter(pk=saloon_id, org_id=org_id).select_related("farm").first()
    )


def _report_detail(report: DailyReport) -> dict:
    """The v1.5.0 canonical body — M7-16 treats this as the BASE the merged
    serializers append to; kept verbatim so old keys never move."""
    return {
        "id": report.id,
        "farm_id": report.farm_id,
        "saloon_id": report.saloon_id,
        "flock_id": report.flock_id,
        "report_date": report.report_date.isoformat(),
        "mortality_count": report.mortality_count,
        "feed_kg": report.feed_kg,
        "water_l": report.water_l,
        "avg_weight_g": report.avg_weight_g,
        "notes": report.notes,
        "status": report.status,
        "submitted_by": report.submitted_by_id,
        "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
        "reviewed_by": report.reviewed_by_id,
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
        "review_note": report.review_note,
        "vet_approved_by": report.vet_approved_by_id,
        "vet_approved_at": report.vet_approved_at.isoformat() if report.vet_approved_at else None,
    }


def _extras_keys(report: DailyReport) -> dict:
    """The §3 appended keys, sourced from the lazy side-table row (a report
    with no row — canonical-only — reads as all-None/False, the §6 defaults)."""
    extras = getattr(report, "extras", None)
    if extras is None:
        return {
            "bird_count": None,
            "avg_temperature_c": None,
            "avg_humidity_pct": None,
            "ventilation_status": None,
            "health_status": None,
            "checklist": {"feed": False, "water": False, "device": False},
        }
    return {
        "bird_count": extras.bird_count,
        "avg_temperature_c": extras.avg_temperature_c,
        "avg_humidity_pct": extras.avg_humidity_pct,
        "ventilation_status": extras.ventilation_status,
        "health_status": extras.health_status,
        "checklist": {
            "feed": extras.checklist_feed,
            "water": extras.checklist_water,
            "device": extras.checklist_device,
        },
    }


def _report_list_item(report: DailyReport) -> dict:
    """§3 merged list item: v1.5 list fields keep names/positions, the new
    keys append, is_completed derives from status (APPROVED = done — the
    Flutter calendar/dashboard flag)."""
    item = {
        "id": report.id,
        "report_date": report.report_date.isoformat(),
        "mortality_count": report.mortality_count,
        "feed_kg": report.feed_kg,
        "water_l": report.water_l,
        "avg_weight_g": report.avg_weight_g,
        "status": report.status,
    }
    item.update(_extras_keys(report))
    item["is_completed"] = report.status == "APPROVED"
    return item


def _report_single(report: DailyReport) -> dict:
    """§3 merged single object: the list item plus notes, the medication
    lines, reported_by/reported_at (aliases of submitted_by/at — returned
    ALONGSIDE, never replacing, the canonical keys) and the review fields."""
    item = _report_list_item(report)
    item.update(_report_detail(report))
    item["reported_by"] = report.submitted_by_id
    item["reported_at"] = report.submitted_at.isoformat() if report.submitted_at else None
    item["medications"] = [
        {
            "medication_name": m.medication_name,
            "dosage": m.dosage,
            "frequency": m.frequency,
        }
        for m in report.medications.all()
    ]
    return item


def _push_flagged(org_id, report: DailyReport, medications: list[dict], ticket_id) -> None:
    """WS push to the org's reports group (D24-8 leg 3).

    Import INSIDE the call so test modules can bind an InMemoryChannelLayer
    before the first push (the telemetry ws.py convention) — and so a channel
    outage can never take the atomic FLAGGED commit down with it: the push is
    best-effort by design, the ticket + status are the durable truth.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f"reports_{org_id}",
            {
                "type": "report.flagged",
                "report_id": report.id,
                "saloon_id": report.saloon_id,
                "flock_id": report.flock_id,
                "report_date": report.report_date.isoformat(),
                "ticket_id": ticket_id,
                "medications": medications,
            },
        )
    except Exception:  # pragma: no cover - channel outage must not roll back
        import logging

        logging.getLogger(__name__).exception("report.flagged push failed (report %s)", report.id)


def _create_medication_rows(report: DailyReport, medications: list[dict]) -> None:
    for med in medications:
        catalog_item = None
        item_id = med.get("catalog_item_id")
        if item_id is not None:
            catalog_item = VetMedicationCatalog.objects.filter(
                pk=item_id, org_id=report.org_id
            ).first()
        ReportMedicationUsage.objects.create(
            org=report.org,
            report=report,
            catalog_item=catalog_item,
            medication_name=med.get("medication_name") or "",
            dosage=med.get("dosage"),
            frequency=med.get("frequency"),
        )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def saloon_reports(request, saloon_id: int):
    org_id = _org_id(request)
    if not org_id:
        return Response({"error": "no org context"}, status=403)
    saloon = _saloon_in_org(saloon_id, org_id)
    if saloon is None:
        return Response({"error": "saloon not found in org"}, status=403)

    if request.method == "GET":
        qs = DailyReport.objects.filter(saloon_id=saloon_id, org_id=org_id).select_related("flock", "saloon").prefetch_related("extras").order_by("-report_date", "-id")
        date_from, date_to = request.query_params.get("from"), request.query_params.get("to")
        if date_from:
            qs = qs.filter(report_date__gte=date_from)
        if date_to:
            qs = qs.filter(report_date__lte=date_to)
        report_status = request.query_params.get("status")
        if report_status:
            qs = qs.filter(status=report_status)
        # M7-16: merged list shape — v1.5 keys keep names/positions, §3 keys
        # appended. Bare list (NOT {"results":…}) to stay byte-compat with
        # the M7-15 base suite; see task-1-report concerns.
        return Response([_report_list_item(r) for r in qs])

    # POST — engineer+ only (reviewer's absence from WRITE_ROLES is the
    # read-only rule in action).
    if not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    flock_id = request.data.get("flock_id")
    report_date = request.data.get("report_date")
    if not flock_id or not report_date:
        return Response({"error": "flock_id and report_date are required"}, status=400)
    try:
        flock = FlockCycle.objects.get(pk=flock_id, saloon_id=saloon_id, org_id=org_id)
    except FlockCycle.DoesNotExist:
        return Response({"error": "flock not found for this saloon"}, status=400)
    try:
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return Response({"error": "report_date must be YYYY-MM-DD"}, status=400)

    medications = request.data.get("medications") or []
    if not isinstance(medications, list) or any(not isinstance(m, dict) for m in medications):
        return Response({"error": "medications must be an array of objects"}, status=400)
    mortality = _mortality(request)
    if mortality is None:
        return Response({"error": "mortality_count must be a non-negative integer"}, status=400)

    # M7-16: fold Flutter aliases into the canonical view before anything
    # else — conflicts and bad literals are 400s that name the field.
    merged, merge_errors = _normalize_write(request.data)
    if merge_errors:
        return Response({"error": merge_errors[0]}, status=400)

    # §2 alias-aware canonicals: merged already folds casualties_today /
    # feed_consumed_kg / water_consumed_l / avg_sample_weight_g. The legacy
    # _mortality() helper only sees the canonical key, so an alias-only
    # write would otherwise store 0 — prefer the folded value when present.
    if "mortality_count" in merged:
        mv = merged["mortality_count"]
        if mv is None or not isinstance(mv, int) or isinstance(mv, bool) or mv < 0:
            return Response({"error": "mortality_count must be a non-negative integer"}, status=400)
        mortality = mv

    # §4 checklist enforcement: the Flutter form requires all three checks
    # before submit; the server mirrors it for any SUBMITTED-targeting write.
    if merged.get("status") == "SUBMITTED" and _checklist_incomplete(merged):
        return Response({"error": "checklist incomplete"}, status=400)

    # ONE row per (flock, saloon, report_date) — the doc's UNIQUE, raced
    # safely: get_or_create inside the atomic block, conflict → 400.
    with transaction.atomic():
        defaults = {
            "org": saloon.org,
            "farm": saloon.farm,
            "mortality_count": mortality,
            "feed_kg": merged["feed_kg"] if "feed_kg" in merged else request.data.get("feed_kg"),
            "water_l": merged["water_l"] if "water_l" in merged else request.data.get("water_l"),
            "avg_weight_g": merged["avg_weight_g"] if "avg_weight_g" in merged else request.data.get("avg_weight_g"),
            "notes": request.data.get("notes"),
            # Any medication line forces FLAGGED (D24-8) atomically with the
            # ticket below — medications beat an explicit is_draft/status.
            "status": "FLAGGED" if medications else "DRAFT",
            "submitted_by": request.user,
            "submitted_at": timezone.now() if medications else None,
        }
        if not medications and merged.get("status") in ("DRAFT", "SUBMITTED"):
            defaults["status"] = merged["status"]
            if merged["status"] == "SUBMITTED":
                defaults["submitted_at"] = timezone.now()
        report, created = DailyReport.objects.get_or_create(
            flock=flock,
            saloon=saloon,
            report_date=day,
            defaults=defaults,
        )
        if not created:
            return Response(
                {"error": "a report for this flock/saloon/date already exists"},
                status=400,
            )
        _write_extras(report, merged)
        ticket_id = None
        if medications:
            _create_medication_rows(report, medications)
            ticket_id = _open_medical_ticket(request, report, medications)
            _push_flagged(org_id, report, medications, ticket_id)
    return Response(_report_single(report), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def saloon_report_by_date(request, saloon_id: int, report_date: str):
    """§3 single-by-date (Flutter calendar day-selection). 404 when absent,
    400 on bad date, C-14 empty-200 on foreign saloon (never leak)."""
    saloon = _saloon_in_org(saloon_id, _org_id(request))
    if saloon is None:
        return Response({"results": [], "count": 0})
    try:
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return Response({"error": "report_date must be YYYY-MM-DD"}, status=400)
    try:
        report = DailyReport.objects.prefetch_related("extras").get(saloon_id=saloon_id, org_id=_org_id(request), report_date=day)
    except DailyReport.DoesNotExist:
        return Response({"error": "not found"}, status=404)
    return Response(_report_single(report))


def _open_medical_ticket(request, report: DailyReport, medications: list[dict]) -> int | None:
    """The FLAGGED branch's auto ticket — support_tickets, category=medical.

    Assigned to the org's FIRST ACTIVE doctor (oldest membership wins);
    an org with no doctor gets an UNASSIGNED ticket, never a failure.
    """
    from apps.accounts.models import OrgMember

    doctor = (
        OrgMember.objects.filter(org_id=report.org_id, role="doctor", is_active=True)
        .select_related("user")
        .order_by("joined_at", "id")
        .first()
    )
    med_names = ", ".join(
        sorted({m.get("medication_name") or "" for m in medications if m.get("medication_name")})
    )
    ticket = SupportTicket.objects.create(
        org=report.org,
        saloon=report.saloon,
        raised_by=request.user,
        assigned_to=doctor.user if doctor else None,
        category="medical",
        linked_report=report,
        title=f"Medication reported on {report.report_date.isoformat()} (saloon {report.saloon_id})",
        description=f"Report {report.id} was auto-flagged: medications [{med_names}]",
    )
    return ticket.id


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def report_edit(request, report_id: int):
    org_id = _org_id(request)
    if not org_id:
        return Response({"error": "no org context"}, status=403)
    report = DailyReport.objects.filter(pk=report_id, org_id=org_id).first()
    if report is None:
        return Response({"error": "report not found"}, status=404)

    staff_session = _is_staff_session(request)
    if not staff_session and not (_roles(request) & WRITE_ROLES):
        return Response({"error": "engineer role required"}, status=403)

    # Verdicts are immutable through this surface — review decisions land via
    # vet-review only, and the time-lock below governs everything else.
    if report.status in ("APPROVED", "REJECTED") and not staff_session:
        return Response({"error": "report already reviewed"}, status=403)

    # M7-16: fold aliases first so conflicts/bad literals are 400s naming
    # the field; the Tehran-day window below stays the auth gate (403).
    merged, merge_errors = _normalize_write(request.data)
    if merge_errors:
        return Response({"error": merge_errors[0]}, status=400)

    # Edit time-lock (D24-3): same calendar day ONLY, Asia/Tehran, 00:00
    # sharp, no grace. Platform staff on an active §4.5 session are exempt —
    # audited below, always.
    today_tehran = datetime.now(TEHRAN).date()
    window_open = report.report_date >= today_tehran
    if not window_open and not staff_session:
        return Response({"error": "edit window closed (past 00:00 Asia/Tehran)"}, status=403)

    if "status" in merged and merged["status"] not in ("DRAFT", "SUBMITTED"):
        return Response({"error": "status may only move between DRAFT and SUBMITTED here"}, status=400)
    if "mortality_count" in merged:
        mv = merged["mortality_count"]
        if mv is None or not isinstance(mv, int) or isinstance(mv, bool) or mv < 0:
            return Response({"error": "mortality_count must be a non-negative integer"}, status=400)

    # §4 checklist gate on the SUBMITTED target (existing + incoming merged;
    # absent checks read as FALSE per side-table defaults). Staff/session
    # exempt, same exemption as the time-lock (§4).
    effective_status = merged.get("status", report.status)
    if effective_status == "SUBMITTED" and not staff_session:
        try:
            _ex = report.extras
        except DailyReportExtras.DoesNotExist:
            _ex = None
        _current = {
            "checklist_feed": _ex.checklist_feed if _ex else False,
            "checklist_water": _ex.checklist_water if _ex else False,
            "checklist_device": _ex.checklist_device if _ex else False,
        }
        _current.update({k: merged[k] for k in ("checklist_feed", "checklist_water", "checklist_device") if k in merged})
        if _checklist_incomplete(_current):
            return Response({"error": "checklist incomplete"}, status=400)

    update_fields = []
    for field in ("mortality_count", "feed_kg", "water_l", "avg_weight_g", "notes"):
        if field in merged:
            setattr(report, field, merged[field])
            update_fields.append(field)
        elif field in request.data:
            setattr(report, field, request.data[field])
            update_fields.append(field)
    if "status" in merged and merged["status"] != report.status:
        report.status = merged["status"]
        update_fields.append("status")
        if merged["status"] == "SUBMITTED" and report.submitted_at is None:
            report.submitted_by = request.user
            report.submitted_at = timezone.now()
            update_fields += ["submitted_by", "submitted_at"]
    with transaction.atomic():
        if update_fields:
            update_fields = list(dict.fromkeys(update_fields))
            report.save(update_fields=update_fields)
        _write_extras(report, merged)

    if not window_open and staff_session:
        _audit_fields = list(update_fields) + [k for k in EXTRA_FIELDS if k in merged]
        AccountActivityLog.objects.create(
            actor=request.user,
            org_id=org_id,
            farm_id=report.farm_id,
            saloon_id=report.saloon_id,
            action="REPORT_ADMIN_EDIT",
            ip_address=request.META.get("REMOTE_ADDR"),
            details={
                "report_id": report.id,
                "report_date": report.report_date.isoformat(),
                "incident_id": _claims(request).get("incident_id"),
                "fields": _audit_fields,
            },
        )
    report = DailyReport.objects.select_related("flock", "saloon").prefetch_related("extras", "medications").get(pk=report.pk, org_id=org_id)
    return Response(_report_single(report))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def report_vet_review(request, report_id: int):
    org_id = _org_id(request)
    if not org_id:
        return Response({"error": "no org context"}, status=403)
    if not (_roles(request) & REVIEW_ROLES):
        return Response({"error": "reviewer role is read-only"}, status=403)
    report = DailyReport.objects.filter(pk=report_id, org_id=org_id).first()
    if report is None or report.status not in ("SUBMITTED", "FLAGGED"):
        return Response({"error": "report not found or not reviewable"}, status=404)

    decision = request.data.get("decision")
    if decision not in ("APPROVED", "REJECTED"):
        return Response({"error": "decision must be APPROVED or REJECTED"}, status=400)

    report.status = decision
    report.reviewed_by = request.user
    report.reviewed_at = timezone.now()
    report.review_note = request.data.get("review_note")
    report.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
    return Response(_report_detail(report))
