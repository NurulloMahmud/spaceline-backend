#!/bin/bash
# Deploy the currently pushed main onto this server.
#
# Authentication comes from the git credential file configured on the box
# (credential.helper store --file=/root/.git-credentials-email-agent), so no
# token appears in this script, in the remote URL, or in shell history.
set -e

APP_DIR=/home/api/email-agent
PROGRAM=email_agent
HEALTH_URL=http://127.0.0.1:8100/api/v1/health

cd "$APP_DIR"

echo "==> Pulling latest code..."
git pull --ff-only origin main

echo "==> Installing dependencies..."
./venv/bin/pip install -q -r requirements.txt

echo "==> Restarting service..."
supervisorctl restart "$PROGRAM"

echo "==> Waiting for startup..."
for i in $(seq 1 15); do
    sleep 1
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        echo "==> Healthy: $(curl -s "$HEALTH_URL")"
        supervisorctl status "$PROGRAM"
        exit 0
    fi
done

echo "==> FAILED: service did not become healthy in 15s" >&2
supervisorctl status "$PROGRAM" >&2
tail -30 /var/log/email_agent.err.log >&2
exit 1
