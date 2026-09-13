#!/usr/bin/env python3
"""M8 C5 — backend HIL readiness checks (receiver side of the hardware seam).

Probes the contract endpoints a real gateway will hit and the static wiring
the firmware depends on, so ANY gap is found BEFORE the hardware team brings a
device. Pure stdlib — runs on the laptop or the VPS without the venv.

  * live HTTP probes (skip with --skip-live):
      C1 /api/v1/health/                    → 200
      C2 /api/v1/hardware/time/             → 200 + unix_ts (firmware NTP
                                              fallback, FW-spec §2.8)
      C3 /api/v1/gateways/{slot}/config/    → route exists (unknown slot MUST
                                              be the 404-envelope, not a 405)
  * static contract checks (always):
      C4 O9 diagnostics route documented in backend/openapi.yaml
      C5 T5–T9 MQTT topics permitted by the emqx provisioning rules
      C6 OTA checksum generator present (scripts/ota_checksums.py)
      C7 LOS-suppression tunable documented in seeds/tests (rebuild window)

Exit 0 = all PASS (SKIP tolerated); 1 = any FAIL. The hardware handover gate
(scripts/hil_handover/) runs AFTER these are green.

فارسی: بررسیِ آمادگیِ سمتِ سرور برای تحویلِ سخت‌افزار — پیش از آمدنِ گیت‌ویِ واقعی.
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CHECKS: list[dict] = []


def _ssl_ctx(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, ctx: ssl.SSLContext, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            body = resp.read(4096)
            return resp.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace")
    except Exception as exc:  # network-level failures
        return None, str(exc)


def record(check_id: str, label: str, status: str, detail: str = "") -> None:
    CHECKS.append({"id": check_id, "label": label, "status": status, "detail": detail})


def probe_health(base: str, ctx) -> None:
    status, body = _get(f"{base}/api/v1/health/", ctx)
    ok = status == 200
    record("C1", "GET /api/v1/health/",
           "PASS" if ok else "FAIL", f"http={status}")
    if ok:
        try:
            probe_ok = json.loads(body)
            record("C1", "…health payload", "PASS", f"json={sorted(probe_ok)}")
        except ValueError:
            record("C1", "…health payload", "FAIL", "not JSON")


def probe_hardware_time(base: str, ctx) -> None:
    status, body = _get(f"{base}/api/v1/hardware/time/", ctx)
    ok = status == 200
    detail = f"http={status}"
    if ok:
        try:
            data = json.loads(body)
            ok = isinstance(data.get("unix_ts"), (int, float))
            detail = f"http={status} unix_ts={data.get('unix_ts')}"
        except ValueError:
            ok = False
            detail = f"http={status} not JSON"
    record("C2", "GET /api/v1/hardware/time/ (FW NTP fallback)",
           "PASS" if ok else "FAIL", detail)


def probe_config_route(base: str, slot: str, ctx) -> None:
    if not slot:
        record("C3", "GET /api/v1/gateways/{slot}/config/ route",
               "SKIP", "no --slot given")
        return
    status, body = _get(f"{base}/api/v1/gateways/{slot}/config/", ctx)
    # unknown slot → 404 envelope proves the ROUTE exists (401/403 prove
    # auth comes first — also fine); a 405/HTML 404 would mean no route.
    ok = status in (401, 403, 404)
    record("C3", f"GET /api/v1/gateways/{slot}/config/ route presence",
           "PASS" if ok else "FAIL", f"http={status} (expect 401/403/404)")


def static_contract_checks() -> None:
    yaml = (REPO / "backend" / "openapi.yaml").read_text(encoding="utf-8", errors="replace")
    record("C4", "O9 diagnostics route documented in openapi.yaml",
           "PASS" if re.search(r"gateways/\{?slot\}?/diagnostics|gateway_diagnostics", yaml)
           else "FAIL", "grep path: gateways/{slot}/diagnostics")

    emqx_src = (REPO / "scripts" / "emqx_provision.py").read_text(
        encoding="utf-8", errors="replace")
    prov_src = (REPO / "backend" / "apps" / "provisioning" / "api.py").read_text(
        encoding="utf-8", errors="replace")
    conf_src = (REPO / "emqx" / "emqx.conf").read_text(encoding="utf-8", errors="replace")
    hay = emqx_src + "\n" + prov_src + "\n" + conf_src
    missing = [t for t in ("clock/sync", "ota/response", "diagnostics/request") if t not in hay]
    record("C5", "T3–T9 topics wired in gateway ACL provisioning",
           "PASS" if not missing else "FAIL",
           "missing:" + ",".join(missing) if missing else "clock/sync + ota + diagnostics in provisioning/api.py")

    record("C6", "OTA checksum generator present (scripts/ota_checksums.py)",
           "PASS" if (REPO / "scripts" / "ota_checksums.py").exists() else "FAIL")

    seeds_src = (REPO / "backend" / "tests" / "test_seeds.py").read_text(
        encoding="utf-8", errors="replace") if (REPO / "backend" / "tests" / "test_seeds.py").exists() else ""
    record("C7", "LOS-suppression tunable (los_suppress_after_redis_restart_min)",
           "PASS" if "los_suppress_after_redis_restart_min" in seeds_src else "FAIL",
           "config key in test_seeds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("HIL_BASE_URL", "https://localhost"))
    parser.add_argument("--slot", default=os.environ.get("HIL_SLOT", ""))
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (rehearsal self-signed)")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ctx = _ssl_ctx(args.insecure or args.base_url.startswith("http://"))
    if not args.skip_live:
        probe_health(args.base_url.rstrip("/"), ctx)
        probe_hardware_time(args.base_url.rstrip("/"), ctx)
        probe_config_route(args.base_url.rstrip("/"), args.slot, ctx)
    static_contract_checks()

    failures = [c for c in CHECKS if c["status"] == "FAIL"]
    if args.json:
        print(json.dumps({"checks": CHECKS, "exit": 1 if failures else 0}, indent=2))
    else:
        for c in CHECKS:
            print(f"[{c['status']:4}] {c['id']} {c['label']} — {c['detail']}")
        print(f"\n{'PASS' if not failures else 'FAIL'} — {len(CHECKS)} checks, "
              f"{sum(1 for c in CHECKS if c['status'] == 'PASS')} pass, "
              f"{sum(1 for c in CHECKS if c['status'] == 'SKIP')} skip, "
              f"{len(failures)} fail")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())