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
COPY requirements/ requirements/
RUN pip wheel --no-cache-dir --wheel-dir /wheels-base  -r requirements/base.txt && \
    pip wheel --no-cache-dir --wheel-dir /wheels-test  -r requirements/test.txt

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=builder /wheels-test /wheels-test
RUN pip install --no-cache-dir /wheels-test/* && rm -rf /wheels-test
COPY . .
USER appuser
# CI harness entrypoint is overridden by docker-compose.ci.yml `command: pytest`.

# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/morghdari/backend"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Non-root runner (Blueprint §8): the container process never needs root.
RUN useradd --create-home appuser
WORKDIR /app
COPY --from=builder /wheels-base /wheels-base
COPY --from=builder /build/requirements requirements
RUN pip install --no-cache-dir /wheels-base/* && rm -rf /wheels-base
COPY . .
# Blueprint §8: healthcheck targets the platform health endpoint. NOTE until
# v0.6 implements /api/v1/health/ this check reports unhealthy by design —
# logged as CONSTRAINTS C-06 so nobody "fixes" it by weakening the spec
# (C-05 is taken by the EMQX-mechanism env override noted in M0-07 compose).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/', timeout=4)" || exit 1
USER appuser
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
