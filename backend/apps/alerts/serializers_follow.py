"""Alerts follow-through components — docs-only serializers (contract v1.7.2).

Mirrors what `apps/alerts/api_follow.py` returns. The alert row reuses
`AlertInstanceSerializer`'s shape (built by `views._alert_out`) so the org feed
and the per-saloon feed cannot drift apart.

فارسی: طرح‌واره‌های مستندسازیِ فهرستِ سازمانیِ هشدارها و پنجره‌های بی‌صداکننده.
"""

from rest_framework import serializers

from apps.alerts.serializers import AlertInstanceSerializer


class AlertFeedResponseSerializer(serializers.Serializer):
    """GET /alerts/ — the org-wide feed (the missing sibling of the per-saloon read)."""

    results = AlertInstanceSerializer(
        many=True, help_text="Alerts newest-first, RLS- and scope-narrowed."
    )
    count = serializers.IntegerField(help_text="Total rows matching the filters.")


class MuteItemSerializer(serializers.Serializer):
    mute_id = serializers.IntegerField()
    saloon_id = serializers.CharField(help_text="Stringified saloon PK (contract shape).")
    farm_id = serializers.IntegerField(allow_null=True, required=False)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    reason = serializers.CharField()
    active = serializers.BooleanField(
        help_text="True while starts_at ≤ now < ends_at (the engine's own predicate)."
    )
    created_by = serializers.IntegerField(allow_null=True, required=False)
    created_at = serializers.DateTimeField(allow_null=True, required=False)


class MuteListResponseSerializer(serializers.Serializer):
    results = MuteItemSerializer(many=True)
    count = serializers.IntegerField()


class MuteCreateRequestSerializer(serializers.Serializer):
    """POST /maintenance-mutes/ body (F12).

    `starts_at`/`ends_at` are ISO-8601 (a trailing `Z` is accepted). The window
    is capped at 72 hours, must end after it starts, must not be entirely in the
    past, and must not overlap another window of the same saloon.
    """

    saloon_id = serializers.IntegerField(help_text="Saloon of the caller's org.")
    starts_at = serializers.CharField(help_text="ISO-8601 window start.")
    ends_at = serializers.CharField(help_text="ISO-8601 window end (≤ 72 h after start).")
    reason = serializers.CharField(
        max_length=200, help_text="Why the saloon is under maintenance (audit text)."
    )


class MuteRevokeResponseSerializer(serializers.Serializer):
    mute_id = serializers.IntegerField()
    saloon_id = serializers.CharField(help_text="Stringified saloon PK.")
    ends_at = serializers.DateTimeField(help_text="Shortened to the revocation instant.")
    status = serializers.CharField(help_text='Always "REVOKED".')
