#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
rand() { openssl rand -hex 32; }
if [ -f .env ]; then echo ".env exists, not overwriting"; exit 0; fi
cat > .env <<EOF
MORGHDARI_TAG=vps-01
DJANGO_SECRET_KEY=$(rand)
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,api.morghdari.com
DJANGO_DEBUG=False
DATABASE_NAME=morghdari
DATABASE_USER=morghdari_app
DATABASE_PASSWORD=$(rand)
POSTGRES_ADMIN_USER=morghdari_admin
POSTGRES_ADMIN_PASSWORD=$(rand)
POSTGRES_EXPORTER_PASSWORD=$(rand)
REDIS_PASSWORD=$(rand)
EMQX_DASHBOARD_PASSWORD=$(rand)
EMQX_NODE__COOKIE=$(rand)
EMQX_DB_USER=emqx_auth
EMQX_DB_PASSWORD=$(rand)
SUBSCRIBER_USERNAME=subscriber-01
SUBSCRIBER_PASSWORD=$(rand)
SUBSCRIBER_USERNAME_2=subscriber-02
SUBSCRIBER_PASSWORD_2=$(rand)
WALG_S3_PREFIX=s3://morghdari-walg-test
AWS_ACCESS_KEY_ID=testkey
AWS_SECRET_ACCESS_KEY=testsecret
AWS_REGION=auto
AWS_ENDPOINT=http://minio.invalid
AWS_S3_FORCE_PATH_STYLE=true
CERTIFICATE_EMAIL=test@localhost
EOF
echo "ENV-CREATED $(grep -c . .env) lines"
