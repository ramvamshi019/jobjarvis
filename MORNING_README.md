# Good morning — here's what I did and what to run

## What changed

The standalone script has been heavily expanded:

- `backend/scripts/add_companies_oneshot.py` now has **6,191 unique curated company slugs** (up from ~700 last night) across all major tech sectors, YC batches, EU/APAC/LatAm/Israel startups, Fortune 500/1000, and more.
- Added 4 new ATS probe functions: **BambooHR**, **TeamTailor**, **Recruitee**, **iCIMS** (was 5, now 9 platforms).
- Added a checkpoint file at `/tmp/companies_checkpoint.json` so if the run is interrupted, just re-run the same command and it picks up where it left off — no wasted work.
- Stays clear of the broken Celery `ats_directory_tasks`. Talks directly to Postgres via asyncpg.

The script is syntactically clean and parsed-validated.

---

## How to run

Two commands. Total wait: 2–4 hours depending on which ATSes are slow today.

```bash
cd ~/Desktop/jobjarvis

# 1. Push the new script into the worker container
docker cp backend/scripts/add_companies_oneshot.py \
  jobjarvis_celery_worker:/tmp/add_companies.py

# 2. Run it (unbuffered output so you see live progress)
docker exec jobjarvis_celery_worker python3 -u /tmp/add_companies.py
```

You can leave it running in that terminal and check progress in another one with:

```bash
watch -n 60 "docker exec jobjarvis_postgres psql -U jobjarvis -d jobjarvis -c \"SELECT ats, COUNT(*) AS total FROM companies WHERE active=true GROUP BY ats ORDER BY total DESC;\""
```

---

## What you'll see during the run

Per-platform output:

```
[greenhouse] probing 6191 slugs (concurrency=60)…
  [greenhouse] 500/6191 probed, 142 confirmed so far…
  [greenhouse] 1000/6191 probed, 287 confirmed so far…
  ...
[greenhouse] confirmed 1450 / 6191
[greenhouse] upserted 1450  errors 0
[greenhouse] DB total active companies: 2667
```

Then it moves to lever, ashby, smartrecruiters, workable, bamboohr, teamtailor, recruitee, icims in sequence.

A final SUMMARY block prints at the end with totals.

---

## Realistic expectations

| Platform | Hit rate | Expected hits |
|---|---|---|
| Greenhouse | 20–25% | 1,200–1,500 |
| Lever | 12–18% | 750–1,100 |
| Ashby | 8–12% | 500–750 |
| SmartRecruiters | 4–7% | 250–450 |
| Workable | 5–10% | 300–600 |
| BambooHR | 4–8% | 250–500 |
| TeamTailor | 6–10% | 350–600 |
| Recruitee | 3–6% | 200–400 |
| iCIMS | 3–6% | 200–400 |

**Total raw hits:** ~4,000–6,300

**Net new companies in your DB after dedup by name:** roughly 2,500–4,500

Going from 1,217 → roughly **3,500–5,500 active companies** after this run completes. Then your existing scan_tasks pick up the new companies on their normal schedule and start populating jobs from them.

---

## If something goes wrong

**Script crashes early on:**
```bash
docker compose logs celery_worker --tail=50
```
Re-run the same docker exec command — checkpoint will resume.

**Container restarts mid-run:**
The checkpoint persists on the container's `/tmp` (not the host), so if the container is fully recreated it's gone. To make it survive container restarts, mount it from the host before running:
```bash
docker exec jobjarvis_celery_worker cp /tmp/companies_checkpoint.json /app/checkpoint.json 2>/dev/null
```
But for a one-shot run this isn't necessary.

**You want to start fresh:**
```bash
docker exec jobjarvis_celery_worker rm -f /tmp/companies_checkpoint.json
```
Then re-run.

**Errors on every upsert again:**
We already fixed `scan_priority NOT NULL`. If a different NOT NULL column shows up, copy the error message and I'll patch it — same simple fix as before.

---

## Why this won't get you to 30k

Honest expectation-setting: you asked for 30,000+ real companies. That's not realistically achievable in a single overnight run because:

1. **Greenhouse, Lever, Ashby, SmartRecruiters** combined have maybe 15,000–20,000 publicly addressable customers globally. We're already covering a big chunk with ~6k well-known slugs.
2. To scale past ~5k requires either (a) **slug-guessing discovery** (your existing `discovery_tasks` does this — 0.1–1% hit rate but probes millions, slowly), or (b) **scraping ATS provider directories** (some publish customer lists), or (c) **buying a company DB** like Clearbit or Crunchbase Enterprise.

Tonight gets you to ~5k clean. To go from 5k → 30k:

- **Easy win:** Let `discovery_tasks` (your slug-guesser) keep running on its 2-hour beat. Over 1–2 weeks it'll add 5–15k more.
- **Medium win:** Fix the `_run_async` deadlock in `ats_directory_tasks.py` so the curated list runs every 4 hours like it was designed to.
- **Hard win:** Wire in the Apify MCP I see you have to scrape Crunchbase / LinkedIn for company lists, then feed those slugs into a similar standalone script.

I can take any of those on whenever you wake up and want to push further. For now, run the 2-step command above and you'll wake up with thousands more companies than you went to bed with.
