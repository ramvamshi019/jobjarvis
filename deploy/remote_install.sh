#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  JobJarvis – one-line remote installer.
#
#  Pipe this to the server from your laptop:
#
#    cat deploy/remote_install.sh | ssh root@<SERVER_IP> \
#        ANTHROPIC_API_KEY=sk-ant-... \
#        USAJOBS_API_KEY=fQluJm... \
#        DOMAIN=jobjarvis.example.com  bash
#
#  What it does on the server:
#    1. apt install git + docker + docker-compose
#    2. clone the repo
#    3. generate strong secrets (SECRET_KEY, POSTGRES_PASSWORD)
#    4. write deploy/.env with everything filled in
#    5. run deploy.sh (builds + starts every container)
#    6. run smoke_test.sh and exit with its status
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="https://github.com/ramvamshi019/jobjarvis.git"
HOME_DIR="${HOME:-/root}"
APP_DIR="${HOME_DIR}/jobjarvis"

# ── Inputs (read from environment) ────────────────────────────────────────────
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"   # optional — free path still works
USAJOBS_API_KEY="${USAJOBS_API_KEY:-}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
DOMAIN="${DOMAIN:-}"            # blank → access via IP only, no HTTPS
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"

# ── Install Docker if missing ────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "→ Installing Docker"
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg lsb-release git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io \
                       docker-buildx-plugin docker-compose-plugin
fi

# ── Clone (or update) the repo ───────────────────────────────────────────────
if [ ! -d "$APP_DIR" ]; then
    echo "→ Cloning JobJarvis"
    git clone "$REPO" "$APP_DIR"
else
    echo "→ Updating JobJarvis"
    cd "$APP_DIR" && git pull
fi
cd "$APP_DIR"

# ── Write deploy/.env ────────────────────────────────────────────────────────
SECRET_KEY="$(openssl rand -hex 32)"
POSTGRES_PASSWORD="$(openssl rand -hex 24)"

cat > deploy/.env <<EOF
DOMAIN=${DOMAIN}
SECRET_KEY=${SECRET_KEY}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
OPENAI_API_KEY=${OPENAI_API_KEY}
USAJOBS_API_KEY=${USAJOBS_API_KEY}
USAJOBS_USER_EMAIL=ramvamshikrishna0@gmail.com
SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}
AI_DAILY_BUDGET_USD=5.0
ACCESS_TOKEN_EXPIRE_MINUTES=10080
LOG_LEVEL=INFO
BACKUP_RETENTION=7
CORS_ALLOWED_ORIGINS=
EOF

chmod 600 deploy/.env

# ── Bring up the stack ──────────────────────────────────────────────────────
echo "→ Running deploy.sh (5–10 min on first deploy)"
bash deploy/deploy.sh

# ── Verify ───────────────────────────────────────────────────────────────────
echo "→ Running smoke_test.sh"
if [ -x deploy/smoke_test.sh ]; then
    bash deploy/smoke_test.sh || true
fi

echo
echo "═════════════════════════════════════════════════════════════════"
if [ -n "$DOMAIN" ]; then
    echo "  Site is at:  https://${DOMAIN}"
else
    IP=$(curl -s ifconfig.me || echo "<your-server-ip>")
    echo "  Site is at:  http://${IP}"
fi
echo "═════════════════════════════════════════════════════════════════"
