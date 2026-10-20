#!/usr/bin/env python
"""First-device rehearsal: the complete install loop on the PRODUCTION broker.

WHY this is a script and not a pytest module.

Two facts had only ever been proven separately:

  * the full install loop (register -> request -> approve -> assign -> bind ->
    device applies -> submit -> PROVISIONED) ran against the CI broker, whose
    authn is EMQX's own built-in database; and
  * the production broker profile (Postgres authn, `emqx/emqx.conf` verbatim)
    was only ever asked "will you let this unit connect".

A first real device does both at once, and that combination was untested.

It cannot be a pytest module, because pytest isolates the test database: the
rows a test writes land in `test_<name>` while the broker queries the real
database, so every device is refused with "not authorised" for a reason that has
nothing to do with the code. Forcing the two together means fighting pytest's
create/flush/teardown machinery against a schema owned by real migrations (the
unmanaged `daily_reports` table alone breaks the flush).

So this runs the SAME flow function the live test uses — `_run_full_installation_loop`
from `tests/test_m10_03_bootstrap_lane_live.py`, imported rather than copied —
outside pytest, against the real database, committing as it goes because the
broker is a separate process that must see the rows.

Run it only against a disposable rehearsal database:

    docker compose -f docker-compose.broker-prod.yml -p morghdari_bp run --rm e2e-full
"""

import os
import sys

# `python /scripts/x.py` puts /scripts on sys.path, NOT the working directory —
# so the Django project (`config`, `apps`, `tests`) would be invisible. Add both
# the script's directory (the mounted device implementation) and the app root.
for candidate in (
    "/scripts",
    os.path.join(os.path.dirname(os.path.abspath(__file__))),
    "/app",
    os.getcwd(),
):
    if candidate and os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

    import django

    django.setup()

    from django.contrib.auth import get_user_model

    from tests.test_m10_03_bootstrap_lane_live import (
        BROKER_AUTHN,
        _run_full_installation_loop,
    )

    if BROKER_AUTHN != "postgres":
        print(
            "REFUSING: MQTT_BROKER_AUTHN must be 'postgres' — this script exists "
            "to rehearse the production broker profile. The CI-broker equivalent "
            "is the e2e-bootstrap service.",
            file=sys.stderr,
        )
        return 2

    print("=" * 72)
    print("FIRST-DEVICE REHEARSAL — production broker profile")
    print("=" * 72)
    _run_full_installation_loop(get_user_model())
    print("=" * 72)
    print("PASSED: a device registered at the factory was installed, configured,")
    print("        sensed, submitted and activated with no human approval —")
    print("        authenticated by Postgres the whole way.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
