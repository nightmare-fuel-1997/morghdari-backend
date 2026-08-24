# CONSTRAINTS — real-world constraints discovered during the build

> EN: The purpose of building version-by-version is to surface REAL constraints early.
> Every friction point found while building lands here with its workaround/decision.
>
> فارسی: هدف ساخت نسخه‌به‌نسخه، بیرون کشیدن محدودیت‌های واقعی است؛ هر مانعی که در
> ساخت پیدا شود اینجا ثبت می‌شود تا در جمع‌بندی نسخه بررسی گردد.

| # | Date | Constraint | Impact | Decision / Workaround |
|---|---|---|---|---|
| C-01 | 2026-08-24 | Default `python` on this machine is 3.11; Blueprint pins 3.12 (installed but not default) | venvs would silently build on 3.11 | Always create venvs with `py -V:3.12`; CI image is 3.12 anyway |
| C-02 | 2026-08-24 | Docker Desktop installed but daemon NOT auto-running after boot | CI/dev gates impossible until started | Start Docker Desktop manually before working (`docker info` to check); ~5 s to become ready on this machine |
| C-03 | 2026-08-24 | Repo forces LF via `.gitattributes`, but Windows tooling (and some editors) still emit CRLF locally | Spurious whole-file diffs / noisy `git status` on touched files | Keep `.gitattributes` as the single source of truth (`* text=auto eol=lf`); never fight it per-file. Docker builds are unaffected (context is copied as-is) |
| C-04 | 2026-08-24 | Docs pinned `timescale/timescaledb:2-pg16` — tag never existed on Docker Hub (Hub exposes `latest-pg16`, exact pins like `2.29.2-pg16`) | Dev/CI/prod compose could not pull | Doc-first fix: all three backend_docs artifacts re-pinned to `2.29.2-pg16` (docs commit db4ef95); local compose mirrors; upgrades are now deliberate pin bumps |
| C-05 | 2026-08-24 | EMQX 5.5 crash-loops (`missing_mechanism_field`) when an authentication BACKEND env override is set without its MECHANISM pair — the old docs-only-BACKEND form is invalid on 5.5 | Dev EMQX container restarts forever, dashboard unreachable | Env overrides must always carry BOTH keys: `EMQX_AUTHENTICATION__1__MECHANISM=password_based` + `...__BACKEND=built_in_database` (docker-compose.dev.yml); ref comment committed in M0-07 |
| C-06 | 2026-08-24 | Blueprint §8 HEALTHCHECK targets `/api/v1/health/` which first EXISTS in v0.6 | The backend container reports `unhealthy` by design throughout v0.1–v0.5 despite serving fine | Accepted placeholder, not a bug: do NOT weaken/remove the HEALTHCHECK to make it green (Dockerfile ref). Monitor via logs until v0.6 lands the endpoint |
