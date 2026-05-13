#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  JobJarvis production smoke-test
#
#  Run this AFTER `bash deploy/deploy.sh` finishes.  It walks through every
#  critical dependency and tells you what's broken.  Exit code is the number
#  of failures.
#
#  Usage:    bash deploy/smoke_test.sh
# ─────────────────────────────────────────────────────────────────────────────
set -u

cd "$(dirname "$0")/.."

source deploy/.env

DOMAIN="${DOMAIN:?DOMAIN must be set in deploy/.env}"
FAIL=0
PASS=0
green="\033[32m"; red="\033[31m"; yellow="\033[33m"; reset="\033[0m"

pass() { printf "${green}✅ %s${reset}\n" "$*"; PASS=$((PASS+1)); }
fail() { printf "${red}❌ %s${reset}\n" "$*"; FAIL=$((FAIL+1)); }
warn() { printf "${yellow}⚠️  %s${reset}\n" "$*"; }
heading() { printf "\n${yellow}▸ %s${reset}\n" "$*"; }

heading "1. Container health"
for svc in jj_postgres jj_redis jj_backend jj_celery_worker jj_celery_beat jj_frontend jj_caddy; do
    state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$svc" 2>/dev/null)
    case "$state" in
        running/healthy|running/n/a) pass "$svc → $state" ;;
        *)                            fail "$svc → ${state:-not found}" ;;
    esac
done

heading "2. Database connectivity + schema"
if docker exec jj_postgres pg_isready -U jobjarvis >/dev/null 2>&1; then
    pass "postgres pg_isready"
else
    fail "postgres pg_isready"
fi
COMPANIES=$(docker exec jj_postgres psql -U jobjarvis -d jobjarvis -tAc \
    "SELECT COUNT(*) FROM companies;" 2>/dev/null || echo "ERROR")
JOBS=$(docker exec jj_postgres psql -U jobjarvis -d jobjarvis -tAc \
    "SELECT COUNT(*) FROM jobs;" 2>/dev/null || echo "ERROR")
if [[ "$COMPANIES" =~ ^[0-9]+$ ]] && [ "$COMPANIES" -gt 0 ]; then
    pass "companies table: $COMPANIES rows"
else
    fail "companies table: $COMPANIES"
fi
if [[ "$JOBS" =~ ^[0-9]+$ ]] && [ "$JOBS" -gt 0 ]; then
    pass "jobs table: $JOBS rows"
else
    fail "jobs table: $JOBS"
fi

heading "3. Backend internal health"
if docker exec jj_backend curl -fs http://localhost:8000/api/health >/dev/null 2>&1; then
    pass "/api/health (internal)"
else
    fail "/api/health (internal)"
fi

heading "4. HTTPS reachability"
HTTP=$(curl -ksw '%{http_code}' -o /dev/null "https://${DOMAIN}/api/health" 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    pass "https://${DOMAIN}/api/health returned 200"
else
    fail "https://${DOMAIN}/api/health returned $HTTP — DNS or cert issue?"
fi

heading "5. TLS certificate"
EXP=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$EXP" ]; then
    pass "TLS cert expires: $EXP"
else
    fail "couldn't read TLS cert — Let's Encrypt validation may have failed"
fi

heading "6. AI provider"
AI=$(docker exec jj_backend python3 -c "
import os
print('anthropic' if os.environ.get('ANTHROPIC_API_KEY') else
      ('openai' if os.environ.get('OPENAI_API_KEY') else 'TEMPLATE_ONLY'))
" 2>/dev/null)
if [ "$AI" = "TEMPLATE_ONLY" ]; then
    warn "no AI key set — cover letters / auto-apply Q&A will use template stubs"
else
    pass "AI provider: $AI"
fi

heading "7. Persistent volumes"
for vol in jj_postgres_data jj_uploads_data jj_backups_data jj_playwright_browsers; do
    # Compose typically prefixes with project name
    if docker volume inspect "deploy_${vol#jj_}" >/dev/null 2>&1 || \
       docker volume inspect "${vol}" >/dev/null 2>&1; then
        pass "volume: ${vol#jj_}"
    else
        warn "volume not found (may use different prefix): ${vol#jj_}"
    fi
done

heading "8. Celery worker can pick up tasks"
QUEUED=$(docker exec jj_backend python3 -c "
from app.workers.cities_discovery_tasks import discover_one_city
r = discover_one_city.delay('sf')
print(r.id)
" 2>/dev/null)
if [ -n "$QUEUED" ]; then
    pass "queued test task → id=$QUEUED"
    sleep 3
    if docker logs jj_celery_worker --tail 50 2>&1 | grep -q "$QUEUED\|discover_one_city"; then
        pass "celery_worker picked it up"
    else
        warn "celery_worker hasn't logged the task yet — check manually with: docker logs jj_celery_worker"
    fi
else
    fail "couldn't queue test task"
fi

heading "9. Celery beat is scheduling"
if docker logs jj_celery_beat --tail 50 2>&1 | grep -q "beat:"; then
    pass "celery_beat is alive"
else
    warn "celery_beat may not be running — check: docker logs jj_celery_beat"
fi

heading "10. Backup task works"
BR=$(docker exec jj_celery_worker python3 -c "
from app.workers.backup_tasks import backup_database
import json; print(json.dumps(backup_database()))
" 2>/dev/null)
if echo "$BR" | grep -q '"ok": true'; then
    pass "pg_dump backup: $(echo "$BR" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"size_mb\"]} MB in {d[\"elapsed_s\"]}s')")"
else
    fail "backup task failed: $BR"
fi

printf "\n"
printf "═══════════════════════════════════════════════\n"
if [ "$FAIL" -eq 0 ]; then
    printf "${green}✅ ALL CHECKS PASSED  (passed=%d)${reset}\n" "$PASS"
else
    printf "${red}❌ %d FAILURES, %d passed${reset}\n" "$FAIL" "$PASS"
fi
printf "═══════════════════════════════════════════════\n"
exit "$FAIL"
