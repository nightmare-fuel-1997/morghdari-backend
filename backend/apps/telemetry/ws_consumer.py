"""SaloonTelemetryConsumer — the live dashboard socket (M6-06, Blueprint §6).

THE DIAL TONE: `WS /ws/saloons/{saloon_id}/telemetry?token=<JWT>`. One socket
per dashboard tab; every committed reading of that saloon is pushed to every
member's tab within the flush window ("UI follows DB" — TUTORIAL §3.5).

Connection verdicts (order is normative — auth BEFORE the cap so a bad token
can never consume a cap slot):
  ① no/invalid token, token without an org claim, saloon not found,
     saloon's org ≠ token org, or no ACTIVE org membership → close 4003
     (auth/org failure — the WALKTHROUGH §2 code)
  ② token valid + member, but ws:active:{user} already holds
     ws_max_connections_per_user members → close 4000 (cap — AU-WS-CAP)
  ③ else: register in the Set (TTL refreshed), join the group, greet.

Tenant rule (the WS twin of TenantOrgMiddleware ③): `saloons` AND
`saloon_telemetry` are both FORCE-RLS'd, so every query this consumer makes
runs inside tenant_context(org_claim) — the GUC seeded from the VALIDATED
token's org claim, never from a URL argument. Outside that context the app
role sees ZERO rows (fail-closed) — which is itself the cross-org rejection:
a stranger's saloon_id resolves to nothing inside their own context, indisting-
uishable from a missing saloon, and both close 4003.

On EVERY inbound message the Set's zombie-guard TTL is refreshed (a live
socket holds its cap slot exactly as long as it keeps speaking; a dead one
ages out at the 300 s TTL). Inbound messages are otherwise ignored safely —
never an exception (C16 posture).

M6-07 gap-fill: an inbound {"action": "gap_fill", "since": "<iso>"} asks for
the rows the client missed while offline. The server holds NO session state —
the handler answers from the hypertable with an ordered telemetry.gap_fill
reply and the CLIENT merges against its live stream; that statelessness is
what makes the merge trivially testable (TUTORIAL §3.5).

فارسی: مصرف‌کنندهٔ وب‌سوکتِ زنده — توکن از پارامترِ اتصال، عضویتِ فعال از
جدول اعضا، و هر پرس‌وجو درون زمینهٔ سازمانِ ادعای توکن (RLS). کدِ بستنِ ۴۰۰۳
یعنی احراز/سازمان نادرست و ۴۰۰۰ یعنی سقفِ اتصالِ پر؛ هر پیامِ ورودی TTL کلید
سقف را تازه می‌کند و پیام‌های ناشناخته بی‌خطر نادیده گرفته می‌شوند.
"""

import logging
import threading
import time
import uuid

from channels.generic.websocket import JsonWebsocketConsumer

from apps.accounts.models import OrgMember
from apps.alerts.services.redis_keys import thresholds_hash
from apps.farms.models import Saloon
from apps.telemetry import ws

logger = logging.getLogger(__name__)

# Answer ceiling for one gap_fill — the client pages with narrower `since`
# windows if it was offline for an era; unbounded queries behind an innocent
# reconnect are the TUTORIAL §6 "unbounded KPI range" trap in WS form.
GAP_FILL_MAX_ROWS = 200

# BUG-8 residual (2026-09-19): minimum seconds between late group-join
# attempts on a degraded socket. Rapid-fire inbound messages must collapse
# into one attempt — a still-dead layer answers each attempt with a fast
# raise (1s connect knob), but there is no reason to pay it per message.
REJOIN_COOLDOWN_SEC = 10.0


