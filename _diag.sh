#!/usr/bin/env bash
# JobJarvis diagnostic — run from project root: bash _diag.sh
set -u
cd "$(dirname "$0")"

bar() { printf '\n──── %s ────\n' "$1"; }

bar "1. Container status"
docker compose ps

bar "2. Last 60 lines of celery_beat (looking for 'beat: Starting', 'Sending due task')"
docker compose logs --tail 60 celery_beat 2>&1 | tail -60

bar "3. Last 60 lines of celery_worker — startup + any tasks"
docker compose logs --tail 200 celery_worker 2>&1 \
  | grep -iE "ready|received|succeed|fail|error|traceback|scan_tier|PIPELINE|fetched" \
  | tail -60

bar "4. Trigger a fresh Tier-1 scan and capture the task id"
TASK_OUTPUT=$(docker compose exec -T backend python -c "
from app.workers.scan_tasks import scan_tier_companies
r = scan_tier_companies.delay(1)
print(r.id)
" 2>&1)
TASK_ID=$(echo "$TASK_OUTPUT" | tail -n1 | tr -d '\r')
echo "queued task id: $TASK_ID"

bar "5. Wait 15 seconds for worker to pick it up"
sleep 15

bar "6. Task state (PENDING / STARTED / SUCCESS / FAILURE)"
docker compose exec -T backend python -c "
from celery.result import AsyncResult
r = AsyncResult('$TASK_ID')
print('state:', r.state)
print('info :', r.info)
" 2>&1

bar "7. Worker log since the task was queued"
docker compose logs --since 30s celery_worker 2>&1 | tail -80

bar "8. Job-row counts in Postgres (was 2032 before — should grow if scan ran)"
docker compose exec -T postgres psql -U jobjarvis -d jobjarvis -c "
  SELECT
    COUNT(*)                              AS total_jobs,
    COUNT(*) FILTER (WHERE first_seen_at > now() - interval '5 minutes') AS new_last_5min,
    COUNT(*) FILTER (WHERE first_seen_at > now() - interval '1 hour')    AS new_last_hour
  FROM jobs;
" 2>&1

bar "9. Most recent 5 jobs by first_seen_at"
docker compose exec -T postgres psql -U jobjarvis -d jobjarvis -c "
  SELECT id, title, company_id, first_seen_at
  FROM jobs
  ORDER BY first_seen_at DESC
  LIMIT 5;
" 2>&1

bar "10. Companies in Tier-1 (the scan target)"
docker compose exec -T postgres psql -U jobjarvis -d jobjarvis -c "
  SELECT COUNT(*) AS tier1_companies,
         COUNT(*) FILTER (WHERE active = true) AS active,
         COUNT(*) FILTER (WHERE ats_type IS NOT NULL) AS with_ats
  FROM companies
  WHERE tier = 1;
" 2>&1

echo ""
echo "──── DONE ────"
echo "Paste this entire output back to me."
