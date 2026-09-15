"""FarmTelemetryConsumer — the compat WS shim (M6-09, spec §7 / C16).

The shipped Flutter build opens ONE socket per FARM: `WS /ws/farms/{farmSlug}/
telemetry?token=<JWT>` (legacy) with the canonical `/api/compat/v1/ws/farms/...`
twin. Additive-only: this consumer SUBSCRIBES to the existing per-saloon
`saloon_{org_id}_{saloon_id}` groups (Walkthrough §5.1) and translates every
core `telemetry.update` fan-out into the flat record of spec §6 — the core
consumer, message set and close codes stay untouched.

Verdicts mirror the core socket exactly (same ws:active Set, same cap config):
  ① bad token / unknown or foreign farm slug / non-member → close 4003
  ② token valid but ws_max_connections_per_user already full → close 4000
  ③ else register, group_add EVERY saloon group of the farm, greet.

Outbound rules (spec §7): first message {"type": "connection_established",
...}; buffered rows are NEVER pushed (gap-fill stays REST history with client
dedup on sent_ts) — the M6-06 producer carries is_buffered on the fan-out
precisely so shims can enforce this without re-reading the hypertable.
Inbound {"action":"subscribe"|"unsubscribe"} is accepted and refreshes the
cap-key TTL; anything else is ignored safely (never an exception).

Auth runs in an executor thread like every Channels sync consumer: token
verification is pure-JWT (no ORM), and the slug/membership reads sit inside
transaction.atomic() + tenant_context (the M6-06 GUC lesson).

فارسی: سوکتِ سازگاریِ مزرعه — عضویت در گروه‌های موجودِ سالن‌ها، ترجمهٔ پیام به
رکوردِ تختِ فلاتر، هرگز ردیفِ بافرشده پوش داده نمی‌شود؛ ۴۰۰۳ احراز/اسلاگ و
۴۰۰۰ سقف.
"""

import logging

from channels.generic.websocket import JsonWebsocketConsumer

from apps.compat import services_compat as svc
from apps.telemetry import ws

logger = logging.getLogger(__name__)


