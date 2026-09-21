"""CI readiness probe: block until DATABASE_URL accepts connections.

`docker compose run` starts `depends_on` services but does NOT wait for their
healthchecks, so gates raced a still-booting postgres (connection refused).
Rather than editing run_acceptance.sh's ritual, each gate service waits here
first. Pure stdlib + psycopg (a base requirement, always importable).
"""
import os
import sys
import time

try:
    import psycopg
except ImportError:  # pragma: no cover
    print("ci_wait_db: psycopg missing, skipping wait", flush=True)
    sys.exit(0)

url = os.environ.get("DATABASE_URL", "")
deadline = time.time() + int(os.environ.get("CI_WAIT_DB_TIMEOUT", "180"))
while time.time() < deadline:
    try:
        psycopg.connect(url, connect_timeout=3).close()
        print("ci_wait_db: database accepting connections", flush=True)
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001 - any connect failure means wait
        print(f"ci_wait_db: waiting ({exc})", flush=True)
        time.sleep(3)
print("ci_wait_db: database never came up", flush=True)
sys.exit(1)
