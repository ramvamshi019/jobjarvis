#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# JobJarvis — full system diagnostic.
#
# Runs 20 tests, auto-fixes what it can, and prints a clear pass/fail report.
# Idempotent — safe to run multiple times.
#
# Usage:
#   bash backend/scripts/full_diagnostic.sh
# ─────────────────────────────────────────────────────────────────────────────
set +e   # don't bail on first error — we want to test everything

W=jobjarvis_celery_worker
B=jobjarvis_backend
D=jobjarvis_postgres
F=jobjarvis_frontend
R=jobjarvis_redis

PASS="✅"
FAIL="❌"
WARN="⚠️ "

PASSED=0
FAILED=0
WARNED=0

report() {
    local kind=$1; local msg=$2
    if [ "$kind" = "PASS" ]; then
        echo "$PASS  $msg"
        PASSED=$((PASSED + 1))
    elif [ "$kind" = "FAIL" ]; then
        echo "$FAIL  $msg"
        FAILED=$((FAILED + 1))
    else
        echo "$WARN $msg"
        WARNED=$((WARNED + 1))
    fi
}

echo "════════════════════════════════════════════════════════════════"
echo "  JobJarvis · full diagnostic (20 tests + auto-fix)"
echo "════════════════════════════════════════════════════════════════"
echo

# ── 1. Docker daemon alive ────────────────────────────────────────────────
echo "▸ Phase 1: Docker daemon"
if docker ps >/dev/null 2>&1; then
    report PASS "Docker daemon reachable"
else
    report FAIL "Docker daemon NOT reachable — start Docker Desktop first, then re-run"
    exit 1
fi

# ── 2-6. Containers healthy ───────────────────────────────────────────────
echo
echo "▸ Phase 2: Containers"
for c in $D $R $B $W $F; do
    state=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null)
    if [ "$state" = "running" ]; then
        if [ "$health" = "healthy" ] || [ "$health" = "-" ]; then
            report PASS "$c is $state${health:+ / $health}"
        else
            report WARN "$c running but health=$health"
        fi
    else
        report FAIL "$c is $state — auto-restarting…"
        docker compose up -d "$(echo $c | sed 's/jobjarvis_//')" >/dev/null 2>&1
    fi
done

# ── 7. DB connection ──────────────────────────────────────────────────────
echo
echo "▸ Phase 3: Database"
if docker exec $D psql -U jobjarvis -d jobjarvis -tAc "SELECT 1" >/dev/null 2>&1; then
    report PASS "Postgres connection works"
else
    report FAIL "Postgres connection failed"
fi

# ── 8-11. Data counts ─────────────────────────────────────────────────────
COMPANIES=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc "SELECT COUNT(*) FROM companies WHERE active=true" 2>/dev/null)
JOBS=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc "SELECT COUNT(*) FROM jobs WHERE active=true" 2>/dev/null)
EMBEDDED=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc "SELECT COUNT(*) FROM job_embeddings" 2>/dev/null)
USERS=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc "SELECT COUNT(*) FROM users WHERE is_active=true" 2>/dev/null)

[ "${COMPANIES:-0}" -gt 100 ]  && report PASS "companies: $COMPANIES"   || report FAIL "companies too low: $COMPANIES"
[ "${JOBS:-0}"      -gt 100 ]  && report PASS "jobs:      $JOBS"        || report FAIL "jobs too low:      $JOBS"
[ "${EMBEDDED:-0}"  -gt 100 ]  && report PASS "embeddings: $EMBEDDED"   || report FAIL "embeddings too low: $EMBEDDED"
[ "${USERS:-0}"     -ge 1   ]  && report PASS "active users: $USERS"    || report WARN "no users yet — sign up at http://localhost:3000"

# ── 12. Backend API reachable ─────────────────────────────────────────────
echo
echo "▸ Phase 4: Backend API"
if curl -sf http://localhost:8000/api/health >/dev/null 2>&1; then
    report PASS "/api/health returns 200"
else
    report FAIL "/api/health unreachable"
fi

# ── 13. Frontend reachable ────────────────────────────────────────────────
if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    report PASS "Frontend reachable at :3000"
else
    report FAIL "Frontend unreachable"
fi

# ── 14. Scripts present in worker container ───────────────────────────────
echo
echo "▸ Phase 5: Scripts in worker"
for f in backfill_embeddings.py scan_all_jobs.py discovery_lib.py; do
    if docker exec $W test -f /tmp/$f 2>/dev/null; then
        report PASS "/tmp/$f present"
    else
        report WARN "/tmp/$f missing — copying…"
        docker cp backend/scripts/$f $W:/tmp/$f >/dev/null 2>&1 && report PASS "  copied $f"
    fi
