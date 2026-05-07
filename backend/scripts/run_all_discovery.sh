#!/usr/bin/env bash
# Run every company-discovery script in sequence, then dedup, then report.
#
# Total runtime: ~30–45 minutes.
# Total cost:    $0.
# Adds:          ~10,000–20,000 new companies on top of your current corpus.
#
# Usage:
#   chmod +x backend/scripts/run_all_discovery.sh
#   bash    backend/scripts/run_all_discovery.sh

set -e  # die on any error

WORKER=jobjarvis_celery_worker
DB=jobjarvis_postgres
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=================================================================="
echo "  JobJarvis company discovery pipeline"
echo "=================================================================="

count_before=$(docker exec "$DB" psql -U jobjarvis -d jobjarvis -tAc \
  "SELECT COUNT(*) FROM companies WHERE active=true")
echo "  Active companies before: $count_before"
echo

echo "→ Copying scripts into worker container…"
docker cp "$SCRIPTS_DIR/discovery_lib.py"             "$WORKER:/tmp/discovery_lib.py"
docker cp "$SCRIPTS_DIR/discover_hn_whoshiring.py"    "$WORKER:/tmp/discover_hn_whoshiring.py"
docker cp "$SCRIPTS_DIR/discover_yc_complete.py"      "$WORKER:/tmp/discover_yc_complete.py"
docker cp "$SCRIPTS_DIR/discover_awesome_lists.py"    "$WORKER:/tmp/discover_awesome_lists.py"
docker cp "$SCRIPTS_DIR/discover_known_lists.py"      "$WORKER:/tmp/discover_known_lists.py"
docker cp "$SCRIPTS_DIR/dedup_companies.py"           "$WORKER:/tmp/dedup_companies.py"

echo
echo "─── 1/4 · HN 'Who is hiring' (~3-5k companies, ~15 min) ───"
docker exec "$WORKER" python3 -u /tmp/discover_hn_whoshiring.py || true

echo
echo "─── 2/4 · YC complete batches (~3-5k companies, ~5 min) ───"
docker exec "$WORKER" python3 -u /tmp/discover_yc_complete.py || true

echo
echo "─── 3/4 · Awesome-lists (~2-3k companies, ~3 min) ───"
docker exec "$WORKER" python3 -u /tmp/discover_awesome_lists.py || true

echo
echo "─── 4/4 · Common Crawl ATS index (~5-15k companies, ~10 min) ───"
docker exec "$WORKER" python3 -u /tmp/discover_known_lists.py || true

echo
echo "─── Final · Dedup case-variants ───"
docker exec "$WORKER" python3 -u /tmp/dedup_companies.py || true

echo
echo "=================================================================="
echo "  RESULT"
echo "=================================================================="
count_after=$(docker exec "$DB" psql -U jobjarvis -d jobjarvis -tAc \
  "SELECT COUNT(*) FROM companies WHERE active=true")
delta=$((count_after - count_before))
echo "  Active companies before: $count_before"
echo "  Active companies after:  $count_after"
echo "  Net new:                 $delta"
echo
echo "  Breakdown by ATS:"
docker exec "$DB" psql -U jobjarvis -d jobjarvis -c \
  "SELECT ats, COUNT(*) AS cos FROM companies WHERE active=true \
   GROUP BY ats ORDER BY cos DESC;"

echo
echo "Next step: jobs from the new companies will start appearing in your"
echo "DB on the next Celery scan cycle (~10-30 min).  No action needed."