class SaloonTelemetryConsumer(JsonWebsocketConsumer):
    """Auth'd push socket for one saloon's committed telemetry."""

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        # BUG-6 (QA 15.2/15.3/15.4): accept-then-close. Closing BEFORE
        # accept() is translated by Daphne into a bare HTTP 403 handshake -
        # the 4xxx close code never reaches wire clients, so auth-failure
        # (re-login) vs cap-full (retry later) is indistinguishable.
        # فارسی: ابتدا accept سپس close تا کد ۴xxx واقعاً روی وایر برسد.
        verdict = self._authorize()
        if verdict is not None:
            logger.info(
                "WS connect denied code=%s saloon=%s",
                verdict,
                self.scope.get("url_route", {}).get("kwargs", {}).get("saloon_id"),
            )
            self.accept()
            self.close(code=verdict)
            return

        self.saloon_id = self.scope["url_route"]["kwargs"]["saloon_id"]
        # BUG-8 (QA 15.8): redis-down must never kill a valid WS connect.
        # Every redis touch below runs inside _post_auth_setup under ONE
        # wall-clock bound and degrades on timeout/failure (cap passes open,
        # snapshot skipped, group-join best-effort): a dead redis HANGS
        # instead of raising (8s DNS blackhole measured live — socket knobs
        # cannot bound getaddrinfo), and the sync consumer shares ONE thread,
        # so any unbounded wait stalls every socket AND holds the handshake
        # past Daphne's ~5s guillotine (live nginx 502). Auth denies above
        # stay fail-closed — the deny path touches no redis at all.
        # فارسی: همهٔ تماس‌های ردیس پس از احراز هویت با یک سقف زمانی اجرا و
        # در صورت خطا باز عبور می‌کنند؛ رد احراز هویت همچنان بسته می‌ماند.
        # The cap default comes from DB (fast) — never from the bounded block.
        try:
            cap = ws.config_int("ws_max_connections_per_user", 10)
        except Exception:
            logger.exception("WS config read degraded — default cap")
            cap = 10
        # Register FIRST, then re-check the cap with our own membership
        # counted (both inside the bounded block): between the pre-check's
        # scard and this sadd another tab may have taken the last slot — the
        # re-check catches the race and the over-cap socket closes itself
        # with 4000 (disconnect removes it).
        setup = self._post_auth_setup(cap, timeout=2.5)
        if setup.get("decision") == "close-4000":
            self.connection_id = setup.get("connection_id")
            # BUG-6: same accept-then-close rule as the deny branch above.
            self.accept()
            self.close(code=4000)
            return
        self.connection_id = setup.get("connection_id") or uuid.uuid4().hex
        # BUG-8 residual (2026-09-19): a degraded hello (redis down during
        # setup) leaves the socket outside its saloon group — no push until
        # the client reconnects. Remember the join outcome so the next
        # inbound message can late-join (see _try_rejoin).
        # فارسی: اگر اتصالِ گروه در handshake موفق نبود، اولین پیامِ ورودیِ
        # بعدی اتصال را به گروه برمیگرداند.
        self._group_joined = bool(setup.get("joined", False))
        self._last_rejoin_attempt = 0.0
        self.accept()
        self.send_json({
            "type": "telemetry.hello",
            "saloon_id": self.saloon_id,
            "org_id": self.org_id,
            "thresholds": setup.get("thresholds", {}),
        })

    def _post_auth_setup(self, cap, timeout=2.5):
        """Run the whole post-auth redis block under ONE wall-clock bound.

        Pre-checks the cap, registers, re-checks (race guard), joins the
        saloon group and reads the threshold snapshot. Returns a decision
        dict — ``{"decision": "accept", "connection_id", "thresholds"}`` or
        ``{"decision": "close-4000", ...}`` — and NEVER raises: any
        timeout/failure degrades open (cap passes, snapshot {}, join
        best-effort, late join lands whenever redis answers).
        """
        box = {}

        def _target():
            try:
                if ws.active_count(self.user.pk) >= cap:
                    box["result"] = {"decision": "close-4000", "connection_id": None}
                    return
                connection_id = ws.register_connection(self.user.pk)
                if ws.active_count(self.user.pk) > cap:
                    box["result"] = {
                        "decision": "close-4000", "connection_id": connection_id,
                    }
                    return
                try:
                    self._join_once()
                    box["joined"] = True
                except Exception as exc:
                    box["joined"] = False
                    logger.exception(
                        "WS group-join degraded — socket stays up without push: %s",
                        exc,
                    )
                try:
                    bands = ws.ws_redis().hgetall(thresholds_hash(self.saloon_id))
                except Exception:
                    bands = {}
                box["result"] = {
                    "decision": "accept",
                    "connection_id": connection_id,
                    "thresholds": bands,
                }
            except Exception as exc:  # noqa: BLE001 — ANY redis failure degrades open
                logger.exception("WS post-auth setup degraded open: %s", exc)
                box["result"] = {"decision": "accept", "connection_id": None, "thresholds": {}}
            finally:
                box["done"] = True
                # The join outcome lives on the box, but connect() reads the
                # RESULT — copy it over (missing = not joined = rejoin later).
                if "result" in box:
                    box["result"]["joined"] = box.get("joined", False)

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout)
        if not box.get("done"):
            logger.error("WS post-auth setup timed out after %.1fs — degraded open", timeout)
            return {"decision": "accept", "connection_id": None, "thresholds": {}}
        return box.get("result") or {"decision": "accept", "connection_id": None, "thresholds": {}}

    def disconnect(self, code):
        # Clean up the cap membership — but only our own: a pre-auth close
        # (the ①/② verdict paths) never registered anything.
        if getattr(self, "connection_id", None) and getattr(self, "user", None):
            ws.drop_connection(self.user.pk, self.connection_id)
        if getattr(self, "saloon_id", None) is not None and getattr(self, "org_id", None):
            self._leave_saloon_group()

    # ------------------------------------------------------------------
    # Inbound (client → server)
    # ------------------------------------------------------------------

    def _try_rejoin(self):
        """Late group-join for a degraded socket (BUG-8 residual, 2026-09-19).

        Runs on inbound traffic: by the time the client speaks, the layer may
        be back. Joins the saloon group once and regreets with a fresh
        threshold snapshot (best-effort — {} bands when redis still answers
        nothing). Never raises: a still-dead layer only defers to the next
        cooldown window, and the socket stays up either way.
        """
        now = time.monotonic()
        if now - getattr(self, "_last_rejoin_attempt", 0.0) < REJOIN_COOLDOWN_SEC:
            return
        self._last_rejoin_attempt = now
        try:
            self._join_once()
        except Exception:
            logger.debug("WS rejoin deferred — channel layer still down")
            return
        self._group_joined = True
        try:
            thresholds = ws.ws_redis().hgetall(thresholds_hash(self.saloon_id))
        except Exception:
            thresholds = {}
        try:
            self.send_json({
                "type": "telemetry.rejoined",
                "saloon_id": self.saloon_id,
                "org_id": self.org_id,
                "thresholds": thresholds,
            })
        except Exception:
            logger.exception("WS rejoined hello failed")

    def receive_json(self, content=None, **kwargs):
        # EVERY inbound message is a liveness proof: refresh the zombie-guard
        # TTL first, regardless of the payload's validity.
        ws.refresh_connection_ttl(self.user.pk)
        # BUG-8 residual: a degraded socket rejoins its group on next traffic.
        if not getattr(self, "_group_joined", True):
            self._try_rejoin()
        content = content or {}
        if content.get("action") == "gap_fill":
            self._handle_gap_fill(content)
        # Anything else is ignored safely — never an exception (C16 posture).

    def _handle_gap_fill(self, content):
        since = self._parse_since(content.get("since"))
        if since is None:
            self.send_json({
                "type": "telemetry.gap_fill",
                "saloon_id": self.saloon_id,
                "rows": [],
                "error": "since must be an ISO datetime",
            })
            return
        slot_id = content.get("slot_id")
        rows = self._gap_rows(since, slot_id)
        self.send_json(ws.gap_fill_message(self.saloon_id, rows))

    # ------------------------------------------------------------------
    # Outbound handlers — Channels dispatch keys
    # (ws.telemetry_update_message / ws.gap_fill_message set `type`)
    # ------------------------------------------------------------------

    def telemetry_update(self, event):
        # Forward verbatim; the layer already scoped it to our org's group.
        self.send_json(event)

    def telemetry_gap_fill(self, event):  # parity hook for server-side fills
        self.send_json(event)

    def report_flagged(self, event):
        """M7-15 receiver for the §4n FLAGGED push (D24-8 leg 3).

        The producer (apps.reports.api._push_flagged) targets the org-scoped
        `reports_{org_id}` group — same org-in-the-name tenancy boundary as
        the saloon groups (Decision 19). The v0.9 doctor dashboard joins that
        group with its own consumer; this handler exists so the event type
        has a sanctioned receiver TODAY and an accidental membership never
        warns "No handler for message type report.flagged".
        """
        self.send_json(event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _authorize(self):
        """Set self.user/self.org_id, or return the deny code 4003.

        A deactivated membership fails here: the token's org claim must match
        an ACTIVE OrgMember row, so a fired worker's still-fresh JWT cannot
        ride the socket (C-14 spirit — claim semantics, not revocation).
        """
        token = self._query_param(self.scope.get("query_string", b""), "token")
        if not token:
            logger.warning("AUTH-DENY clause=no-token")
            return 4003
        user, claims = self._resolve_token(token)
        if user is None:
            logger.warning("AUTH-DENY clause=bad-token")
            return 4003

        org_claim = claims.get("org_id")
        if not org_claim:
            logger.warning("AUTH-DENY clause=no-org-claim")
            return 4003

        # Resolve + prove the saloon INSIDE the claim's tenant context: RLS
        # makes a foreign saloon invisible — the same 4003 as a missing one.
        # The GUC is TRANSACTION-scoped (set_config true), so — exactly like
        # TenantOrgMiddleware — the context AND the reads live inside ONE
        # atomic block; in autocommit the GUC would die before the query.
        from django.db import transaction
        from apps.tenants.context import tenant_context

        with transaction.atomic(), tenant_context(org_claim):
            saloon_org = (
                Saloon.objects.filter(pk=self.scope["url_route"]["kwargs"]["saloon_id"])
                .values_list("org_id", flat=True)
                .first()
            )
            member = (
                OrgMember._default_manager.filter(user=user, org_id=org_claim, is_active=True)
                .exists()
            )
        # The explicit org comparison is the PORTABLE check (Blueprint §6's
        # own 4003 clause); RLS is the backstop that makes a foreign saloon
        # invisible to the app role. Never rely on the backstop alone.
        if saloon_org is None or str(saloon_org) != str(org_claim) or not member:
            logger.warning(
                "AUTH-DENY clause=saloon-or-member saloon_org=%s member=%s",
                saloon_org, member,
            )
            return 4003

        self.user = user
        self.org_id = str(org_claim)

        # No redis touched here by design (BUG-8): auth verdicts must resolve
        # fast while redis is down — the cap check lives in _post_auth_setup
        # under the wall-clock bound. Failed auth still never consumes a slot.
        return None

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
        """JWT → (user, claims). Signature + expiry verified; anything else
        fails closed to (None, None) — the WS twin of the middleware's rule."""
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

    def _join_once(self):
        """One raw group-join (raises on failure) — the caller
        (_post_auth_setup) bounds it by wall-clock and degrades it."""
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        async_to_sync(get_channel_layer().group_add)(
            ws.group_name(self.org_id, self.saloon_id), self.channel_name
        )

    def _leave_saloon_group(self):
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            async_to_sync(get_channel_layer().group_discard)(
                ws.group_name(self.org_id, self.saloon_id), self.channel_name
            )
        except Exception:
            logger.exception("group_discard failed — membership dies with the socket")

    @staticmethod
    def _parse_since(raw):
        from django.utils.dateparse import parse_datetime

        if not raw:
            return None
        return parse_datetime(str(raw))

    def _gap_rows(self, since, slot_id=None):
        """Committed rows the client missed: THIS saloon, device_ts >= since,
        ordered ascending (the client merge is a stamp walk). slot_id stays
        OPTIONAL. The query runs inside the claim's tenant context — RLS is
        the backstop even though the group name already scopes the socket."""
        from django.db import transaction
        from apps.telemetry.models import SaloonTelemetry
        from apps.tenants.context import tenant_context

        with transaction.atomic(), tenant_context(self.org_id):
            queryset = (
                SaloonTelemetry._default_manager.filter(saloon_id=self.saloon_id)
                .filter(device_ts__gte=since)
                .order_by("device_ts", "server_received_ts")
            )
            if slot_id:
                queryset = queryset.filter(slot_id=slot_id)
            return [
                {
                    "slot_id": row.slot_id,
                    "device_ts": row.device_ts.isoformat(),
                    "server_received_ts": row.server_received_ts.isoformat(),
                    "is_buffered": bool(row.is_buffered),
                    "metrics": row.metrics,
                }
                for row in queryset[:GAP_FILL_MAX_ROWS]
            ]
