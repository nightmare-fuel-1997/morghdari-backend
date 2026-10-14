#!/usr/bin/env bash
cd "$(dirname "$0")/.."
echo 'vps-01' > .deploy_tag
bash scripts/deploy.sh v0.7-broken > /tmp/qa_deploy.log 2>&1
echo "deploy-exit=$?"
tail -6 /tmp/qa_deploy.log
