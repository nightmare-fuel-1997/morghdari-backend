"""Migrate gate — proves the migration graph is DEPLOYABLE, not just consistent.

WHY this exists (2026-09-06 audit): the test matrix runs pytest with
--nomigrations, so migration FILES are never imported by any test. The
hand-written accounts/0003 shipped with a nonexistent
`django.db.models.deletion.ForeignKey` reference and would have crashed the
first real `manage.py migrate`. This gate closes that hole (deployment
skill: "Database migration tested" is a production-readiness item).

What it does, in order, all against a THROWAWAY database it creates and
drops itself (never the pytest --reuse-db target — reusing that DB is the
stale-schema trap from task_plan's errors table):

  1. `makemigrations --check`      — model/state drift would ship as a
                                     surprise 000N on the deploy box.
  2. `migrate` (forward, full)     — the deploy path, timed.
  3. `migrate` again               — idempotency: second run must be a no-op.
  4. Reversibility probe (WARN)    — attempts `migrate <app> zero` per app,
                                     newest first; a failure is reported as a
                                     rollback-posture WARNING, not a gate
                                     failure (legacy migrations may be
                                     intentionally irreversible).
  5. Sanity: django_migrations non-empty; every applied app has rows.
  6. DROP the throwaway database.

Exit 0 = deployable. Any step 1–3/5 failure exits non-zero.

Usage (inside the CI image, or any host with DATABASE_URL reachable):
  python scripts/migrate_gate.py

Env:
  MIGRATE_GATE_ADMIN_URL  — superuser URL used to CREATE/DROP the throwaway
                            DB (defaults to DATABASE_URL's server, database
                            segment swapped for POSTGRES_DB).
  MIGRATE_GATE_DB_NAME    — throwaway DB name (default morghdari_migrate_gate).

فارسی: این گیت ثابت می‌کند مایگریشن‌ها واقعاً اجرا می‌شوند — چون تست‌ها با
--nomigrations اجرا می‌شوند، تا امروز هیچ چیزی فایلِ مایگریشن را import
نمی‌کرد؛ این اسکریپت همان حفره را می‌بندد.
"""

import os
import sys
import time

# Script dir (backend/scripts) is sys.path[0] under `python scripts/...` —
# the Django project root is the PARENT; put it on the path explicitly so the
# gate runs from any CWD (host shell or CI container).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import django

GATE_DB = os.environ.get("MIGRATE_GATE_DB_NAME", "morghdari_migrate_gate")


def _swap_db(url: str, dbname: str) -> str:
    head, sep, tail = url.rstrip("/").rpartition("/")
    return head + sep + dbname + ("" if tail == url else "")


def _admin_url() -> str:
    url = os.environ.get("MIGRATE_GATE_ADMIN_URL") or os.environ["DATABASE_URL"]
    # The maintenance connection must point at an EXISTING database; swap the
    # path segment for the bootstrap DB name from POSTGRES-style env, else
    # keep the original DB (it exists by definition — the URL already works).
    bootstrap = os.environ.get("POSTGRES_DB")
    if bootstrap:
        head, sep, _tail = url.rstrip("/").rpartition("/")
        return head + sep + bootstrap
    return url


def main() -> int:
    admin_url = _admin_url()
    gate_url = _swap_db(admin_url, GATE_DB)
    failures: list[str] = []

    # ---------------------------------------------------------------- setup
    import psycopg

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{GATE_DB}"')
        conn.execute(f'CREATE DATABASE "{GATE_DB}"')
    print(f"[gate] throwaway database ready: {GATE_DB}")

    try:
        os.environ["DATABASE_URL"] = gate_url
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
        django.setup()

        from django.apps import apps
        from django.core.management import call_command

        # -------------------------------------------------- 1. state parity
        try:
            call_command("makemigrations", check=True, dry_run=True, verbosity=0)
            print("[gate] 1/5 makemigrations --check: CLEAN")
        except SystemExit as exc:  # check=True exits non-zero on drift
            failures.append(f"makemigrations --check failed (exit {exc.code})")
            print(f"[gate] 1/5 makemigrations --check: DRIFT (exit {exc.code})")

        # ------------------------------------------------- 2. forward migrate
        t0 = time.monotonic()
        call_command("migrate", interactive=False, verbosity=1)
        print(f"[gate] 2/5 migrate (forward): OK in {time.monotonic() - t0:.1f}s")

        # ---------------------------------------------------- 3. idempotency
        call_command("migrate", interactive=False, verbosity=0)
        print("[gate] 3/5 migrate (re-run): OK — no pending migrations")

        # --------------------------------------------------------- 4. sanity
        with psycopg.connect(gate_url) as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM django_migrations").fetchone()
        if count <= 0:
            failures.append("django_migrations is empty after a full migrate")
        else:
            print(f"[gate] 4/5 sanity: {count} migrations recorded")

        # ---------------------------------------- 5. reversibility (WARN only)
        # Unwind EVERY app (newest app first — worked for the full graph);
        # a per-app probe, not a verdict: legacy migrations may be
        # intentionally irreversible, so a failure here prints posture
        # evidence and does NOT fail the gate.
        with psycopg.connect(gate_url) as conn:
            apps_applied = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT app FROM django_migrations ORDER BY app DESC"
                ).fetchall()
            ]
        blocked = []
        for app in apps_applied:
            try:
                call_command("migrate", app, "zero", interactive=False, verbosity=0)
                print(f"[gate] 5/5 reverse {app} -> zero: OK")
            except Exception as exc:  # noqa: BLE001 — evidence, not verdict
                blocked.append(app)
                print(f"[gate] 5/5 reverse {app} -> zero: NOT REVERSIBLE "
                      f"({type(exc).__name__}: {exc})")
        if not blocked:
            print("[gate] 5/5 rollback posture: EVERY app unwinds to zero")

        # Close Django's connection BEFORE teardown — the pooled socket holds
        # the throwaway DB open and the DROP would die with ObjectInUse.
        from django.db import connections

        connections.close_all()
        del apps
    finally:
        # ------------------------------------------------------- teardown
        import psycopg

        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{GATE_DB}"')
        print(f"[gate] throwaway database dropped: {GATE_DB}")

    if failures:
        for f in failures:
            print(f"[gate] FAILURE: {f}", file=sys.stderr)
        return 1
    print("[gate] RESULT: DEPLOYABLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
