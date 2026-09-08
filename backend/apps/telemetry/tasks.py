"""Telemetry Celery tasks — v0.7 M7-06/M7-07.

The drain tasks are DEFINED in consumers.py (they share the drain machinery
and the RLS-scoped helpers), but Celery's autodiscover only imports each
app's ``tasks`` module — without this re-export the worker registered NO
telemetry task and every beat kick died with
``KeyError: 'telemetry.process_realtime_payload'`` while the streams piled
up silently (laptop rehearsal, 2026-09-08).

M7-07 adds the per-gateway pending-drain task the support-op
``POST /platform/support/tasks/drain/`` dispatches (task=drain_pending_approval)
— the same machinery the FM approval path runs inline, wrapped as a task so
the support surface never blocks on Redis I/O.

Keep this module a thin surface: the canonical implementations stay in
consumers.py next to the drain code they drive.
"""

from celery import shared_task

from .consumers import process_historical_payload, process_realtime_payload  # noqa: F401


@shared_task(name="telemetry.drain_pending_for_gateway")
def drain_pending_for_gateway(gateway_slot: str) -> int:
    """Drain ONE gateway's held (PENDING_APPROVAL) telemetry FIFO.

    Worker-side twin of provisioning.services.approve_gateway's inline drain:
    identical machinery, dispatchable without a state transition (support
    ops may re-drain after a partial approval-time failure).
    """
    from morghdari.subscriber.pipeline import STREAM_PENDING

    from .consumers import drain_stream

    return drain_stream(STREAM_PENDING, count=1000, gateway_slot=gateway_slot)
