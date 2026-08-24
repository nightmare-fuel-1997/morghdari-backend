-- =============================================================================
-- initdb/00_extensions.sql — runs once on FIRST bootstrap of a fresh cluster
-- (mounted read-only at /docker-entrypoint-initdb.d by both compose files).
--
-- فارسی: این اسکریپت فقط در راه‌اندازیِ اولِ کلاستر اجرا می‌شود؛ افزونهٔ
-- TimescaleDB از همین‌جا فعال می‌شود تا hypertableها (M1-02) کار کنند.
--
-- Why here and not in a Django migration: CREATE EXTENSION needs superuser
-- privileges. The entrypoint runs as the bootstrap superuser; the app's
-- migrate role deliberately is NOT one (Blueprint §3 rule 1). The image is
-- timescale/timescaledb so no apt/pip install is needed — the extension
-- binaries ship inside the image.
--
-- Idempotent by IF NOT EXISTS: safe against re-run on an existing volume.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
