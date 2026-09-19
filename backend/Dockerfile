# syntax=docker/dockerfile:1
# =============================================================================
# Morghdari backend — ONE image, FOUR commands (Implementation Blueprint §8):
#   daphne config.asgi:application          (API + WebSocket)
#   celery -A config worker                 (ingestion/alerts)
#   celery -A config beat                   (schedulers)
#   python -m morghdari.subscriber          (MQTT→Redis bridge, v0.3)
#
# فارسی: یک ایمیج واحد برای هر چهار سرویس؛ مرحلهٔ builder فقط wheel می‌سازد و
# مرحلهٔ نهایی بدون کامپایلر اجرا می‌شود (کاربر غیر-ریشه appuser).
#
# Stage layout:
#   builder : compiles wheels for base AND test requirements (no interpreter
#             headers needed in the final image).
#   test    : runtime + test wheels — the ONLY stage CI runs (target: test).
#   runtime : final stage = base wheels ONLY. Production purity is structural:
#             pytest physically cannot exist in a pulled prod image because
#             this stage never copies /wheels-test.
# =============================================================================

FROM python:3.12-slim AS builder
WORKDIR /build
# Optional package-index override (2026-09-16, VPS day-1 finding on
# 185.8.175.150): pypi.org is effectively unreachable from some hosts — the
# index answered after 45 s and then timed out, so `pip wheel` failed with
# "Could not find a version that satisfies the requirement Django<5.2".
# Point the build at a reachable index instead of changing anything else:
#   docker compose ... build --build-arg PIP_INDEX_URL=https://<mirror>/simple/
# (docker-compose.vps.yml forwards the value from .env's PIP_INDEX_URL).
# Unset or empty keeps the stock PyPI behaviour, so CI and other hosts are
# unaffected. NOTE: a third-party mirror serves the packages — only set this
# when the default index is genuinely unreachable, and prefer a mirror you
# trust or an internal one.
ARG PIP_INDEX_URL=""
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      printf '[global]\nindex-url = %s\ntimeout = 60\nretries = 5\n' "$PIP_INDEX_URL" > /etc/pip.conf; \
      echo "pip index override:"; cat /etc/pip.conf; \
    fi
COPY requirements/ requirements/
RUN pip wheel --no-cache-dir --wheel-dir /wheels-base  -r requirements/base.txt && \
    pip wheel --no-cache-dir --wheel-dir /wheels-test  -r requirements/test.txt

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS test
# BUG-17 (QA 19.7): trivy GATE3 failed on 12 fixable HIGH/CRITICAL in debian 13.6 slim (perl-base, libsqlite3-0, libpcre2-8-0, gzip). Upgrade at build time until the upstream slim digest incorporates the deb13u1/u2 fixes; re-check each rebuild.
# فارسی: آسیب‌پذیری‌های قابل‌رفع ایمیج پایه با ارتقا در زمان بیلد رفع می‌شود تا دایجست بالادستی اصلاح شود.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# install -d makes /app itself appuser-owned (WORKDIR alone would create it
# root-owned; pytest-cov then cannot create its .coverage file inside it).
RUN useradd --create-home appuser && install -d -o appuser -g appuser /app
WORKDIR /app
COPY --from=builder /wheels-test /wheels-test
RUN pip install --no-cache-dir /wheels-test/* && rm -rf /wheels-test
# --chown: pytest-cov must write .coverage.<host>.<pid> into /app while running
# as appuser (first CI run proved root-owned /app breaks the coverage flush).
COPY --chown=appuser:appuser . .
USER appuser
# CI harness entrypoint is overridden by docker-compose.ci.yml `command: pytest`.

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
# BUG-17 (QA 19.7): trivy GATE3 failed on 12 fixable HIGH/CRITICAL in debian 13.6 slim (perl-base, libsqlite3-0, libpcre2-8-0, gzip). Upgrade at build time until the upstream slim digest incorporates the deb13u1/u2 fixes; re-check each rebuild.
# فارسی: آسیب‌پذیری‌های قابل‌رفع ایمیج پایه با ارتقا در زمان بیلد رفع می‌شود تا دایجست بالادستی اصلاح شود.
LABEL org.opencontainers.image.source="https://github.com/morghdari/backend"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Non-root runner (Blueprint §8): the container process never needs root.
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=builder /wheels-base /wheels-base
COPY --from=builder /build/requirements requirements
RUN pip install --no-cache-dir /wheels-base/* && rm -rf /wheels-base
COPY . .
# O6 photo uploads (contract v1.7.0) land in MEDIA_ROOT (/app/media): the dir
# must exist AND belong to appuser, otherwise the first prod upload 500s on
# PermissionDenied while every gate stays green (tests use a tempdir MEDIA_ROOT
# and never touch this path). Prod must additionally mount a named volume on
# /app/media (see DEPLOYMENT.md) or uploads die with the container.
RUN mkdir -p /app/media /app/static && chown appuser:appuser /app/media /app/static
# PROD-FIX 2026-09-19 (admin CSS 404, live on VPS 82.115.17.252): bake
# collected static into the image so a FRESH static_volume is pre-populated
# on first mount (Docker copies image contents into a new named volume), and
# appuser owns /app/static so runtime `collectstatic` never fails with
# PermissionDenied (the volume is root-owned at creation; daphne runs as
# appuser). Uses development defaults — no secrets needed, static output is
# settings-independent.
USER appuser
RUN python manage.py collectstatic --noinput --clear -v1
# Blueprint §8: healthcheck targets the platform health endpoint. M7 rehearsal
# 2026-09-07: probe is a RAW-SOCKET liveness check — SECURE_SSL_REDIRECT=True
# turns an http probe into a 301 whose https follow times out (urllib hung the
# container permanently unhealthy); any HTTP response proves the ASGI server
# answers, the content check is nginx's (external 200). C-06 resolution note:
# /api/v1/health/ exists since v0.6.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',8000),4); s.sendall(b'GET /api/v1/health/ HTTP/1.0\r\nHost: localhost\r\n\r\n'); assert s.recv(64).startswith(b'HTTP/')" || exit 1
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
