"""The subscriber must initialise the ORM itself, not inherit it by accident.

`_django_ready()` used to be reachable only from `_slot_db_fetch`, i.e. on a
pack-resolution CACHE MISS. In steady state the resolver cache is warm (24 h
TTL, held in Redis, so it survives a restart), which means a freshly restarted
subscriber could serve telemetry for hours without ever initialising Django.

The first command response then died inside `reconcile_command_response` with:

    django.core.exceptions.AppRegistryNotReady: Apps aren't loaded yet.

...and the command stayed at SENT forever, because the handler swallows
exceptions and only logs them. Found 2026-10-23 by issuing a real command to a
live twin: the device answered (`cmds=4`), the broker delivered, and the backend
never recorded a thing.

The reported lane carried the same latent bug — it was safe only because
telemetry happened to warm Django first.
"""

import json
import sys

import pytest


def _subscriber():
    return sys.modules["morghdari.subscriber.main"]


def test_main_initialises_the_orm_before_connecting(monkeypatch):
    """THE regression. Startup must bring the ORM up explicitly."""
    from morghdari.subscriber import main as sub

    calls = []

    def fake_ready():
        calls.append("django")
        return True

    class Boom(Exception):
        pass

    def fake_build_client():
        # Prove the order: the ORM is up BEFORE the client exists.
        assert calls == ["django"], "the ORM must be initialised before build_client()"
        raise Boom("stop here")

    monkeypatch.setattr(sub, "_django_ready", fake_ready)
    monkeypatch.setattr(sub, "build_client", fake_build_client)

    with pytest.raises(Boom):
        sub.main()
    assert calls == ["django"]


def test_the_command_lane_drops_cleanly_when_the_orm_is_unavailable(monkeypatch, caplog):
    """No AppRegistryNotReady traceback: a clear message and a dropped message."""
    import logging

    from morghdari.subscriber import main as sub

    monkeypatch.setattr(sub, "_django_ready", lambda: False)
    with caplog.at_level(logging.ERROR, logger="morghdari.subscriber"):
        # Must not raise.
        sub._handle_command_response(
            "devices/GW-X/command/response",
            json.dumps({"command_uuid": "abc", "state": "ACK"}).encode(),
            "GW-X",
        )
    assert "ORM is unavailable" in caplog.text


def test_the_reported_lane_drops_cleanly_when_the_orm_is_unavailable(monkeypatch, caplog):
    import logging

    from morghdari.subscriber import main as sub

    monkeypatch.setattr(sub, "_django_ready", lambda: False)
    with caplog.at_level(logging.ERROR, logger="morghdari.subscriber"):
        sub._handle_reported_device(
            "devices/GW-X/reported",
            json.dumps({"slot_id": "GW-X", "state": "APPLIED"}).encode(),
            "GW-X",
        )
    assert "ORM is unavailable" in caplog.text


def test_the_command_lane_reaches_the_reconciler_when_the_orm_is_up(monkeypatch):
    """When the ORM is available the handler must actually call the reconciler —
    the guard must not become a silent early-return."""
    from morghdari.subscriber import main as sub

    seen = []
    monkeypatch.setattr(sub, "_django_ready", lambda: True)
    monkeypatch.setattr(
        "apps.provisioning.tasks.reconcile_command_response",
        lambda body, topic_slot_id=None: seen.append((body, topic_slot_id)),
    )
    sub._handle_command_response(
        "devices/GW-X/command/response",
        json.dumps({"command_uuid": "abc", "state": "ACK"}).encode(),
        "GW-X",
    )
    assert seen == [({"command_uuid": "abc", "state": "ACK"}, "GW-X")]
