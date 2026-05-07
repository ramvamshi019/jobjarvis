#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  JobJarvis – one-shot bootstrap for a fresh Ubuntu 22.04 / 24.04 VM
#
#  WHAT THIS DOES
#    1. Updates apt
#    2. Installs Docker + Docker Compose plugin
#    3. Adds your user to the docker group
#    4. Builds + starts every service in deploy/docker-compose.prod.yml
#    5. Waits for healthy state, prints the URL
#
#  USAGE  (from the VM, after cloning the repo to ~/jobjarvis):
#    cd ~/jobjarvis
#    cp deploy/.env.example deploy/.env
#    nano deploy/.env                    # set DOMAIN + secrets
#    bash deploy/deploy.sh
#
#  REQUIREMENTS
#    • Ubuntu 22.04 / 24.04 (other distros: install docker yourself, then run
#      the `Bring up the stack` section directly)
#    • Domain DNS A record pointing to this VM's external IP
#    • Ports 80, 443 open in your firewall (GCP: allow http-server, https-server)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."     # repo root

# ── Sanity ────────────────────────────────────────────────────────────────────
if [ ! -f deploy/.env ]; then
    echo "FATAL: deploy/.env not found.  Copy deploy/.env.example to deploy/.env "
    echo "       and fill in DOMAIN + SECRET_KEY + POSTGRES_PASSWORD."
    exit 2
fi

source deploy/.env
: "${DOMAIN:?DOMAIN must be set in deploy/.env}"
: "${SECRET_KEY:?SECRET_KEY must be set in deploy/.env}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in deploy/.env}"

if [ "$SECRET_KEY" = "REPLACE_ME_WITH_openssl_rand_hex_32" ]; then
    echo "FATAL: SECRET_KEY is still the placeholder. Generate one:"
    echo "       openssl rand -hex 32"
    exit 2
fi

# ── Install Docker if missing ────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "→ Installing Docker…"
    sudo apt-get update -y
    sudo apt-get install -y ca-certificates curl gnupg lsb-release

    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update -y
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                            docker-buildx-plugin docker-compose-plugin

    sudo usermod -aG docker "$USER" || true
    echo "→ Docker installed. You may need to log out + back in for group changes."
fi

# Allow current shell to run docker without sudo (for this session)
DOCKER="docker"
if ! docker ps >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

# ── Forward env to compose ───────────────────────────────────────────────────
export $(grep -v '^#' deploy/.env | xargs)

# ── Build + start ────────────────────────────────────────────────────────────
echo
echo "→ Building images (5–10 min on first deploy)…"
$DOCKER compose -f deploy/docker-compose.prod.yml --env-file deploy/.env build

echo
echo "→ Starting stack…"
$DOCKER compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d

echo
echo "→ Waiting for backend healthcheck (up to 90 sec)…"
for i in $(seq 1 18); do
    state=$($DOCKER inspect -f '{{.State.Health.Status}}' jj_backend 2>/dev/null || echo starting)
    echo "    [$((i*5))s] backend: $state"
    if [ "$state" = "healthy" ]; then break; fi
    sleep 5
done

echo
echo "─────────────────────────────────────────────"
echo "  JobJarvis stack running."
echo "  URL:    https://$DOMAIN"
echo "  Logs:   docker compose -f deploy/docker-compose.prod.yml logs -f"
echo "  Status: docker ps"
echo "─────────────────────────────────────────────"
echo
echo "Next steps:"
echo "  1. Check https://$DOMAIN loads (Caddy provisions cert in <30 sec)"
echo "  2. Sign up via the UI"
echo "  3. Run the embedding backfill once:"
echo "       docker exec jj_celery_worker python3 -u /app/scripts/backfill_embeddings.py"
echo "  4. Optionally run the discovery pipeline:"
echo "       bash backend/scripts/run_all_discovery.sh"
echo
