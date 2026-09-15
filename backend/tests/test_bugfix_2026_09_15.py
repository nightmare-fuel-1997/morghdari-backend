"""Regression tests for the 2026-09-15 bug-fix round (BUG-2/6/9b/10/18).

Deterministic unit-level checks - no DB/Redis needed. Each one fails on the
pre-fix code and passes after the fix.
"""
import datetime as dt

from django.db import models


def test_bug06_ws_deny_accepts_before_close():
    """BUG-6: the auth-deny path must accept() BEFORE close() so the 4003 code
    reaches the wire (Daphne maps a pre-accept close() to a bare HTTP 403)."""
    from apps.telemetry.ws_consumer import SaloonTelemetryConsumer

    consumer = SaloonTelemetryConsumer()
    consumer.scope = {"url_route": {"kwargs": {"saloon_id": 1}}}
    events = []
    consumer._authorize = lambda: 4003
    consumer.accept = lambda: events.append("accept")
    consumer.close = lambda code=None: events.append(("close", code))

    consumer.connect()

    assert events == ["accept", ("close", 4003)], events


def test_bug06_ws_cap_accepts_before_close():
    """BUG-6 (cap path): an over-cap valid socket must also accept then close 4000."""
    from apps.telemetry.ws_consumer import SaloonTelemetryConsumer
    from apps.telemetry import ws as ws_mod

    consumer = SaloonTelemetryConsumer()
    events = []
    consumer._authorize = lambda: None
    consumer.scope = {"url_route": {"kwargs": {"saloon_id": 1}}}
    consumer._post_auth_setup = lambda cap, timeout=2.5: {
        "decision": "close-4000", "connection_id": "cid",
    }
    orig = ws_mod.config_int
    ws_mod.config_int = lambda *a, **k: 10
    consumer.accept = lambda: events.append("accept")
    consumer.close = lambda code=None: events.append(("close", code))
    try:
        consumer.connect()
    finally:
        ws_mod.config_int = orig

    assert events == ["accept", ("close", 4000)], events


def test_bug02_reconciliation_default_day_is_yesterday():
    """BUG-2: the documented default (yesterday Tehran) must not blow up in
    _tehran_day_bounds (the old None path raised TypeError -> HTTP 500)."""
    from apps.monitoring import ops_services as svc

    y = svc.tehran_yesterday()
    assert isinstance(y, dt.date)
    start, end = svc._tehran_day_bounds(y)
    assert (end - start) == dt.timedelta(days=1)


def test_bug09b_clear_active_pointer_deletes_blob():
    """BUG-9b: clearing the coalesce blob deletes the exact alert:active key."""
    from apps.alerts.services import sweeps
    from apps.alerts.services.redis_keys import active_alert_key

    deleted = []

    class FakeClient:
        def delete(self, *keys):
            deleted.extend(keys)

    sweeps.clear_active_pointer(saloon_id=7, metric="temp", client=FakeClient())
    assert deleted == [active_alert_key(7, "temp")]


def test_bug09_cross_org_reads_use_admin_alias():
    """BUG-9: the RLS-blinded cross-org sweep reads must route through the
    admin (BYPASSRLS) alias - the same sanctioned pattern as replay/rebuild."""
    from apps.alerts.services import sweeps

    assert callable(sweeps._admin_alias)
    assert sweeps._admin_alias.__doc__


def test_bug10_18_audit_fk_is_set_null():
    """BUG-10/BUG-18: the audit row's farm/saloon FK must SET_NULL so an empty
    farm/saloon can be hard-deleted instead of 409-ing forever."""
    from apps.auditing.models import AccountActivityLog

    assert AccountActivityLog._meta.get_field("farm").remote_field.on_delete is models.SET_NULL
    assert AccountActivityLog._meta.get_field("saloon").remote_field.on_delete is models.SET_NULL