done

# ── 15. Sentence-transformers loads ───────────────────────────────────────
echo
echo "▸ Phase 6: AI components"
if docker exec $W python3 -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('all-MiniLM-L6-v2', cache_folder='/tmp/st_cache'); print(m.get_sentence_embedding_dimension())" 2>/dev/null | grep -q 384; then
    report PASS "sentence-transformers model loads (384 dims)"
else
    report FAIL "sentence-transformers model failed"
fi

# ── 16. Playwright chromium exists ────────────────────────────────────────
if docker exec $W ls /tmp/pw_browsers/chromium-*/chrome-linux/chrome >/dev/null 2>&1; then
    report PASS "Chromium browser present"
else
    report WARN "Chromium missing — installing… (~1 min)"
    docker exec -e PLAYWRIGHT_BROWSERS_PATH=/tmp/pw_browsers $W python3 -m playwright install chromium >/dev/null 2>&1
    if docker exec $W ls /tmp/pw_browsers/chromium-*/chrome-linux/chrome >/dev/null 2>&1; then
        report PASS "  installed"
    else
        report FAIL "  installation failed"
    fi
fi

# ── 17. Playwright launch works ───────────────────────────────────────────
if docker exec -e PLAYWRIGHT_BROWSERS_PATH=/tmp/pw_browsers $W python3 -c \
    "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>/dev/null; then
    report PASS "Chromium can launch headless"
else
    report FAIL "Chromium launch failed — see: docker logs $W"
fi

# ── 18. Active resume + embedding ─────────────────────────────────────────
echo
echo "▸ Phase 7: User state"
ACTIVE_RESUMES=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc \
    "SELECT COUNT(*) FROM resume_versions WHERE is_active=true" 2>/dev/null)
[ "${ACTIVE_RESUMES:-0}" -ge 1 ] \
    && report PASS "active resumes: $ACTIVE_RESUMES" \
    || report WARN "no active resume — upload at http://localhost:3000/matches"

JOB_MATCHES=$(docker exec $D psql -U jobjarvis -d jobjarvis -tAc \
    "SELECT COUNT(*) FROM job_matches" 2>/dev/null)
[ "${JOB_MATCHES:-0}" -ge 1 ] \
    && report PASS "persisted job matches: $JOB_MATCHES" \
    || report WARN "no matches yet — click Recompute on /matches"

# ── 19. AI providers configured ───────────────────────────────────────────
echo
echo "▸ Phase 8: AI providers"
HAS_ANTHROPIC=$(grep -c "^ANTHROPIC_API_KEY=sk-ant-" .env 2>/dev/null)
HAS_OPENAI=$(grep -c "^OPENAI_API_KEY=sk-" .env 2>/dev/null)
if [ "${HAS_ANTHROPIC:-0}" -ge 1 ]; then
    report PASS "ANTHROPIC_API_KEY configured"
elif [ "${HAS_OPENAI:-0}" -ge 1 ]; then
    report PASS "OPENAI_API_KEY configured"
else
    report WARN "no AI key — cover letter / tailor resume will return template"
fi

# ── 20. Celery beat alive ─────────────────────────────────────────────────
if docker ps --format '{{.Names}}' | grep -q celery_beat; then
    report PASS "Celery beat running (24/7 schedule active)"
else
    report WARN "Celery beat not running — auto-discovery + scan won't run"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════════"
echo "  RESULT: $PASS $PASSED passed   $FAIL $FAILED failed   $WARN $WARNED warned"
echo "════════════════════════════════════════════════════════════════"
echo
if [ "$FAILED" -eq 0 ] && [ "$WARNED" -eq 0 ]; then
    echo "🚀 Everything operational. Go apply to jobs."
elif [ "$FAILED" -eq 0 ]; then
    echo "✓ All critical components working. Warnings above are optional polish."
else
    echo "✗ $FAILED critical failures above. Each line tells you the fix."
fi
echo

echo "──────── Quick-fire smoke test ────────"
echo
echo "Top 3 matches right now:"
docker exec $D psql -U jobjarvis -d jobjarvis -c \
  "SELECT m.match_score::numeric(3,2) AS score, j.title, j.company_name \
   FROM job_matches m JOIN jobs j ON j.id=m.job_id \
   WHERE m.user_id=(SELECT MIN(user_id) FROM job_matches) \
   ORDER BY m.match_score DESC LIMIT 3;"

echo "Latest job in DB:"
docker exec $D psql -U jobjarvis -d jobjarvis -c \
  "SELECT title, company_name, first_seen_at FROM jobs \
   ORDER BY first_seen_at DESC LIMIT 1;"

echo "──── done ────"
