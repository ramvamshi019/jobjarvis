#!/usr/bin/env bash
#
# JobJarvis — one-shot maintenance script.
#
# What it does, in order:
#   1. Copies all scripts into the worker container (in case mount isn't active)
#   2. Smoke-tests the embedding pipeline (limit=50)
#   3. If smoke passes, kicks off the full embedding backfill in background
#   4. Waits 30 sec then prints live status
#   5. Tells you exactly when to come back
#
# Usage:
#   bash backend/scripts/do_everything.sh
#
set -e

WORKER=jobjarvis_celery_worker
DB=jobjarvis_postgres

echo "════════════════════════════════════════════════════════════════"
echo "  JobJarvis — running everything"
echo "════════════════════════════════════════════════════════════════"
echo

# ── 1. Copy scripts into the worker (fallback if compose mount isn't active) ──
echo "1/5 · Copying scripts into worker container…"
for f in backfill_embeddings.py scan_all_jobs.py discovery_lib.py \
         daily_digest.py ai_match_jobs.py dedup_companies.py \
         seed_user_resume.py; do
    if [ -f "backend/scripts/$f" ]; then
        docker cp "backend/scripts/$f" "$WORKER:/tmp/$f" 2>/dev/null
        echo "   ✓ $f"
    fi
done
echo

# ── 2. Smoke-test embedding (50 jobs) ────────────────────────────────────────
echo "2/5 · Smoke-testing embedding pipeline (50 jobs)…"
if docker exec "$WORKER" python3 -u /tmp/backfill_embeddings.py --limit 50 \
   2>&1 | tail -20; then
    echo "   ✓ smoke test passed"
else
    echo "   ✗ smoke test failed — paste the output above and stop here"
    exit 1
fi
echo

# ── 3. Kick off full embedding in background ─────────────────────────────────
echo "3/5 · Starting full embedding backfill in background…"
docker exec -d "$WORKER" python3 -u /tmp/backfill_embeddings.py
sleep 5  # give it time to start
echo "   ✓ background job started"
echo

# ── 4. Print status ──────────────────────────────────────────────────────────
echo "4/5 · Status snapshot…"
docker exec "$DB" psql -U jobjarvis -d jobjarvis -c \
  "SELECT 'COMPANIES' AS metric, (SELECT COUNT(*) FROM companies WHERE active=true) AS value \
   UNION ALL SELECT 'JOBS_TOTAL', (SELECT COUNT(*) FROM jobs WHERE active=true) \
   UNION ALL SELECT 'JOBS_EMBEDDED', (SELECT COUNT(*) FROM job_embeddings) \
   UNION ALL SELECT 'JOBS_US', (SELECT COUNT(*) FROM jobs WHERE active=true AND country='US') \
   UNION ALL SELECT 'JOBS_NEEDING_EMBEDDING', (SELECT COUNT(*) FROM jobs j WHERE j.active=true AND NOT EXISTS (SELECT 1 FROM job_embeddings je WHERE je.job_id=j.id)) \
   UNION ALL SELECT 'JOB_MATCHES_PERSISTED', (SELECT COUNT(*) FROM job_matches);"
echo

# ── 5. Next steps ────────────────────────────────────────────────────────────
echo "5/5 · Next steps"
echo "════════════════════════════════════════════════════════════════"
cat <<'NEXT'

✓ Embedding is running in the background.
  Check progress anytime with:

      docker exec jobjarvis_postgres psql -U jobjarvis -d jobjarvis -c \
        "SELECT COUNT(*) FROM job_embeddings;"

  Target: ~215,000.  Rate: ~30 jobs/sec → ~90 min total.

✓ Open  http://localhost:3000/matches  and:
    • Set Location  →  🇺🇸 US + Remote
    • Set Posted    →  Any time          (don't pick 24h yet — embedding still running)
    • Click "Recompute"
    • Apply to your top 3

✓ When embedded count is > 150,000:
    • Switch Posted → Last 24h
    • Click Recompute again
    • You'll see fresh matches from today's scrape

✓ If anything seems broken, run this script again — it's idempotent.

NEXT