class FarmTelemetryConsumer(JsonWebsocketConsumer):
    """One farm socket = N saloon groups, flat-record outbound."""

    def connect(self):
        verdict = self._authorize()
        if verdict is not None:
            # BUG-6 (QA 15.2): accept-then-close - a close() before accept()
            # becomes a bare HTTP 403 handshake and the 4xxx code is lost.
            # فارسی: ابتدا accept سپس close تا کد ۴xxx تحویل شود.
            self.accept()
            self.close(code=verdict)
            return

        self.connection_id = ws.register_connection(self.user.pk)
        cap = ws.config_int("ws_max_connections_per_user", 10)
        if ws.active_count(self.user.pk) > cap:
            # BUG-6: same accept-then-close rule as the deny branch above.
            self.accept()
            self.close(code=4000)
            return

        self.accept()
        self._join_farm_groups()
        self.send_json({
            "type": "connection_established",
            "farm_slug": self.farm_slug,
            "org_id": self.org_id,
            "saloon_count": len(self.saloon_ids),
        })

    def disconnect(self, code):
        if getattr(self, "connection_id", None) and getattr(self, "user", None):
            ws.drop_connection(self.user.pk, self.connection_id)
        self._leave_farm_groups()

    def receive_json(self, content=None, **kwargs):
        ws.refresh_connection_ttl(self.user.pk)
        content = content or {}
        if content.get("action") in ("subscribe", "unsubscribe"):
            return  # accepted; TTL refreshed above is the whole effect
        # Anything else is ignored safely (C16 posture).

    # ------------------------------------------------------------------
    # Core fan-out translation (dispatch key: telemetry.update)
    # ------------------------------------------------------------------

    def telemetry_update(self, event):
        if event.get("is_buffered"):
            return  # buffered rows are NEVER pushed (spec §7 / Walkthrough §5)
        saloon_id = event.get("saloon_id")
        flat = svc.compat_flatten_reading(_ShimRow(event), self.saloon_slugs.get(saloon_id, ""))
        self.send_json(flat)
    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _authorize(self):
        token = self._query_param(self.scope.get("query_string", b""), "token")
        if not token:
            return 4003
        user, claims = self._resolve_token(token)
        if user is None:
            return 4003
        org_claim = claims.get("org_id")
        if not org_claim:
            return 4003

        kwargs = self.scope["url_route"]["kwargs"]
        # BUG-7 (QA 15.7): compat route passes farmSlug but consumer read farm_slug → KeyError on every connect. Accept both spellings; canonical name stays farm_slug.
        # فارسی: ناهماهنگی نام پارامتر باعث سقوط هر اتصال سازگاری بود.
        farm_slug = kwargs.get("farm_slug", kwargs.get("farmSlug"))
        from django.db import transaction
        from apps.farms.models import Saloon
        from apps.tenants.context import tenant_context

        with transaction.atomic(), tenant_context(org_claim):
            row, farm = svc.compat_resolve_farm(farm_slug, org_claim)
            if farm is None:
                return 4003
            from apps.accounts.models import OrgMember

            member = OrgMember._default_manager.filter(
                user=user, org_id=org_claim, is_active=True
            ).exists()
            if not member:
                return 4003
            saloon_ids = list(
                Saloon._default_manager.filter(org_id=org_claim, farm_id=farm.pk)
                .values_list("pk", flat=True)
            )
            slugs = {
                r.saloon_id: r.slug
                for r in _slug_rows(org_claim, kind="device")
                if r.saloon_id in set(saloon_ids)
            }

        if not saloon_ids:
            return 4003  # a farm with no devices has nothing to subscribe to

        self.user = user
        self.org_id = str(org_claim)
        self.farm_slug = farm_slug
        self.saloon_ids = saloon_ids
        self.saloon_slugs = slugs

        cap = ws.config_int("ws_max_connections_per_user", 10)
        if ws.active_count(user.pk) >= cap:
            return 4000
        return None

    def _join_farm_groups(self):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        for saloon_id in self.saloon_ids:
            try:
                async_to_sync(layer.group_add)(
                    ws.group_name(self.org_id, saloon_id), self.channel_name
                )
            except Exception:
                logger.exception("compat group_add failed for saloon=%s", saloon_id)

    def _leave_farm_groups(self):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            for saloon_id in getattr(self, "saloon_ids", []) or []:
                async_to_sync(layer.group_discard)(
                    ws.group_name(self.org_id, saloon_id), self.channel_name
                )
        except Exception:
            logger.exception("compat group_discard failed — membership dies with the socket")

    @staticmethod
    def _query_param(query_bytes, name):
        from urllib.parse import parse_qs

        if isinstance(query_bytes, bytes):
            query_bytes = query_bytes.decode("utf-8", "replace")
        try:
            values = parse_qs(query_bytes or "").get(name)
        except Exception:
            return None
        return values[0] if values else None

    @staticmethod
    def _resolve_token(token):
        from django.contrib.auth import get_user_model
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        try:
            access = AccessToken(token)
        except TokenError:
            return None, None
        user = get_user_model().objects.filter(pk=access.payload.get("user_id")).first()
        if user is None:
            return None, None
        return user, dict(access.payload)


def _slug_rows(org_id, kind):
    from apps.compat.models import LegacySlugMap

    return list(LegacySlugMap.objects.filter(org_id=org_id, kind=kind))


class _ShimRow:
    """Adapts a core telemetry.update fan-out event to what
    compat_flatten_reading expects (attribute access; no DB round-trip).
    received_at stamps the translation moment — the push fires right after
    the row's commit, so now() IS the server-received truth for live rows."""

    def __init__(self, event):
        from django.utils import timezone

        self.server_received_ts = timezone.now()
        self.device_ts = event.get("device_ts")
        self.is_buffered = bool(event.get("is_buffered"))
        self.buffered_delay_mins = None
        self.metrics = event.get("metrics") or {}
