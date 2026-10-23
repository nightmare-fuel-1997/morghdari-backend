#!/usr/bin/env python3
"""sim.py — the multi-gateway simulator: a fleet of digital twins.

WHAT CHANGED FROM THE PREVIOUS SIMULATOR
----------------------------------------
The old `sim.py` was a telemetry generator: it read credentials, connected, and
published schema-2.0 readings forever. It never subscribed, never applied a
configuration, never answered a command, and never published status — so it
could not exercise the parts of the system that matter most (registration,
config apply, the command channel, LOS detection), and it could not fail in the
ways real hardware fails.

This one runs a `DigitalTwin` per gateway (`simulator/twin.py`). Each twin
behaves as `docs/hardware-firmware-requirements-v1.md` specifies: it connects
with `username = unit_id`, subscribes to its own `desired` and `command`
topics, applies the retained configuration and reports `APPLIED`, publishes
schema-2.0 telemetry and status with a Last Will, and answers commands in three
separate events. Once a twin is bound it moves to the tenant topics, exactly
as a real device does.

The backend cannot tell a twin from hardware. That is the whole point: whatever
breaks, breaks here rather than on a real gateway in a real shed.

CREDENTIALS
-----------
Read from `/sim/creds.json`, bind-mounted from the host's git-ignored
`.sim.env`. Shape (unchanged from the previous simulator, so existing
deployments keep working):

    {"GW-SIM-01": {"username": "...", "password": "...", "farm_id": 2}, ...}

FAULT INJECTION (all optional, default off)
-------------------------------------------
    SIM_FAULT_DROP=0.05      skip 5% of telemetry windows (connectivity)
    SIM_FAULT_DELAY=0.05     delay 5% of sends by SIM_FAULT_DELAY_SEC
    SIM_FAULT_INVALID=0.02   emit 2% of readings out of physical range
    SIM_FAULT_RESTART=0.01   power-cycle 1% of devices per interval

Farsi: شبیه‌سازِ ناوگان — هر گیت‌وی یک دوقلؤ دیجیتالی که قرارداد سخت‌افزار را
زنده می‌کند.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from twin import DigitalTwin, FaultProfile  # noqa: E402

LOG = logging.getLogger("sim")

CREDS_PATH = os.environ.get("SIM_CREDS_PATH", "/creds.json")
# Gateways created by provision.py. The fleet merges this in and reloads it, so
# a newly installed gateway starts reporting without restarting the simulator.
PROVISIONED_PATH = os.environ.get(
    "SIM_PROVISIONED_PATH", "/sim-state/provisioned.json")
RELOAD_EVERY_SEC = float(os.environ.get("SIM_RELOAD_SEC", "30"))
SUMMARY_EVERY_SEC = float(os.environ.get("SIM_SUMMARY_SEC", "300"))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fleet:
    """Owns every twin, keeps them alive, and reports what each one is doing."""

    def __init__(self, host: str, port: int, interval: float,
                 faults: FaultProfile):
        self.host = host
        self.port = port
        self.interval = interval
        self.faults = faults
        self.twins: list[DigitalTwin] = []
        self._stopping = False
        self._last_summary = 0.0
        self._last_reload = 0.0

    # ------------------------------------------------------------------ load
    def load_credentials(self, *paths) -> int:
        """Build one twin per credential entry. Returns how many were built.

        Several files may be given; later ones win on a duplicate slot, so the
        provisioned file can override the base one.
        """
        merged = {}
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    merged.update(json.load(handle))
            except FileNotFoundError:
                LOG.info("no credentials at %s — skipped", path)
            except (ValueError, OSError) as exc:
                LOG.error("%s is not usable (%s) — skipped", path, exc)

        for slot, entry in merged.items():
            password = entry.get("password") or ""
            if not password:
                LOG.error("%s has no password — skipped", slot)
                continue
            self.twins.append(self._make_twin(slot, entry))
        return len(self.twins)

    def _make_twin(self, slot: str, entry: dict) -> DigitalTwin:
        return DigitalTwin(
            unit_id=slot,
            password=entry["password"],
            farm_id=entry.get("farm_id"),
            hardware_id=entry.get("hardware_id"),
            packs=tuple(entry.get("packs") or ("P-A", "P-B")),
            host=self.host,
            port=self.port,
            interval_sec=self.interval,
            faults=self.faults,
        )

    def reload_provisioned(self) -> int:
        """Pick up gateways provision.py has installed since we started.

        A new gateway is appended; an existing one is left alone, because its
        twin is already running with its own state and restarting a healthy
        device to refresh a credential would be worse than the staleness.
        """
        try:
            with open(PROVISIONED_PATH, encoding="utf-8") as handle:
                fresh = json.load(handle)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError):
            return 0
        known = {t.unit_id for t in self.twins}
        added = 0
        for slot, entry in fresh.items():
            if slot in known or not entry.get("password"):
                continue
            self.twins.append(self._make_twin(slot, entry))
            self._connect_with_retry(self.twins[-1])
            added += 1
            LOG.info("joined the fleet: %s (farm %s)", slot, entry.get("farm_id"))
        return added

    # ----------------------------------------------------------------- start
    def start_all(self) -> None:
        """Connect every twin. A refused connection is logged CRITICAL and
        retried forever — that is what a real device does, and a silent retry
        loop is exactly how a broken deployment looks healthy."""
        for twin in self.twins:
            self._connect_with_retry(twin)

    def _connect_with_retry(self, twin: DigitalTwin,
                            attempts: int = 3, backoff: float = 5.0) -> bool:
        for attempt in range(1, attempts + 1):
            if self._stopping:
                return False
            try:
                rc = twin.start()
            except Exception as exc:  # noqa: BLE001 — keep the fleet alive
                rc = -1
                twin.last_error = f"{type(exc).__name__}: {exc}"
            if rc == 0:
                LOG.info("connected %s (farm=%s, applied_v=%d)",
                         twin.unit_id, twin.farm_id, twin.applied_version)
                return True
            LOG.warning("%s connect rc=%s (attempt %d/%d) %s",
                        twin.unit_id, rc, attempt, attempts,
                        twin.last_error or "")
            if attempt < attempts:
                time.sleep(backoff * attempt)
        LOG.critical("%s could not connect after %d attempts — will retry "
                     "on the next pass", twin.unit_id, attempts)
        return False

    # ------------------------------------------------------------------- loop
    def run_forever(self) -> None:
        LOG.info("fleet up: %d twin(s), interval=%gs, faults=[%s]",
                 len(self.twins), self.interval, self.faults.describe())
        while not self._stopping:
            now = time.time()
            for twin in self.twins:
                if self._stopping:
                    break
                self._keep_alive(twin)
                if twin.connected and twin.org_id:
                    if now - twin.last_tick_at >= twin.report_interval_sec:
                        twin.last_tick_at = now
                        twin.tick()
            if now - self._last_summary >= SUMMARY_EVERY_SEC:
                self._last_summary = now
                self._log_summary()
            if now - self._last_reload >= RELOAD_EVERY_SEC:
                self._last_reload = now
                self.reload_provisioned()
            time.sleep(1.0)
        self.stop_all()

    def _keep_alive(self, twin: DigitalTwin) -> None:
        """Reconnect a twin that lost its session.

        paho's own loop retries the socket, but a device whose credentials were
        rotated, or that was never able to connect at all, needs the fleet to
        try again — otherwise one bad unit silently stops reporting forever.
        """
        if twin.connected:
            return
        now = time.time()
        if now - twin.last_reconnect_at < 30.0:
            return
        twin.last_reconnect_at = now
        LOG.info("%s is offline — reconnecting", twin.unit_id)
        try:
            twin.stop()
        except Exception:  # noqa: BLE001
            pass
        self._connect_with_retry(twin, attempts=1)

    # ---------------------------------------------------------------- summary
    def _log_summary(self) -> None:
        LOG.info("---- fleet summary %s ----", _utcnow())
        for twin in self.twins:
            LOG.info("  %s", twin.summary())
            for kind, detail in twin.faults_hit[-3:]:
                LOG.info("      fault %s: %s", kind, detail)

    def stop_all(self) -> None:
        LOG.info("stopping fleet")
        for twin in self.twins:
            try:
                twin.stop()
            except Exception:  # noqa: BLE001
                pass

    def request_stop(self, *_args) -> None:
        self._stopping = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("SIM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = os.environ.get("MQTT_HOST", "emqx")
    port = int(os.environ.get("MQTT_PORT", "8883"))
    interval = float(os.environ.get("SIM_INTERVAL_SEC", "60"))
    faults = FaultProfile.from_env()

    fleet = Fleet(host, port, interval, faults)
    try:
        count = fleet.load_credentials(CREDS_PATH, PROVISIONED_PATH)
    except FileNotFoundError:
        LOG.critical("no credentials at %s — nothing to simulate", CREDS_PATH)
        return 1
    except (ValueError, KeyError) as exc:
        LOG.critical("%s is not a usable credentials file: %s", CREDS_PATH, exc)
        return 1
    if not count:
        LOG.critical("no usable credentials in %s", CREDS_PATH)
        return 1

    signal.signal(signal.SIGTERM, fleet.request_stop)
    signal.signal(signal.SIGINT, fleet.request_stop)

    fleet.start_all()
    fleet.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
