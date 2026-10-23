"""Every queue a task is routed to must be consumed by a worker.

`CELERY_TASK_ROUTES` declares where work goes. The worker's `-Q` flag declares
what it reads. Nothing checked that the two agreed, so a route could point at a
queue no worker consumes and the task would sit in it forever — silently.

That is not hypothetical. On 2026-10-23 `provisioning.*` and `auditing.*` were
both routed and neither was consumed. A command issued over the API returned
202 and then stayed PENDING with `attempts=0`; the task was in Redis the whole
time (`LLEN provisioning` = 1). The bind-time config publish only worked
because it calls the publisher synchronously instead of through `.delay()`.

These tests read the compose file, because that is where the worker's queue list
actually lives — a settings-level check would pass while the deployed worker
starved.
"""

import re
from pathlib import Path

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND_DIR.parent / "docker-compose.prod.yml"


def _routes():
    from django.conf import settings

    return settings.CELERY_TASK_ROUTES


def _worker_queues() -> set:
    """The queues the deployed worker consumes, parsed from its `-Q` flag."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    command = compose["services"]["celery-worker"]["command"]
    match = re.search(r"-Q\s+(\S+)", command)
    assert match, f"could not find a -Q flag in the worker command: {command!r}"
    return {queue.strip() for queue in match.group(1).split(",") if queue.strip()}


def test_the_compose_file_is_present():
    """Guard the guard: a moved compose file must fail loudly, not skip."""
    assert COMPOSE.exists(), f"docker-compose.prod.yml not found at {COMPOSE}"


def test_every_routed_queue_is_consumed():
    """THE regression. A route to an unconsumed queue is a task that never runs."""
    consumed = _worker_queues()
    routed = {route["queue"] for route in _routes().values() if route.get("queue")}

    missing = sorted(routed - consumed)
    assert not missing, (
        "CELERY_TASK_ROUTES sends work to queues the worker does not consume: "
        f"{missing}. Every task routed there will sit in Redis forever. "
        f"Worker consumes: {sorted(consumed)}"
    )


def test_the_provisioning_and_audit_queues_specifically():
    """Named explicitly because these two are the ones that were missing, and a
    future edit that drops them should fail with a message that says so."""
    consumed = _worker_queues()
    assert "provisioning" in consumed, "publish_command and the convergence sweep route here"
    assert "audit" in consumed, "auditing.* routes here"


def test_the_publish_command_task_routes_to_provisioning():
    """The command channel's publish step is the task that exposed this."""
    routes = _routes()
    assert routes["provisioning.*"]["queue"] == "provisioning"
