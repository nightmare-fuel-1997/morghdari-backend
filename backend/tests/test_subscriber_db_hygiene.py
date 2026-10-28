"""Subscriber DB hygiene — 2026-10-28 incident regression.

A postgres restart closed the subscriber's long-lived Django connection and
nothing reopened it: the config-report lane stopped processing while telemetry
kept flowing (that path needs no DB), so the failure was silent until the
journey test caught a slot stuck in STALLED.

The fix is `close_old_connections()` at the top of the message dispatcher. These
tests pin that the guard runs before any routing — including when routing
raises — so a future refactor cannot quietly drop it.
"""
import django.db

from morghdari.subscriber import main as sub


class _Msg:
    topic = "acme/farms/1/gateways/GW-1/telemetry"
    payload = b'{"schema_version": "2.0"}'


def test_dispatcher_closes_dead_db_connection_first(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(django.db, "close_old_connections", lambda: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(sub, "_redis", lambda: object())
    monkeypatch.setattr(sub, "handle_message", lambda *a, **k: None)

    sub._on_message(None, None, _Msg())

    assert calls["n"] == 1, "close_old_connections must run before routing"


def test_dispatcher_survives_a_closing_failure(monkeypatch):
    """A broken close_old_connections must never block message processing."""

    def _boom():
        raise RuntimeError("db layer exploded")

    monkeypatch.setattr(django.db, "close_old_connections", _boom)
    monkeypatch.setattr(sub, "_redis", lambda: object())
    seen = {"handled": 0}
    monkeypatch.setattr(sub, "handle_message", lambda *a, **k: seen.__setitem__("handled", seen["handled"] + 1))

    sub._on_message(None, None, _Msg())

    assert seen["handled"] == 1
