"""Ops serializers — M6-10 (contract v1.7.0). Deliberately thin dict builders
like compat/serializers_compat.py: the O-keys are contractual, so they are
written once, explicitly, with the source column beside each — no ModelSerializer
magic that could silently rename a field.

فارسی: شکلِ کلیدها قراردادی است؛ پس دیکشنری‌ها آگاهانه و دستی ساخته می‌شوند.
"""


def audit_item(row) -> dict:
    """O1 row — contract keys; created_at ← AccountActivityLog.timestamp."""
    return {
        "id": str(row.id),
        "actor_id": row.actor_id,
        "username": row.actor.username if row.actor_id else None,
        "action": row.action,
        "details": row.details,
        "created_at": row.timestamp.isoformat().replace("+00:00", "Z") if row.timestamp else None,
    }


def threshold_item(rule) -> dict:
    """O2/O3 row — the real LifecycleThresholdRule schema (day-ranged bands).
    The draft spec's `enabled`/`updated_at` keys have NO columns in the frozen
    table — the ratifying yaml (and this row) matches the real schema instead
    (erratum noted in the spec file)."""
    return {
        "id": rule.id,
        "metric_key": rule.metric,
        "template": rule.template_id,
        "day_from": rule.day_from,
        "day_to": rule.day_to,
        "min": float(rule.min_value) if rule.min_value is not None else None,
        "max": float(rule.max_value) if rule.max_value is not None else None,
    }


def template_item(t) -> dict:
    """O4 row — the read-only lifecycle_templates catalog."""
    return {
        "template_id": t.template_id,
        "org_id": t.org_id,
        "breed_type": t.breed_type,
        "name": t.name,
        "is_default": bool(t.is_default),
    }


def photo_item(p) -> dict:
    """O5 row — contract keys; url ← file_url, taken_at ← uploaded_at."""
    return {
        "id": p.id,
        "url": p.file_url,
        "caption": p.description,
        "taken_at": p.uploaded_at.isoformat().replace("+00:00", "Z") if p.uploaded_at else None,
        "uploaded_by": p.uploaded_by_id,
    }


def gateway_item(slot, saloon_count: int, last_seen_at) -> dict:
    """O7 row — last_seen_at arrives from the diag register (O9's store)."""
    return {
        "slot_id": slot.slot_id,
        "farm_id": slot.farm_id,
        "provisioning_status": slot.provisioning_status,
        "last_seen_at": last_seen_at,
        "saloon_count": saloon_count,
    }
