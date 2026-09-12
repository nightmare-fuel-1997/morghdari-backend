# syntax=docker/dockerfile:1
# =============================================================================
# morghdari/postgres-walg — TimescaleDB + wal-g continuous WAL archiving (M8-01)
#
# Adds to the pinned timescale image:
#   1. the wal-g binary (Postgres build, pinned release);
#   2. postgresql config turning archive_mode ON with WAL push
#      (DR §2.1 / §3.3 — the RPO math depends on archive_timeout <= 300).
#
# Base-image reality (found live 2026-09-12, T2 preflight): the pinned
# timescale image is ALPINE/musl, not Ubuntu — so this Dockerfile uses apk
# (no apt), fetches with wget (preinstalled), installs gcompat (the wal-g
# release asset is a glibc binary; verified to run under gcompat), and takes
# the asset's real inner filename (wal-g-pg-*.tar.gz extracts
# `wal-g-pg-<ver>-<arch>`, NOT `wal-g`).
#
# config_file reality (same preflight): passing -c config_file REPLACES the
# stock PGDATA/postgresql.conf, so wal-g-postgres.conf MUST repeat the
# connectivity-critical stock settings (listen_addresses, timescaledb
# preload) — otherwise the rehealed stack is deaf on container restart.
#
# wal-g reads its storage config from the environment (WALG_S3_* / AWS_*),
# which compose injects — no credentials ever live in this image or its conf.
#
# The T2 restore drill (DR §3.1) appends recovery settings into
# PGDATA/postgresql.auto.conf under PGDATA; this image changes none of that.
#
# Rehearsal (laptop): the rehearsal compose override adds `build: ./docker/postgres-walg`
# (no registry on the laptop). VPS: push ghcr.io/morghdari/postgres-walg:<tag>.
# =============================================================================

FROM timescale/timescaledb:2.29.2-pg16

# Pinned wal-g release + Postgres asset (wal-g GitHub "releases" table,
# v3.x = the Go rewrite). NOTE: upstream publishes Ubuntu/glibc assets only —
# gcompat below is what lets them run on this Alpine base.
ARG WALG_RELEASE=v3.0.9
ARG WALG_ASSET=wal-g-pg-22.04-amd64.tar.gz
ARG WALG_BIN_INNER=wal-g-pg-22.04-amd64

RUN set -eux; \
    apk add --no-cache ca-certificates gcompat; \
    wget -qO /tmp/walg.tar.gz \
      "https://github.com/wal-g/wal-g/releases/download/${WALG_RELEASE}/${WALG_ASSET}"; \
    tar -xzf /tmp/walg.tar.gz -C /tmp; \
    install -m 0755 "/tmp/${WALG_BIN_INNER}" /usr/local/bin/wal-g; \
    rm -rf /tmp/walg.tar.gz "/tmp/${WALG_BIN_INNER}"; \
    wal-g --version 2>&1 | grep -i wal-g

# wal-push stderr lands here (archive_command appends); postgres must own it.
RUN mkdir -p /var/log/wal-g && chown postgres:postgres /var/log/wal-g

COPY wal-g-postgres.conf /etc/postgresql/wal-g-postgres.conf

# config_file passed on the command line: the stock timescale entrypoint still
# runs (initdb on first boot, docker-entrypoint-initdb.d, etc.) and then
# postgres picks up BOTH our config file AND the drill-injected
# postgresql.auto.conf (auto configs always load).
CMD ["postgres", "-c", "config_file=/etc/postgresql/wal-g-postgres.conf"]
