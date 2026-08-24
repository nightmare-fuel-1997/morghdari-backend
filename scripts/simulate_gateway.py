#!/usr/bin/env python
"""
simulate_gateway.py — gateway simulator scaffold (TASKS M1-10, preview of v0.3).

فارسی: اسکلت شبیه‌ساز گیت‌وی — فقط پارامترها و ساختار؛ انتشار واقعی MQTT در v0.3
وصل می‌شود (قاعدهٔ hardening: هیچ منطقی زودتر از نسخه‌اش نمی‌آید).

v0.1 scope: argparse skeleton ONLY. `--help` must work with zero dependencies
beyond the stdlib — paho is imported lazily inside main() so the CLI can be
inspected on any machine, even without the MQTT stack installed.

Real behavior lands in v0.3 (subscriber milestone): connect to local EMQX,
publish a valid gateway payload per the Gateway Firmware Specification to the
farm topic, respecting the interval. Until then this script deliberately does
NOTHING except parse arguments and print what it WOULD do.
"""

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simulate_gateway",
        description=(
            "Morghdari gateway simulator (scaffold). "
            "v0.1: parses arguments only — actual MQTT publishing arrives in v0.3."
        ),
    )
    parser.add_argument(
        "--gateway",
        required=True,
        help="Gateway slot id to simulate (e.g., FARM-01-GW-A)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between payload publishes (default: 60)",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="MQTT broker host (default: localhost, dev EMQX from docker-compose.dev.yml)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883 plain — TLS 8883 arrives with certs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Lazy import kept OUT of module scope so --help works dependency-free.
    try:
        import paho.mqtt.client as mqtt  # noqa: F401

        has_paho = True
    except ImportError:
        has_paho = False

    print(
        f"[scaffold] would simulate gateway {args.gateway!r} against "
        f"{args.host}:{args.port} every {args.interval:g}s"
    )
    if not has_paho:
        print("[scaffold] paho-mqtt not installed; publishing unavailable anyway")
    print("[scaffold] NOT publishing — real simulation is a v0.3 deliverable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
